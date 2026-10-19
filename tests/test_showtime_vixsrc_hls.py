"""VixSrc HLS adapter regression tests: correct media, scoped access, no CDN token leak."""
import http.client
import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(ROOT))
import showtime_vixsrc_hls as vix

def shim_module():
    spec = importlib.util.spec_from_file_location("showtime_hls_bridge_test", ROOT / "showtime-mpv-shim.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

URL = "https://vixsrc.to/playlist/475052?token=signed&expires=1888888888"
MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720
https://vixsrc.to/playlist/475052?token=secret-variant
"""
VARIANT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=AES-128,URI="https://vixsrc.to/key/movie.key",IV=0xaabbcc
#EXTINF:10.0,
https://sc-u12-01.bravecastle98.boats/hls/seg1.html?key=hidden
#EXTINF:7.0,
https://sc-u12-01.bravecastle98.boats/hls/seg2.m4s?key=hidden
#EXT-X-ENDLIST
"""

class VixNormalizerTests(unittest.TestCase):
    def test_only_vixsrc_playlist_is_rewritten(self):
        self.assertTrue(vix.is_vixsrc_hls(URL))
        for u in [
            "https://vixsrc.to.evil.example/playlist/475052",
            "https://vixsrc.to/embed/475052",
            "http://vixsrc.to/playlist/475052",
            "https://user:pass@vixsrc.to/playlist/475052",
            "file:///etc/shadow",
        ]:
            self.assertFalse(vix.is_vixsrc_hls(u), u)

    def test_hls_variant_aes_key_and_segments_become_media_urls(self):
        prefix = "http://127.0.0.1:9900/vix-hls/secret/1/"
        text, refs = vix.normalize_playlist(VARIANT, URL, prefix, "media")
        rendered = text.decode()
        self.assertIn("URI=\"" + prefix + "key/0.key\"", rendered)
        self.assertIn(prefix + "segment/0.ts", rendered)
        self.assertIn(prefix + "segment/1.ts", rendered)
        self.assertNotIn("bravecastle98", rendered)
        self.assertNotIn("key=hidden", rendered)
        self.assertEqual(len(refs["segment"]), 2)
        self.assertEqual(len(refs["key"]), 1)
        self.assertTrue(rendered.endswith("#EXT-X-ENDLIST\n"))

    def test_cross_origin_playlist_routes_are_rejected(self):
        with self.assertRaises(ValueError):
            vix.normalize_playlist("#EXTM3U\nhttps://127.0.0.1/etc\n", URL, "/", "media")
        with self.assertRaises(ValueError):
            vix.normalize_playlist("#EXTM3U\nhttps://evil.example/video.ts\n", URL, "/", "media")
        with self.assertRaises(ValueError):
            vix.normalize_playlist(VARIANT.replace("https://vixsrc.to/key/", "https://evil.example/key/"), URL, "/", "media")

    def test_shim_returns_normalized_local_url_and_hides_upstream_credentials(self):
        m = shim_module()
        bridge = m.Bridge({"url": URL, "http_headers": ["Referer: https://vixsrc.to/", "User-Agent: Mozilla/5.0"]})
        server = m.HTTPServer(("127.0.0.1", 0), bridge)
        bridge.media_proxy_port = server.server_address[1]
        mock = {
            URL: MASTER.encode(),
            "https://vixsrc.to/playlist/475052?token=secret-variant": VARIANT.encode(),
            "https://vixsrc.to/key/movie.key": b"A" * 16,
            "https://sc-u12-01.bravecastle98.boats/hls/seg1.html?key=hidden": b"B" * 32,
            "https://sc-u12-01.bravecastle98.boats/hls/seg2.m4s?key=hidden": b"C" * 32,
        }
        def load(_self, url, *, segment=False):
            return mock[url]
        with patch.object(vix.VixHlsSession, "_read", load):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                uri = bridge.session()["media_url"]
                self.assertTrue(uri.endswith("/master.m3u8"))
                self.assertNotIn("signed", uri)
                self.assertEqual(bridge.session()["http_headers"], {})
                base = uri.removeprefix("http://127.0.0.1:" + str(server.server_address[1]))
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
                def read(path, method="GET"):
                    conn.request(method, path)
                    res = conn.getresponse()
                    return res.status, res.getheader("Content-Type"), res.read()
                self.assertEqual(read(base)[0], 200)
                vbase = base.rsplit("/", 1)[0] + "/"
                self.assertEqual(read(vbase + "variant/0.m3u8")[0], 200)
                status, mime, body = read(vbase + "0/segment/0.ts")
                self.assertEqual((status, mime, body), (200, vix.CHUNK_MIME, b"B" * 32))
                self.assertEqual(read(vbase + "0/key/0.key")[2], b"A" * 16)
                self.assertEqual(read(vbase + "0/segment/1.ts", "HEAD")[2], b"")
                self.assertEqual(read(base.replace(bridge.media_token, "guess"))[0], 404)
                self.assertEqual(read(vbase + "0/segment/99999.ts")[0], 404)
                old = base
                bridge.set_media("https://cdn.example.org/film.m3u8")
                self.assertEqual(read(old)[0], 404)
                conn.close()
            finally:
                server.shutdown()
                server.server_close()

if __name__ == "__main__":
    unittest.main()
