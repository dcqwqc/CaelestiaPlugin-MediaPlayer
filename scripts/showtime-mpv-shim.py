#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHOWTIME_BOOTSTRAP = ROOT / "showtime-player.py"
REAL_MPV = Path("/usr/bin/mpv")


def json_line(payload):
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def parse_args(argv):
    if any(x in argv for x in ("--version", "-V", "--help", "-h", "--list-options")):
        return {"passthrough": True}
    state = {
        "passthrough": False,
        "ipc": None,
        "url": None,
        "title": "Video",
        "subtitle": None,
        "referrer": "",
        "start": 0.0,
        "chapters_file": None,
        "persistent": False,
    }
    for arg in argv:
        if arg.startswith("--input-ipc-server="):
            state["ipc"] = arg.split("=", 1)[1]
        elif arg.startswith("--force-media-title="):
            state["title"] = arg.split("=", 1)[1]
        elif arg.startswith("--sub-file=") and not state["subtitle"]:
            state["subtitle"] = arg.split("=", 1)[1]
        elif arg.startswith("--referrer="):
            state["referrer"] = arg.split("=", 1)[1]
        elif arg.startswith("--start="):
            try:
                state["start"] = float(arg.split("=", 1)[1])
            except ValueError:
                pass
        elif arg.startswith("--chapters-file="):
            state["chapters_file"] = arg.split("=", 1)[1]
        elif arg == "--idle=yes":
            state["persistent"] = True
        elif arg == "--":
            continue
        elif not arg.startswith("-"):
            state["url"] = arg
    return state


def parse_ffmeta(path):
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    out = []
    current = None
    try:
        for raw in p.read_text(errors="replace").splitlines():
            line = raw.strip()
            if line == "[CHAPTER]":
                if current and "start" in current and "end" in current:
                    out.append(current)
                current = {}
            elif current is not None and "=" in line:
                k, v = line.split("=", 1)
                if k == "START":
                    current["start"] = float(v) / 1000
                elif k == "END":
                    current["end"] = float(v) / 1000
                elif k.lower() == "title":
                    label = v
                    current["label"] = label
                    lv = label.lower()
                    current["type"] = "op" if "intro" in lv else "ed" if "credit" in lv else "recap" if "recap" in lv else "preview" if "preview" in lv else "chapter"
        if current and "start" in current and "end" in current:
            out.append(current)
    except Exception:
        return []
    return [x for x in out if x.get("type") in ("op", "ed", "recap", "preview")]


class Bridge:
    def __init__(self, cfg):
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.clients = set()
        self.closed = False
        self.revision = 1
        self.media_revision = 1
        self.seek_revision = 0
        self.url = cfg.get("url") or ""
        self.title = cfg.get("title") or "Video"
        self.subtitle = cfg.get("subtitle")
        self.referrer = cfg.get("referrer") or ""
        self.resume_us = int(float(cfg.get("start") or 0) * 1_000_000)
        self.chapters = parse_ffmeta(cfg.get("chapters_file"))
        self.position = float(cfg.get("start") or 0)
        self.duration = 0.0
        self.paused = False
        self.can_next = bool(cfg.get("persistent"))
        self.can_prev = bool(cfg.get("persistent"))
        self.next_label = "Next Episode"
        self.prev_label = "Previous Episode"
        self.properties = {
            "time-pos": self.position,
            "playback-time": self.position,
            "duration": self.duration,
            "percent-pos": 0.0,
            "pause": False,
            "seeking": False,
            "paused-for-cache": False,
            "cache-buffering-state": 100.0,
            "demuxer-cache-duration": 30.0,
            "demuxer-via-network": self.url.startswith(("http://", "https://")),
            "cache-speed": 0.0,
            "vo-configured": True,
            "eof-reached": False,
            "idle-active": False,
            "core-idle": False,
            "filename": self.url,
            "media-title": self.title,
            "track-list": [],
            "user-data/kunai-request": "",
            "user-data/kunai-track-changed": "",
        }
        self.subtitles = []
        if self.subtitle:
            self.add_subtitle(self.subtitle, "Subtitle", "", selected=True)

    def add_client(self, sock):
        with self.lock:
            self.clients.add(sock)

    def remove_client(self, sock):
        with self.lock:
            self.clients.discard(sock)

    def send_to(self, sock, payload):
        try:
            sock.sendall(json_line(payload))
        except OSError:
            self.remove_client(sock)

    def broadcast(self, payload):
        data = json_line(payload)
        with self.lock:
            clients = list(self.clients)
        for sock in clients:
            try:
                sock.sendall(data)
            except OSError:
                self.remove_client(sock)

    def prop(self, name, value):
        with self.lock:
            self.properties[name] = value
        self.broadcast({"event": "property-change", "name": name, "data": value})

    def add_subtitle(self, url, label="", language="", selected=False):
        with self.lock:
            existing = next((x for x in self.subtitles if x["url"] == url), None)
            if existing:
                if selected:
                    self.subtitle = url
                    self.selected_subtitle_id = existing["id"]
                return existing
            idx = len(self.subtitles) + 1
            item = {
                "id": f"mpv-{idx}",
                "url": url,
                "label": label or language or f"Subtitle {idx}",
                "language": language or "",
                "source": "original",
                "available": True,
            }
            self.subtitles.append(item)
            if selected or not getattr(self, "selected_subtitle_id", None):
                self.subtitle = url
                self.selected_subtitle_id = item["id"]
            self.properties["track-list"] = [
                {"id": i + 1, "type": "sub", "title": s["label"], "lang": s["language"], "external": True, "selected": s["id"] == getattr(self, "selected_subtitle_id", "")}
                for i, s in enumerate(self.subtitles)
            ]
            return item

    def session(self):
        with self.lock:
            return {
                "ok": True,
                "media_url": self.url,
                "anime_title": self.title,
                "episode": "",
                "previous_episode": self.prev_label if self.can_prev else None,
                "next_episode": self.next_label if self.can_next else None,
                "subtitle_url": self.subtitle,
                "study_url": None,
                "selected_subtitle_id": getattr(self, "selected_subtitle_id", "off"),
                "subtitles": list(self.subtitles),
                "chapters": list(self.chapters),
                "resume_us": self.resume_us,
                "revision": self.revision,
                "seek_us": int(self.position * 1_000_000),
                "seek_revision": self.seek_revision,
            }

    def update_progress(self, body):
        try:
            pos = max(0.0, int(body.get("position_ns", 0)) / 1_000_000_000)
            dur = max(0.0, int(body.get("duration_ns", 0)) / 1_000_000_000)
        except Exception:
            return
        with self.lock:
            self.position = pos
            self.duration = dur
            self.properties["time-pos"] = pos
            self.properties["playback-time"] = pos
            self.properties["duration"] = dur
            self.properties["percent-pos"] = (pos / dur * 100.0) if dur > 0 else 0.0
        self.prop("time-pos", pos)
        self.prop("playback-time", pos)
        self.prop("duration", dur)
        if dur > 0:
            self.prop("percent-pos", pos / dur * 100.0)

    def set_media(self, url, options=None):
        options = options or {}
        with self.cond:
            self.url = str(url)
            self.position = float(options.get("start") or 0)
            self.resume_us = int(self.position * 1_000_000)
            if options.get("chapters-file"):
                self.chapters = parse_ffmeta(options.get("chapters-file"))
            self.revision += 1
            self.media_revision += 1
            self.properties["filename"] = self.url
            self.properties["time-pos"] = self.position
            self.properties["playback-time"] = self.position
            self.properties["eof-reached"] = False
            self.cond.notify_all()
        self.prop("filename", self.url)
        self.prop("media-title", self.title)
        self.broadcast({"event": "file-loaded"})
        self.prop("time-pos", self.position)
        self.prop("playback-time", self.position)

    def request_navigation(self, direction):
        with self.cond:
            before = self.media_revision
        req = "previous" if direction.startswith("prev") else "next"
        self.prop("user-data/kunai-request", req)
        # Kunai's mpv Lua bridge normally stops the current file itself after
        # publishing this request. Showtime replaces that Lua surface, so emit
        # the same end-file signal here and let Kunai resolve the adjacent episode.
        self.broadcast({"event": "end-file", "reason": "stop"})
        deadline = time.monotonic() + 48
        with self.cond:
            while self.media_revision <= before and not self.closed and time.monotonic() < deadline:
                self.cond.wait(timeout=0.25)
            return self.media_revision > before

    def handle_command(self, command):
        if not isinstance(command, list) or not command:
            return None
        op = str(command[0])
        if op == "observe_property":
            name = str(command[2]) if len(command) > 2 else ""
            value = self.properties.get(name)
            return ("observe", name, value)
        if op == "get_property":
            name = str(command[1]) if len(command) > 1 else ""
            return self.properties.get(name)
        if op == "set_property":
            name = str(command[1]) if len(command) > 1 else ""
            value = command[2] if len(command) > 2 else None
            if name == "pause":
                self.paused = bool(value)
                self.prop("pause", self.paused)
            elif name == "force-media-title":
                self.title = str(value or "Video")
                self.prop("media-title", self.title)
                with self.lock:
                    self.revision += 1
            elif name == "chapters-file":
                self.chapters = parse_ffmeta(str(value or ""))
                with self.lock:
                    self.revision += 1
            elif name == "user-data/kunai-can-next":
                self.can_next = bool(value)
            elif name == "user-data/kunai-can-previous":
                self.can_prev = bool(value)
            elif name == "user-data/kunai-next-label":
                self.next_label = str(value or "Next Episode")
            elif name == "user-data/kunai-previous-label":
                self.prev_label = str(value or "Previous Episode")
            self.prop(name, value)
            return None
        if op == "loadfile" and len(command) > 1:
            options = command[4] if len(command) > 4 and isinstance(command[4], dict) else {}
            self.set_media(command[1], options)
            return None
        if op == "seek" and len(command) > 1:
            try:
                pos = float(command[1])
            except Exception:
                pos = self.position
            with self.lock:
                self.position = pos
                self.seek_revision += 1
                self.revision += 1
            self.prop("time-pos", pos)
            self.prop("playback-time", pos)
            return None
        if op == "sub-add" and len(command) > 1:
            url = str(command[1])
            label = str(command[3]) if len(command) > 3 else ""
            lang = str(command[4]) if len(command) > 4 else ""
            selected = len(command) > 2 and str(command[2]) == "select"
            item = self.add_subtitle(url, label, lang, selected)
            with self.lock:
                self.revision += 1
            self.prop("track-list", self.properties["track-list"])
            return item.get("id")
        if op == "sub-remove":
            return None
        if op == "sub-reload":
            return None
        if op == "stop":
            self.broadcast({"event": "end-file", "reason": "stop"})
            return None
        if op == "quit":
            self.closed = True
            self.broadcast({"event": "end-file", "reason": "quit"})
            return "quit"
        if op == "show-text":
            return None
        return None


class IPCHandler(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        self.server.bridge.add_client(self.request)

    def finish(self):
        self.server.bridge.remove_client(self.request)
        super().finish()

    def handle(self):
        b = self.server.bridge
        for raw in self.rfile:
            try:
                msg = json.loads(raw)
                command = msg.get("command")
                request_id = msg.get("request_id")
                result = b.handle_command(command)
                if request_id is not None:
                    b.send_to(self.request, {"request_id": request_id, "error": "success", "data": None if isinstance(result, tuple) else result})
                if isinstance(result, tuple) and result and result[0] == "observe":
                    _, name, value = result
                    b.send_to(self.request, {"event": "property-change", "name": name, "data": value})
                if result == "quit":
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
            except Exception:
                continue


class IPCServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, path, bridge):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.bridge = bridge
        super().__init__(path, IPCHandler)


class HTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def bridge(self):
        return self.server.bridge

    def read_json(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/session":
            self.send_json(self.bridge.session())
            return
        self.send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/progress":
            self.bridge.update_progress(self.read_json())
            self.send_json({"ok": True})
            return
        if u.path == "/api/navigate":
            direction = (urllib.parse.parse_qs(u.query).get("direction") or ["next"])[0]
            if not self.bridge.request_navigation(direction):
                self.send_json({"ok": False, "error": "Kunai did not resolve the adjacent episode"}, 504)
                return
            self.send_json(self.bridge.session())
            return
        if u.path == "/api/subtitles/select":
            body = self.read_json()
            track_id = str(body.get("id") or "off")
            with self.bridge.lock:
                if track_id == "off":
                    self.bridge.subtitle = None
                    self.bridge.selected_subtitle_id = "off"
                else:
                    track = next((x for x in self.bridge.subtitles if x["id"] == track_id), None)
                    if track:
                        self.bridge.subtitle = track["url"]
                        self.bridge.selected_subtitle_id = track_id
                self.bridge.revision += 1
            self.send_json(self.bridge.session())
            return
        self.send_json({"ok": False, "error": "not found"}, 404)


class HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, addr, bridge):
        self.bridge = bridge
        super().__init__(addr, HTTPHandler)


def launch_showtime(bridge, http_port):
    env = os.environ.copy()
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    cmd = [
        "flatpak", "run",
        "--share=network",
        "--filesystem=host:ro",
        f"--filesystem={ROOT}:ro",
        "--command=python3",
        f"--env=MEDIA_PLAYER_SESSION_URL=http://127.0.0.1:{http_port}/api/session",
    ]
    if bridge.subtitle:
        cmd.append(f"--env=MEDIA_PLAYER_SUBTITLE_URI={bridge.subtitle}")
    cmd += ["org.gnome.Showtime", str(SHOWTIME_BOOTSTRAP), "--new-window", bridge.url]
    return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv):
    cfg = parse_args(argv)
    if cfg.get("passthrough"):
        return subprocess.call([str(REAL_MPV), *argv])
    if not cfg.get("url"):
        # Non-playback mpv invocation: retain compatibility for tooling.
        return subprocess.call([str(REAL_MPV), *argv])

    bridge = Bridge(cfg)
    httpd = HTTPServer(("127.0.0.1", 0), bridge)
    http_port = httpd.server_address[1]
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()

    ipcd = None
    ipc_thread = None
    ipc_path = cfg.get("ipc")
    if ipc_path:
        Path(ipc_path).parent.mkdir(parents=True, exist_ok=True)
        ipcd = IPCServer(ipc_path, bridge)
        ipc_thread = threading.Thread(target=ipcd.serve_forever, daemon=True)
        ipc_thread.start()

    proc = launch_showtime(bridge, http_port)
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        code = proc.wait()
    finally:
        bridge.closed = True
        bridge.broadcast({"event": "end-file", "reason": "quit"})
        with bridge.cond:
            bridge.cond.notify_all()
        httpd.shutdown()
        httpd.server_close()
        if ipcd:
            ipcd.shutdown()
            ipcd.server_close()
            try:
                os.unlink(ipc_path)
            except FileNotFoundError:
                pass
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
