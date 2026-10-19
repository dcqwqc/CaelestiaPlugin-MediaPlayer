"""Origin-restricted VixSrc HLS normalizer for GStreamer/Showtime.

VixSrc publishes AES-128 encrypted MPEG-TS segments under mixed .html/.m4s
extensions; some use Zstandard HTTP Content-Encoding. GStreamer HLS may treat
the .html response as markup or not decode zstd. Retain the signed upstream
URLs in the host process only; return a bearer-scoped MPEG-TS HLS playlist to
Showtime, with uniformly labeled, decompressed (still AES-encrypted) segments.
"""
from __future__ import annotations

import re
import threading
import urllib.error
import urllib.parse
import urllib.request

VIX_PREFIX = "/vix-hls/"
HLS_MIME = "application/vnd.apple.mpegurl"
CHUNK_MIME = "video/mp2t"
MAX_PLAYLIST = 3_000_000
MAX_SEGMENT = 30_000_000
CDN_SUFFIXES = (".bravecastle98.boats", ".vix-content.net")
URI_TAG_RE = re.compile(r'URI="([^"]+)"')


def is_vixsrc_hls(url):
    try:
        value = urllib.parse.urlsplit(url)
        return (value.scheme == "https" and value.hostname == "vixsrc.to"
                and value.username is None and value.password is None
                and re.fullmatch(r"/playlist/[1-9][0-9]{0,12}", value.path) is not None)
    except (ValueError, TypeError):
        return False


def _safe_origin(url, *, master=False):
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ""
        return (p.scheme == "https" and p.username is None and p.password is None
                and p.port in (None, 443)
                and (host == "vixsrc.to" if master else (
                    host == "vixsrc.to" or any(host.endswith(s) for s in CDN_SUFFIXES))))
    except (ValueError, TypeError):
        return False


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, host):
        super().__init__()
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).hostname != self.host or not _safe_origin(newurl):
            raise urllib.error.HTTPError(newurl, 403, "Cross-origin redirect denied", headers, None)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def normalize_playlist(playlist, source_url, path_prefix, kind):
    """Rewrite HLS media/key/variant URIs to loopback; never echo upstream URLs."""
    if not playlist.startswith("#EXTM3U"):
        raise ValueError("not an HLS manifest")
    refs = {"variant": [], "segment": [], "key": []}
    lines = []
    for raw in playlist.splitlines():
        if raw.startswith("#EXT-X-KEY:") or raw.startswith("#EXT-X-MAP:"):
            def map_uri(m):
                remote = urllib.parse.urljoin(source_url, m.group(1))
                if not _safe_origin(remote, master=True):
                    raise ValueError("cross-origin key or map")
                ref_type = "key"
                refs[ref_type].append(remote)
                return 'URI="' + path_prefix + "key/" + str(len(refs[ref_type]) - 1) + '.key"'
            lines.append(URI_TAG_RE.sub(map_uri, raw))
        elif raw and not raw.startswith("#"):
            remote = urllib.parse.urljoin(source_url, raw)
            ref_type = "variant" if kind == "master" else "segment"
            if not _safe_origin(remote, master=(kind == "master")):
                raise ValueError("cross-origin media URL")
            refs[ref_type].append(remote)
            suffix = ".m3u8" if kind == "master" else ".ts"
            lines.append(path_prefix + ref_type + "/" + str(len(refs[ref_type]) - 1) + suffix)
        else:
            lines.append(raw)
    return ("\n".join(lines) + "\n").encode("utf-8"), refs


class VixHlsSession:
    def __init__(self, source, headers, token):
        if not is_vixsrc_hls(source):
            raise ValueError("source must be a VixSrc signed playlist")
        self.source = source
        self.headers = dict(headers)
        self.token = token
        self.lock = threading.Lock()
        self.variants = {}
        self.segments = {}
        self.keys = {}
        self.manifests = {}

    def _read(self, url, *, segment=False):
        if not _safe_origin(url, master=not segment):
            raise ValueError("upstream URL is outside allowed origins")
        host = urllib.parse.urlsplit(url).hostname
        headers = {"Accept-Encoding": "identity", "User-Agent": self.headers.get("User-Agent", "Mozilla/5.0")}
        if host == "vixsrc.to":
            headers.update({k: v for k, v in self.headers.items()
                            if k.lower() in ("referer", "origin", "user-agent")})
        else:
            headers["Referer"] = "https://vixsrc.to/"
        request = urllib.request.Request(url, headers=headers)
        opener = urllib.request.build_opener(_NoCrossHostRedirect(host))
        with opener.open(request, timeout=15) as reply:
            encoding = reply.headers.get("Content-Encoding", "").lower()
            limit = MAX_SEGMENT if segment else MAX_PLAYLIST
            data = reply.read(limit + 1)
            if len(data) > limit:
                raise ValueError("media payload too large")
            if encoding == "zstd":
                import zstandard
                data = zstandard.ZstdDecompressor().decompress(
                    data, max_output_size=MAX_SEGMENT
                )
            elif encoding not in ("", "identity"):
                raise ValueError("unsupported encoded fragment")
            return data

    def fetch(self, route, port):
        # Each signed bridge instance has a private namespace. Restrict all
        # routes to exact, fixed-length integer IDs; no URL forwarding.
        prefix = f"http://127.0.0.1:{port}{VIX_PREFIX}{self.token}/"
        if route == "master.m3u8":
            with self.lock:
                cached = self.manifests.get(route)
            if cached is not None:
                return cached, HLS_MIME
            body = self._read(self.source)
            manifest, refs = normalize_playlist(body.decode("utf-8"), self.source, prefix, "master")
            with self.lock:
                self.variants.update(enumerate(refs["variant"]))
                self.manifests[route] = manifest
            return manifest, HLS_MIME
        variant = re.fullmatch(r"variant/([0-9]{1,2})\.m3u8", route)
        if variant:
            n = int(variant.group(1))
            with self.lock:
                original = self.variants.get(n)
                cached = self.manifests.get(route)
            if cached is not None:
                return cached, HLS_MIME
            if original is None:
                raise KeyError("unknown variant")
            raw = self._read(original)
            vprefix = prefix + str(n) + "/"
            data, refs = normalize_playlist(raw.decode("utf-8"), original, vprefix, "media")
            with self.lock:
                self.segments[n] = refs["segment"]
                self.keys[n] = refs["key"]
                self.manifests[route] = data
            return data, HLS_MIME
        segment = re.fullmatch(r"([0-9]{1,2})/segment/([0-9]{1,5})\.ts", route)
        key = re.fullmatch(r"([0-9]{1,2})/key/([0-9]{1,2})\.key", route)
        if segment or key:
            n, i = map(int, (segment or key).groups())
            with self.lock:
                entries = self.segments.get(n, []) if segment else self.keys.get(n, [])
                original = entries[i] if i < len(entries) else None
            if original is None:
                raise KeyError("unknown segment or key")
            data = self._read(original, segment=bool(segment))
            if segment and (not data or len(data) % 16):
                raise ValueError("invalid AES-128 encrypted segment")
            if key and len(data) != 16:
                raise ValueError("invalid HLS AES key")
            return data, CHUNK_MIME if segment else "application/octet-stream"
        raise KeyError("unknown HLS route")


def handle_vixsrc_hls_request(handler):
    """Route a bearer-scoped local media request with no secret response echo."""
    bridge = handler.bridge
    with bridge.lock:
        source, token, headers = bridge.url, bridge.media_token, bridge.header_dict()
        active = getattr(bridge, "vixsrc_hls", None)
        if active is None or active.source != source or active.token != token:
            if not is_vixsrc_hls(source):
                handler.send_error(404, "no active source")
                return
            active = VixHlsSession(source, headers, token)
            bridge.vixsrc_hls = active
    marker = VIX_PREFIX + token + "/"
    if not handler.path.startswith(marker):
        handler.send_error(404, "no such media session")
        return
    route = urllib.parse.urlsplit(handler.path).path[len(marker):]
    try:
        body, mime = active.fetch(route, handler.server.server_address[1])
    except KeyError:
        handler.send_error(404, "unknown media route")
        return
    except (ValueError, UnicodeDecodeError, urllib.error.URLError, TimeoutError, OSError):
        handler.send_error(502, "media fragment unavailable")
        return
    handler.send_response(200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "private, max-age=60")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    if handler.command != "HEAD":
        try:
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
