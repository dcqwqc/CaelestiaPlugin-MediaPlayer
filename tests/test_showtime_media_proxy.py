"""Safe, offline tests for the Showtime adaptive-stream loopback proxy."""
from __future__ import annotations

import http.client
import importlib.util
import sys
import threading
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(ROOT))
import showtime_media_proxy as proxy


class MediaURLTests(unittest.TestCase):
    def test_url_requires_public_https_and_adaptive_manifest(self):
        invalid = (
            "file:///tmp/demo.mpd", "http://cdn.example.org/master.mpd",
            "https://127.0.0.1/master.mpd", "https://localhost/master.mpd",
            "https://internal.local/master.mpd", "https://user:pass@cdn.example.org/v.mpd",
            "https://cdn.example.org/video.mp4",
        )
        for url in invalid:
            self.assertFalse(proxy.eligible_adaptive_url(url), url)
        self.assertTrue(proxy.eligible_adaptive_url("https://cdn.example.org/series/master.mpd"))

    def test_relay_preserves_origin_and_relative_fragment_path(self):
        source = "https://cdn.example.org/show/manifest.mpd"
        entry = proxy.media_proxy_url(source, 9921, "opaque-token")
        self.assertEqual(entry, "http://127.0.0.1:9921/media/opaque-token/show/manifest.mpd")
        self.assertEqual(proxy.target_for_request(
            source, "/media/opaque-token/show/video/init.mp4", "opaque-token",
        ), "https://cdn.example.org/show/video/init.mp4")
        self.assertIsNone(proxy.target_for_request(
            source, "/media/stolen-token/show/video/init.mp4", "opaque-token",
        ))
        self.assertIsNone(proxy.target_for_request(
            source, "/media/opaque-token//other.example/foo", "opaque-token",
        ))

    def test_rewrites_relative_root_absolute_and_attribute_hls_urls_only_on_source_origin(self):
        source = "https://cdn.example.org/show/master.m3u8?sig=secret"
        manifest = (
            b"#EXTM3U\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=1234\n"
            b"/show/variants/hd.m3u8?sig=abc\n"
            b"#EXT-X-MEDIA:TYPE=AUDIO,URI=\"../audio/jp.m3u8?auth=abc\"\n"
            b"#EXT-X-KEY:METHOD=AES-128,URI=\"https://cdn.example.org/keys/key?sig=z\"\n"
            b"#EXT-X-MAP:URI=\"/show/init.mp4\"\n"
            b"https://cdn.example.org/show/chunk01.ts?q=1\n"
            b"relative/chunk02.ts\n"
            b"#EXT-X-KEY:URI=\"https://evil.example/capture\"\n"
        )
        result = proxy.rewrite_hls_manifest(manifest, source, "t123").decode()
        for expected in (
            "/media/t123/show/variants/hd.m3u8?sig=abc",
            'URI="/media/t123/audio/jp.m3u8?auth=abc"',
            'URI="/media/t123/keys/key?sig=z"',
            'URI="/media/t123/show/init.mp4"',
            "/media/t123/show/chunk01.ts?q=1",
            "/media/t123/show/relative/chunk02.ts",
        ):
            self.assertIn(expected, result)
        self.assertIn('URI="https://evil.example/capture"', result)
        self.assertNotIn("https://cdn.example.org/show/chunk01.ts", result)
        self.assertEqual(proxy.rewrite_hls_manifest(bytes((255, 254)), source, "t123"), bytes((255, 254)))

    def test_cross_origin_redirect_cannot_receive_source_credentials(self):
        guard = proxy.SingleOriginRedirect(("https", "cdn.example.org"))
        with self.assertRaises(HTTPError) as caught:
            guard.redirect_request(None, None, 302, "Found", {}, "https://evil.example/capture")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()


class _Response:
    def __init__(self, payload=b"ABCD", status=206):
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/octet-stream"
        self.headers["Content-Length"] = str(len(payload))
        if status == 206:
            self.headers["Content-Range"] = "bytes 0-3/200"
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n):
        part, self.payload = self.payload[:n], self.payload[n:]
        return part


class HTTPProxyTests(unittest.TestCase):
    def test_controller_auth_and_failed_playback_event(self):
        spec = importlib.util.spec_from_file_location("shim_auth_test", ROOT / "showtime-mpv-shim.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        b = m.Bridge({"url": "https://cdn.example.org/series/master.mpd"})
        server = m.HTTPServer(("127.0.0.1", 0), b)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        try:
            conn.request("GET", "/api/session")
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            headers = {"X-Showtime-Session-Token": b.token}
            conn.request("GET", "/api/session", headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            failure = b'{"reason":"access-denied","media_revision":1}'
            conn.request("POST", "/api/playback-error", body=failure)
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            self.assertIsNone(b.failed_media_revision)
            conn.request("POST", "/api/playback-error", body=failure, headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(__import__("json").loads(response.read())["accepted"])
            self.assertEqual(b.failed_media_revision, 1)
            conn.request("POST", "/api/playback-error", body=failure, headers=headers)
            response = conn.getresponse()
            self.assertFalse(__import__("json").loads(response.read())["accepted"])
        finally:
            conn.close()
            server.shutdown()
            server.server_close()

    def test_hls_manifest_rewrites_media_and_updates_length_without_leaking_headers(self):
        spec = importlib.util.spec_from_file_location("hls_test_shim", ROOT / "showtime-mpv-shim.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        bridge = m.Bridge({"url": "https://cdn.example.org/show/master.m3u8"})
        bridge.http_headers = ["Cookie: source=secret", "Referer: https://player.example/"]
        server = m.HTTPServer(("127.0.0.1", 0), bridge)
        bridge.media_proxy_port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        requests = []
        manifest = b"#EXTM3U\n#EXTINF:4.0,\n/show/video/001.ts?sig=a\n"
        class FakeOpener:
            def open(self, request, timeout):
                requests.append((request.full_url, dict(request.header_items())))
                return _Response(
                    payload=manifest if request.full_url.endswith("master.m3u8") else b"DATA",
                    status=200,
                )
        try:
            with patch.object(proxy.urllib.request, "build_opener", return_value=FakeOpener()):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
                route = proxy.media_proxy_url(bridge.url, bridge.media_proxy_port, bridge.media_token)
                path = route.partition("127.0.0.1:" + str(server.server_address[1]))[2]
                conn.request("GET", path, headers={"Range": "bytes=0-20"})
                res = conn.getresponse()
                body = res.read()
                self.assertEqual(res.status, 200)
                self.assertEqual(int(res.getheader("Content-Length")), len(body))
                expected = f"/media/{bridge.media_token}/show/video/001.ts?sig=a"
                self.assertIn(expected.encode(), body)
                self.assertNotIn("Range", requests[0][1])
                conn.request("GET", expected, headers={"Cookie": "attacker=value"})
                segment = conn.getresponse()
                self.assertEqual(segment.status, 200)
                self.assertEqual(segment.read(), b"DATA")
                self.assertEqual(requests[1][0], "https://cdn.example.org/show/video/001.ts?sig=a")
                forwarded = {k.lower():v for k,v in requests[1][1].items()}
                self.assertEqual(forwarded["cookie"], "source=secret")
                self.assertEqual(forwarded["referer"], "https://player.example/")
                conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_cross_cdn_manifest_links_use_registered_anonymous_relay(self):
        spec = importlib.util.spec_from_file_location("cross_host_test_shim", ROOT / "showtime-mpv-shim.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        bridge = m.Bridge({"url": "https://cdn.example.org/movie/master.m3u8"})
        bridge.http_headers = [
            "Cookie: access-secret",
            "Authorization: Bearer access-secret",
            "Referer: https://private-player.example/?token=secret",
            "User-Agent: Chrome-test",
        ]
        server = m.HTTPServer(("127.0.0.1", 0), bridge)
        bridge.media_proxy_port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        requests = []
        original = "https://other.example.net/video/segment0001.m4s?sig=private"
        manifest = ("#EXTM3U\n#EXTINF:4.0,\n" + original + "\n").encode()
        class FakeOpener:
            def open(self, request, timeout):
                requests.append((request.full_url, dict(request.header_items())))
                return _Response(
                    payload=manifest if request.full_url.endswith("master.m3u8") else b"DATA",
                    status=200,
                )
        try:
            with (
                patch.object(proxy, "_public_https_host", return_value=True),
                patch.object(proxy.urllib.request, "build_opener", return_value=FakeOpener()),
            ):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
                start = proxy.media_proxy_url(bridge.url, bridge.media_proxy_port, bridge.media_token)
                conn.request("GET", start.partition(f"127.0.0.1:{bridge.media_proxy_port}")[2])
                initial = conn.getresponse()
                text = initial.read().decode()
                self.assertEqual(initial.status, 200)
                self.assertNotIn(original, text)
                rewritten = text.splitlines()[-1]
                self.assertIn("/_ref/", rewritten)
                self.assertRegex(rewritten, r"/media/[^/]+/_ref/[0-9a-f]{32}")
                conn.request("GET", rewritten, headers={"Cookie": "forged=attacker", "Range": "bytes=0-3"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"DATA")
                self.assertEqual(requests[1][0], original)
                sent = {k.lower(): v for k, v in requests[1][1].items()}
                self.assertEqual(sent.get("user-agent"), "Chrome-test")
                self.assertEqual(sent.get("range"), "bytes=0-3")
                self.assertNotIn("cookie", sent)
                self.assertNotIn("authorization", sent)
                self.assertNotIn("referer", sent)
                bridge.set_media("https://cdn.example.org/movie/new.m3u8")
                conn.request("GET", rewritten)
                revoked = conn.getresponse()
                self.assertEqual(revoked.status, 404)
                revoked.read()
                conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_media_is_bearer_scoped_and_source_headers_forwarded(self):
        spec = importlib.util.spec_from_file_location("showtime_test_shim", ROOT / "showtime-mpv-shim.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        bridge = module.Bridge({"url": "https://cdn.example.org/show/manifest.mpd"})
        bridge.http_headers = ["Cookie: signed=abc", "Referer: https://player.example/"]
        server = module.HTTPServer(("127.0.0.1", 0), bridge)
        bridge.media_proxy_port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        requested = []
        class FakeOpener:
            def open(self, request, timeout):
                requested.append((request.full_url, dict(request.header_items())))
                return _Response()
        with patch.object(proxy.urllib.request, "build_opener", return_value=FakeOpener()):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            try:
                uri = bridge.session()["media_url"]
                route = uri.partition(f"127.0.0.1:{server.server_address[1]}")[2]
                conn.request("GET", route.replace("/manifest.mpd", "/video/init.mp4"), headers={"Range": "bytes=0-3", "Cookie": "attacker=value"})
                response = conn.getresponse()
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b"ABCD")
                self.assertEqual(requested[0][0], "https://cdn.example.org/show/video/init.mp4")
                outgoing = {k.lower(): v for k, v in requested[0][1].items()}
                self.assertEqual(outgoing["cookie"], "signed=abc")
                self.assertEqual(outgoing["referer"], "https://player.example/")
                self.assertEqual(outgoing["range"], "bytes=0-3")
                conn.request("GET", route.replace(bridge.media_token, "expired-token"))
                result = conn.getresponse()
                self.assertEqual(result.status, 404)
                result.read()
                bridge.set_media("https://cdn.example.org/show/manifest.mpd", {"http-header-fields": "Cookie: new=token"})
                conn.request("GET", route)
                result = conn.getresponse()
                self.assertEqual(result.status, 404, "replaced source must revoke old bearer token")
                result.read()
            finally:
                conn.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    unittest.main()
