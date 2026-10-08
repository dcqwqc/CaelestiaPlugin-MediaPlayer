"""Local, origin-locked media relay for Showtime adaptive playback.

GStreamer's adaptivedemux2 uses an independent libsoup session to fetch DASH/HLS
fragments. Passing headers to souphttpsrc only authenticates the manifest, not
its fragments. Relaying adaptive URLs via the already-loopback IPC server keeps
provider credentials on the source origin and avoids modifying GStreamer.
"""
from __future__ import annotations

import ipaddress
import re
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus


SAFE_RESPONSE_HEADERS = ("Content-Length", "Content-Range", "Accept-Ranges", "Cache-Control")
ADAPTIVE_SUFFIXES = (".mpd", ".m3u8")
MEDIA_PREFIX = "/media/"


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
    target = target_for_request(source, handler.path, token)
    if not target:
        handler.send_error(404, "media session expired or invalid")
        return

    parsed = urllib.parse.urlsplit(source)
    origin = (parsed.scheme.lower(), parsed.netloc.lower())
    # Caller-controlled Host/Connection and forwarded client cookies are never
    # consulted. Only headers that Kunai resolved for the selected source pass.
    forwarded = dict(headers)
    requested_range = handler.headers.get("Range", "")
    if re.fullmatch(r"bytes=\d+-\d*", requested_range):
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
        handler.send_response(status)
        response_type = response.headers.get("Content-Type", "application/octet-stream")
        if urllib.parse.urlsplit(target).path.lower().endswith(".mpd"):
            response_type = "application/dash+xml"
        elif urllib.parse.urlsplit(target).path.lower().endswith(".m3u8"):
            response_type = "application/vnd.apple.mpegurl"
        handler.send_header("Content-Type", response_type)
        for name in SAFE_RESPONSE_HEADERS:
            value = response.headers.get(name)
            if value and not any(ch in value for ch in ("\r", "\n")):
                handler.send_header(name, value)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        if handler.command == "HEAD":
            return
        try:
            while True:
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
