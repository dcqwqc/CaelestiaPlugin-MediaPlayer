"""Local, origin-locked media relay for Showtime adaptive playback.

GStreamer's adaptivedemux2 uses an independent libsoup session to fetch DASH/HLS
fragments. Passing headers to souphttpsrc only authenticates the manifest, not
its fragments. Relaying adaptive URLs via the already-loopback IPC server keeps
provider credentials on the source origin and avoids modifying GStreamer.
"""
from __future__ import annotations

import functools
import hashlib
import ipaddress
import socket
import re
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus


SAFE_RESPONSE_HEADERS = ("Content-Length", "Content-Range", "Accept-Ranges", "Cache-Control")
ADAPTIVE_SUFFIXES = (".mpd", ".m3u8")
MEDIA_PREFIX = "/media/"
HLS_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
MAX_MEDIA_REFERENCES = 8192


def eligible_adaptive_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname or ""
        try:
            ipaddress.ip_address(hostname)
            return False  # Never proxy local/remote literal IP addresses.
        except ValueError:
            pass
        if "." not in hostname or hostname.endswith((".local", ".localhost", ".internal")):
            return False
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.path.lower().endswith(ADAPTIVE_SUFFIXES)
        )
    except ValueError:
        return False


def media_proxy_url(url, port, token):
    if not eligible_adaptive_url(url) or not port or not token:
        return None
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((
        "http", f"127.0.0.1:{port}",
        f"{MEDIA_PREFIX}{token}/{parsed.path.lstrip('/')}",
        parsed.query, "",
    ))


def target_for_request(source, request_path, token):
    """Preserve the source origin while resolving DASH/HLS relative paths."""
    if not eligible_adaptive_url(source):
        return None
    incoming = urllib.parse.urlsplit(request_path)
    prefix = f"{MEDIA_PREFIX}{token}/"
    if not incoming.path.startswith(prefix):
        return None
    path = "/" + incoming.path[len(prefix):]
    # Never allow a forged media URL to select another host or protocol.
    if not path.startswith("/") or path.startswith("//"):
        return None
    p = urllib.parse.urlsplit(source)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, incoming.query, ""))


@functools.lru_cache(maxsize=128)
def _public_https_host(host):
    try:
        if not host or "." not in host or host.endswith((".local", ".localhost", ".internal")):
            return False
        # Do not let a malicious manifest instruct the relay to read LAN,
        # loopback, metadata endpoints or reserved networks.
        addrs = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(addrs) and all(ipaddress.ip_address(x[4][0]).is_global for x in addrs)
    except (ValueError, OSError):
        return False


def safe_external_media_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "https" and parsed.username is None
                and parsed.password is None and parsed.port in (None, 443)
                and _public_https_host((parsed.hostname or "").lower()))
    except ValueError:
        return False


def register_external_media_reference(bridge, url, token):
    if not safe_external_media_url(url):
        return None
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    with bridge.lock:
        if token != bridge.media_token:
            return None
        refs = bridge.external_media_refs
        if key not in refs and len(refs) >= MAX_MEDIA_REFERENCES:
            return None
        refs[key] = url
    return f"{MEDIA_PREFIX}{token}/_ref/{key}"


def proxy_hls_reference(reference, manifest_url, token, *, original_media_url=None, register_cross_origin=None):
    # Only same-origin HTTPS media enters this bearer-scoped relay.
    try:
        absolute = urllib.parse.urljoin(manifest_url, reference)
        original = urllib.parse.urlsplit(manifest_url)
        parsed = urllib.parse.urlsplit(absolute)
        root = urllib.parse.urlsplit(original_media_url or manifest_url)
        if parsed.username or parsed.password or not parsed.path.startswith("/"):
            return reference
        if (parsed.scheme.lower(), parsed.netloc.lower()) == (
            root.scheme.lower(), root.netloc.lower()
        ):
            path = f"{MEDIA_PREFIX}{token}/{parsed.path.lstrip('/')}"
            return urllib.parse.urlunsplit(("", "", path, parsed.query, ""))
        # A manifest explicitly named a different HTTPS CDN. Only a registered
        # bearer route may request it; never give that host source cookies.
        if register_cross_origin is not None:
            return register_cross_origin(absolute) or reference
        return reference
    except ValueError:
        return reference


def rewrite_hls_manifest(payload, manifest_url, token, *, original_media_url=None, register_cross_origin=None):
    # Rewrites plain media/variant URLs and quoted URI attributes (keys, maps,
    # alternate audio/subtitles, session keys); all other tags are preserved.
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload
    out = []
    for line in content.splitlines(keepends=True):
        value = line.strip()
        if not value:
            out.append(line)
        elif value.startswith("#"):
            # HLS syntax requires the URI attribute to be quoted.
            start = 0
            while True:
                begin = line.find('URI="', start)
                if begin < 0:
                    break
                uri_start = begin + len('URI="')
                uri_end = line.find('"', uri_start)
                if uri_end < 0:
                    break
                original = line[uri_start:uri_end]
                replacement = proxy_hls_reference(original, manifest_url, token, original_media_url=original_media_url,
                                                   register_cross_origin=register_cross_origin)
                line = line[:uri_start] + replacement + line[uri_end:]
                start = uri_start + len(replacement) + 1
            out.append(line)
        else:
            out.append(line.replace(value, proxy_hls_reference(value, manifest_url, token, original_media_url=original_media_url,
                                             register_cross_origin=register_cross_origin), 1))
    return "".join(out).encode("utf-8")


class SingleOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        super().__init__()
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        dest = urllib.parse.urlsplit(newurl)
        if (dest.scheme.lower(), dest.netloc.lower()) != self.origin:
            raise urllib.error.HTTPError(newurl, HTTPStatus.FORBIDDEN,
                "cross-origin media redirect blocked", headers, None)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def relay_request(handler):
    """Handle only a bearer-scoped GET/HEAD. No external proxy capabilities."""
    bridge = handler.bridge
    with bridge.lock:
        token = bridge.media_token
        source = bridge.url
        headers = bridge.header_dict()
    requested = urllib.parse.urlsplit(handler.path)
    ref_prefix = f"{MEDIA_PREFIX}{token}/_ref/"
    is_external = requested.path.startswith(ref_prefix)
    if is_external:
        ref_key = requested.path[len(ref_prefix):]
        with bridge.lock:
            target = (
                bridge.external_media_refs.get(ref_key)
                if re.fullmatch(r"[0-9a-f]{32}", ref_key) and not requested.query
                else None
            )
        if target and not safe_external_media_url(target):
            target = None
    else:
        target = target_for_request(source, handler.path, token)
    if not target:
        handler.send_error(404, "media session expired or invalid")
        return

    parsed = urllib.parse.urlsplit(target)
    origin = (parsed.scheme.lower(), parsed.netloc.lower())
    # Provider cookies, authorization and Referer never leave the source host.
    # Registered cross-CDN links get only a noncredential user agent and Range.
    forwarded = (
        {name: value for name, value in headers.items() if name.lower() == "user-agent"}
        if is_external else dict(headers)
    )
    is_hls_manifest = urllib.parse.urlsplit(target).path.lower().endswith(".m3u8")
    requested_range = handler.headers.get("Range", "")
    if not is_hls_manifest and re.fullmatch(r"bytes=\d+-\d*", requested_range):
        forwarded["Range"] = requested_range

    req = urllib.request.Request(target, headers=forwarded, method=handler.command)
    opener = urllib.request.build_opener(SingleOriginRedirect(origin))
    try:
        response = opener.open(req, timeout=16)
    except urllib.error.HTTPError as e:
        # Minimal status mapping, without reflecting upstream body or credentials.
        handler.send_response(e.code if 400 <= e.code <= 599 else 502)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    except (TimeoutError, OSError, ValueError):
        handler.send_error(502, "media origin unavailable")
        return

    with response:
        status = getattr(response, "status", 200)
        manifest_body = None
        initial_body = b""
        # Buffer playlists only up to a hard cap; larger bodies pass through
        # unchanged. Rewritten manifest sizes must not use upstream byte ranges.
        if is_hls_manifest and handler.command == "GET" and status == 200:
            initial_body = response.read(HLS_MANIFEST_MAX_BYTES + 1)
            if len(initial_body) <= HLS_MANIFEST_MAX_BYTES and (
                initial_body.lstrip().removeprefix(bytes((239, 187, 191))).startswith(b"#EXTM3U")
            ):
                manifest_body = rewrite_hls_manifest(
                    initial_body, target, token,
                    original_media_url=source,
                    register_cross_origin=lambda uri: register_external_media_reference(
                        bridge, uri, token
                    ),
                )
        handler.send_response(status)
        response_type = response.headers.get("Content-Type", "application/octet-stream")
        if urllib.parse.urlsplit(target).path.lower().endswith(".mpd"):
            response_type = "application/dash+xml"
        elif is_hls_manifest:
            response_type = "application/vnd.apple.mpegurl"
        handler.send_header("Content-Type", response_type)
        for name in SAFE_RESPONSE_HEADERS:
            if manifest_body is not None and name in ("Content-Length", "Content-Range", "Accept-Ranges"):
                continue
            value = response.headers.get(name)
            if value and not any(ch in value for ch in (chr(13), chr(10))):
                handler.send_header(name, value)
        if manifest_body is not None:
            handler.send_header("Content-Length", str(len(manifest_body)))
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        if handler.command == "HEAD":
            return
        try:
            if manifest_body is not None:
                handler.wfile.write(manifest_body)
            else:
                if initial_body:
                    handler.wfile.write(initial_body)
                while True:
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
