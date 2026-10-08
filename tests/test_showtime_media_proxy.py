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

    def test_cross_origin_redirect_cannot_receive_source_credentials(self):
        guard = proxy.SingleOriginRedirect(("https", "cdn.example.org"))
        with self.assertRaises(HTTPError) as caught:
            guard.redirect_request(None, None, 302, "Found", {}, "https://evil.example/capture")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()


class _Response:
    def __init__(self):
        self.status = 206
        self.headers = Message()
        self.headers["Content-Type"] = "application/octet-stream"
        self.headers["Content-Length"] = "4"
        self.headers["Content-Range"] = "bytes 0-3/200"
        self.payload = b"ABCD"

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
