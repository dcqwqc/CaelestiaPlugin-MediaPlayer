#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import secrets
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from showtime_media_proxy import MEDIA_PREFIX, media_proxy_url, relay_request
from showtime_subtitle_preference import (
    should_select_new_track, subtitle_intent, load_preference, save_preference,
)
SHOWTIME_BOOTSTRAP = ROOT / "showtime-player.py"
REAL_MPV = Path("/usr/bin/mpv")


HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
DANGEROUS_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "proxy-authorization"}

def media_header_dict(values):
    """Map provider headers to HTTPS headers with no request smuggling."""
    result = {}
    for item in values:
        if not isinstance(item, str):
            continue
        name, sep, value = item.partition(":")
        name = name.strip()
        # VidRock/ngcorp requires the intentional one-space User-Agent.
        # Stripping it makes urllib add its normal UA, which the CDN rejects.
        value = " " if name.lower() == "user-agent" and value and not value.strip() else value.strip()
        if (sep and HEADER_RE.fullmatch(name) and name.lower() not in DANGEROUS_HEADERS
                and value and not any(c in value for c in (chr(10),chr(13)))):
            result[name] = value
    return result

GENERATED_LANGUAGES = [
    ('en','English'),
    ('de','German'),
    ('ja','Japanese'),
    ('ja-romaji','Japanese Romaji'),
    ('af','Afrikaans'),
    ('ar','Arabic'),
    ('eu','Basque'),
    ('bn','Bengali'),
    ('bg','Bulgarian'),
    ('ca','Catalan'),
    ('zh','Chinese (Simplified)'),
    ('zh-TW','Chinese (Traditional)'),
    ('hr','Croatian'),
    ('cs','Czech'),
    ('da','Danish'),
    ('nl','Dutch'),
    ('et','Estonian'),
    ('fil','Filipino'),
    ('fi','Finnish'),
    ('fr','French'),
    ('gl','Galician'),
    ('el','Greek'),
    ('gu','Gujarati'),
    ('he','Hebrew'),
    ('hi','Hindi'),
    ('hu','Hungarian'),
    ('is','Icelandic'),
    ('id','Indonesian'),
    ('it','Italian'),
    ('kn','Kannada'),
    ('ko','Korean'),
    ('lv','Latvian'),
    ('lt','Lithuanian'),
    ('ms','Malay'),
    ('ml','Malayalam'),
    ('mt','Maltese'),
    ('mr','Marathi'),
    ('no','Norwegian'),
    ('fa','Persian'),
    ('pl','Polish'),
    ('pt','Portuguese'),
    ('pa','Punjabi'),
    ('ro','Romanian'),
    ('ru','Russian'),
    ('sr','Serbian'),
    ('sk','Slovak'),
    ('sl','Slovenian'),
    ('es','Spanish'),
    ('sw','Swahili'),
    ('sv','Swedish'),
    ('ta','Tamil'),
    ('te','Telugu'),
    ('th','Thai'),
    ('tr','Turkish'),
    ('uk','Ukrainian'),
    ('ur','Urdu'),
    ('vi','Vietnamese'),
]


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
        "http_headers": [],
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
        elif arg.startswith("--http-header-fields="):
            raw = arg.split("=", 1)[1]
            state["http_headers"].extend(x.strip() for x in raw.split(",") if x.strip())
        elif arg.startswith("--user-agent="):
            state["http_headers"].append("User-Agent: " + arg.split("=", 1)[1])
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
        self.token = secrets.token_urlsafe(32)
        self.media_token = secrets.token_urlsafe(32)
        self.media_proxy_port = None
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.clients = set()
        self.closed = False
        self.revision = 1
        self.media_revision = 1
        self.failed_media_revision = None
        self.access_denied_urls = set()
        self.seek_revision = 0
        self.url = cfg.get("url") or ""
        self.title = cfg.get("title") or "Video"
        self.subtitle = cfg.get("subtitle")
        self.referrer = cfg.get("referrer") or ""
        self.http_headers = list(cfg.get("http_headers") or [])
        if self.referrer and not any(str(h).lower().startswith(("referer:", "referrer:")) for h in self.http_headers):
            self.http_headers.append("Referer: " + self.referrer)
        self.base_url = ""
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
        self.next_subtitle_mpv_id = 1
        self.subtitle_cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "media-player" / "subtitles"
        self.subtitle_cache.mkdir(parents=True, exist_ok=True)
        self.ai_processes = {}
        self.pending_subtitle_id = None
        self.selected_subtitle_id = "off"
        # Explicit choice survives new track IDs, provider changes and episodes.
        # None means follow provider defaults; ("off", "") is an explicit None.
        self.persist_subtitle_choice = bool(cfg.get("persistent"))
        self.subtitle_preference = (
            load_preference() if self.persist_subtitle_choice else ("original","en")
        )
        if self.subtitle:
            self.add_subtitle(self.subtitle, "Subtitle", "", selected=True)

    def remember_subtitle_preference(self, intent):
        if intent is None:
            return
        self.subtitle_preference=intent
        if self.persist_subtitle_choice:
            save_preference(intent)

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
                if should_select_new_track(self.subtitle_preference, existing, selected):
                    self.subtitle = url
                    self.selected_subtitle_id = existing["id"]
                    for track in self.properties["track-list"]:
                        track["selected"] = track["id"] == existing["mpv_id"]
                return existing
            mpv_id = self.next_subtitle_mpv_id
            self.next_subtitle_mpv_id += 1
            item = {
                "id": f"mpv-{mpv_id}",
                "mpv_id": mpv_id,
                "url": url,
                "label": label or language or f"Subtitle {mpv_id}",
                "language": language or "",
                "source": "original",
                "available": True,
            }
            self.subtitles.append(item)
            choose = should_select_new_track(self.subtitle_preference, item, selected)
            if choose:
                self.subtitle = url
                self.selected_subtitle_id = item["id"]
            self.properties["track-list"] = [
                {
                    "id": track["mpv_id"],
                    "type": "sub",
                    "title": track["label"],
                    "lang": track["language"],
                    "external": True,
                    "selected": track["id"] == getattr(self, "selected_subtitle_id", ""),
                }
                for track in self.subtitles
            ]
            return item

    def media_cache_key(self):
        try:
            parsed = urllib.parse.urlsplit(self.url)
            stable_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except Exception:
            stable_url = self.url
        logical = f"{self.title}:{stable_url}"
        return hashlib.sha256(logical.encode()).hexdigest()[:24]

    def generated_path(self, lang):
        return self.subtitle_cache / self.media_cache_key() / f"generated-{lang}.vtt"

    def generated_status(self, lang):
        path = self.generated_path(lang)
        if path.exists():
            return {"status": "ready", "error": None}
        status_path = path.with_name(f"generated-{lang}.status.json")
        try:
            value = json.loads(status_path.read_text())
            return value if isinstance(value, dict) else {"status": "idle", "error": None}
        except Exception:
            return {"status": "idle", "error": None}

    def subtitle_catalog(self):
        tracks = [dict(item) for item in self.subtitles]
        for lang, label in GENERATED_LANGUAGES:
            path = self.generated_path(lang)
            state = self.generated_status(lang)
            tracks.append({
                "id": f"generated-{lang}",
                "source": "generated",
                "label": f"AI Generated {label}",
                "language": label,
                "available": path.exists(),
                "status": state.get("status", "idle"),
                "error": state.get("error"),
                "url": None,
            })
        return tracks

    def ai_environment(self):
        env = os.environ.copy()
        driver_root = Path.home() / ".local/share/media-player/intel-gpu-runtime/root"
        driver_lib = driver_root / "usr/lib"
        driver_ocl = driver_lib / "intel-opencl"
        icd_dir = Path.home() / ".local/share/media-player/intel-gpu-runtime/icd"
        intel_icd = icd_dir / "intel.icd"
        if (driver_ocl / "libigdrcl.so").exists() and intel_icd.exists():
            old_ld = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = f"{driver_lib}:{driver_ocl}" + (f":{old_ld}" if old_ld else "")
            env["OCL_ICD_VENDORS"] = str(icd_dir)
        env["MEDIA_AI_ASR_QUALITY"] = "quality"
        env.setdefault("MEDIA_AI_OPENVINO_DEVICE", "GPU")
        env.setdefault("MEDIA_AI_TIMING_MODE", "fast")
        return env

    def start_generation(self, track_id):
        if not track_id.startswith("generated-"):
            raise RuntimeError("not an AI subtitle track")
        lang = track_id.removeprefix("generated-")
        if lang not in {code for code, _ in GENERATED_LANGUAGES}:
            raise RuntimeError("unsupported generated subtitle language")
        path = self.generated_path(lang)
        if path.exists():
            with self.lock:
                self.remember_subtitle_preference(("generated", lang))
                self.selected_subtitle_id = track_id
                self.subtitle = None
                self.pending_subtitle_id = None
                self.revision += 1
            return self.session()

        key = (self.media_cache_key(), lang)
        with self.lock:
            self.remember_subtitle_preference(("generated", lang))
            running = self.ai_processes.get(key)
            if running is not None and running.poll() is None:
                self.pending_subtitle_id = track_id
                return self.session()

        env_python = Path.home() / ".local/share/media-player/ai-env/bin/python"
        if not env_python.exists():
            raise RuntimeError("AI subtitle environment is not installed yet")
        path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(env_python), str(ROOT / "ai-subtitles.py"),
            "--media-url", self.url,
            "--output-dir", str(path.parent),
            "--target", lang,
        ]
        for header in self.http_headers:
            cmd.extend(["--http-header", str(header)])
        logdir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "showtime-mpv"
        logdir.mkdir(parents=True, exist_ok=True)
        log = (logdir / f"ai-{self.media_cache_key()}-{lang}.log").open("ab")
        proc = subprocess.Popen(
            cmd,
            env=self.ai_environment(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        with self.lock:
            self.ai_processes[key] = proc
            self.pending_subtitle_id = track_id
            self.revision += 1

        def monitor():
            code = proc.wait()
            log.close()
            with self.lock:
                self.ai_processes.pop(key, None)
                if code == 0 and path.exists() and self.media_cache_key() == key[0]:
                    self.selected_subtitle_id = track_id
                    self.subtitle = None
                if self.pending_subtitle_id == track_id:
                    self.pending_subtitle_id = None
                self.revision += 1

        threading.Thread(target=monitor, daemon=True).start()
        return self.session()

    def generated_bytes(self, track_id):
        if not track_id.startswith("generated-"):
            return None
        path = self.generated_path(track_id.removeprefix("generated-"))
        return path.read_bytes() if path.exists() else None

    def select_subtitle(self, track_id):
        if track_id == "off":
            with self.lock:
                self.remember_subtitle_preference(("off", ""))
                self.subtitle = None
                self.selected_subtitle_id = "off"
                self.pending_subtitle_id = None
                self.revision += 1
            return self.session()
        if track_id.startswith("generated-"):
            path = self.generated_path(track_id.removeprefix("generated-"))
            if not path.exists():
                raise RuntimeError("generated subtitle is not ready")
            with self.lock:
                self.remember_subtitle_preference(("generated", track_id.removeprefix("generated-")))
                self.subtitle = None
                self.selected_subtitle_id = track_id
                self.pending_subtitle_id = None
                self.revision += 1
            return self.session()
        track = next((item for item in self.subtitles if item["id"] == track_id), None)
        if not track:
            raise RuntimeError("subtitle track not found")
        with self.lock:
            self.remember_subtitle_preference(subtitle_intent(track))
            self.subtitle = track["url"]
            self.selected_subtitle_id = track_id
            self.pending_subtitle_id = None
            self.revision += 1
        return self.session()

    def header_dict(self):
        with self.lock:
            return media_header_dict(self.http_headers)

    def session(self):
        with self.lock:
            selected = getattr(self, "selected_subtitle_id", "off")
            subtitle_url = self.subtitle
            if selected == "off":
                subtitle_url = None
            elif selected.startswith("generated-") and self.base_url:
                if self.generated_bytes(selected) is not None:
                    subtitle_url = (
                        f"{self.base_url}/api/subtitles/{urllib.parse.quote(selected)}.vtt"
                        f"?token={urllib.parse.quote(self.token)}"
                    )
            return {
                "ok": True,
                "media_url": (
                    media_proxy_url(self.url, self.media_proxy_port, self.media_token)
                    if self.http_headers else None
                ) or (
                    Path(self.url).resolve().as_uri() if self.url.startswith("/") else self.url
                ),
                "http_headers": (
                    {} if self.http_headers and media_proxy_url(
                        self.url, self.media_proxy_port, self.media_token
                    ) else media_header_dict(self.http_headers)
                ),
                "anime_title": self.title,
                "episode": "",
                "previous_episode": self.prev_label if self.can_prev else None,
                "next_episode": self.next_label if self.can_next else None,
                "subtitle_url": subtitle_url,
                "study_url": None,
                "selected_subtitle_id": selected,
                "subtitles": self.subtitle_catalog(),
                "chapters": list(self.chapters),
                "resume_us": self.resume_us,
                "revision": self.revision,
                "media_revision": self.media_revision,
                "subtitle_preference": (
                    list(self.subtitle_preference) if self.subtitle_preference else None
                ),
                "seek_us": int(self.position * 1_000_000),
                "seek_revision": self.seek_revision,
                "pending_subtitle_id": self.pending_subtitle_id,
            }

    def update_progress(self, body):
        with self.lock:
            if body.get("media_revision") is not None:
                try:
                    if int(body["media_revision"]) != self.media_revision:
                        return
                except (TypeError, ValueError):
                    return
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
        new_headers = []
        referrer = str(options.get("referrer") or "").strip()
        raw_user_agent = str(options.get("user-agent") or "")
        user_agent = " " if raw_user_agent and not raw_user_agent.strip() else raw_user_agent.strip()
        header_fields = str(options.get("http-header-fields") or "").strip()
        if referrer:
            self.referrer = referrer
            new_headers.append("Referer: " + referrer)
        elif self.referrer:
            new_headers.append("Referer: " + self.referrer)
        if user_agent:
            new_headers.append("User-Agent: " + user_agent)
        if header_fields:
            new_headers.extend(x.strip() for x in header_fields.split(",") if x.strip())
        self.referrer = referrer
        self.http_headers = new_headers
        with self.cond:
            if url in self.access_denied_urls:
                self.broadcast({"event": "end-file", "reason": "error",
                                "file_error": "kunai_access_denied"})
                return False
            self.url = str(url)
            self.media_token = secrets.token_urlsafe(32)
            self.subtitles = []
            self.selected_subtitle_id = "off"
            self.subtitle = None
            self.pending_subtitle_id = None
            self.properties["track-list"] = []
            self.position = float(options.get("start") or 0)
            self.resume_us = int(self.position * 1_000_000)
            if options.get("chapters-file"):
                self.chapters = parse_ffmeta(options.get("chapters-file"))
            self.revision += 1
            self.media_revision += 1
            self.failed_media_revision = None
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

    def report_playback_error(self, body):
        """Forward a current GStreamer failure into the mpv-IPC error stream."""
        try:
            revision = int(body.get("revision") or 0)
            media_revision = int(body.get("media_revision") or 0)
        except (TypeError, ValueError):
            return False
        with self.lock:
            if media_revision and media_revision != self.media_revision:
                return False
            if not media_revision and revision and revision != self.revision:
                return False
            if self.failed_media_revision == self.media_revision:
                return False
            self.failed_media_revision = self.media_revision
            denied = body.get("reason") == "access-denied"
            if denied:
                self.access_denied_urls.add(self.url)
            self.properties["core-idle"] = True
            self.properties["idle-active"] = True
            self.properties["vo-configured"] = False
        self.broadcast({
            "event": "end-file", "reason": "error",
            "file_error": "kunai_access_denied" if denied else "loading_failed",
        })
        return True

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
            elif name == "sid":
                if str(value).lower() in ("no", "none", "off", "0"):
                    self.subtitle = None
                    self.selected_subtitle_id = "off"
                else:
                    try:
                        mpv_id = int(value)
                    except (TypeError, ValueError):
                        mpv_id = -1
                    track = next((item for item in self.subtitles if item.get("mpv_id") == mpv_id), None)
                    if track:
                        self.remember_subtitle_preference(subtitle_intent(track))
                        self.subtitle = track["url"]
                        self.selected_subtitle_id = track["id"]
            elif name == "referrer":
                self.referrer = str(value or "")
                self.http_headers = [h for h in self.http_headers if not str(h).lower().startswith(("referer:", "referrer:"))]
                if self.referrer:
                    self.http_headers.append("Referer: " + self.referrer)
            elif name == "http-header-fields":
                preserved = [h for h in self.http_headers if str(h).lower().startswith(("referer:", "referrer:", "user-agent:"))]
                preserved.extend(x.strip() for x in str(value or "").split(",") if x.strip())
                self.http_headers = preserved
            elif name == "user-agent":
                self.http_headers = [h for h in self.http_headers if not str(h).lower().startswith("user-agent:")]
                if value:
                    self.http_headers.append("User-Agent: " + str(value))
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
            try:
                mpv_id = int(command[1]) if len(command) > 1 else -1
            except (TypeError, ValueError):
                mpv_id = -1
            index = next((i for i, track in enumerate(self.subtitles) if track.get("mpv_id") == mpv_id), -1)
            if index >= 0:
                removed = self.subtitles.pop(index)
                if removed["id"] == self.selected_subtitle_id:
                    self.subtitle = None
                    self.selected_subtitle_id = "off"
                self.properties["track-list"] = [
                    {"id": track["mpv_id"], "type": "sub", "title": track["label"], "lang": track["language"], "external": True, "selected": track["id"] == self.selected_subtitle_id}
                    for track in self.subtitles
                ]
                self.revision += 1
                self.prop("track-list", self.properties["track-list"])
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

    def send_bytes(self, data, content_type="application/octet-stream", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get("X-Showtime-Session-Token") != self.bridge.token:
            self.send_json({"ok": False, "error": "unauthorized"}, 403)
            return False
        return True

    def do_GET(self):
        if self.path.startswith(MEDIA_PREFIX):
            relay_request(self)
            return
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/api/subtitles/") and u.path.endswith(".vtt"):
            if urllib.parse.parse_qs(u.query).get("token") != [self.bridge.token]:
                self.send_json({"ok": False, "error": "unauthorized"}, 403)
                return
        elif not self.authorized():
            return
        if u.path == "/api/session":
            self.send_json(self.bridge.session())
            return
        if u.path.startswith("/api/subtitles/") and u.path.endswith(".vtt"):
            track_id = urllib.parse.unquote(u.path[len("/api/subtitles/"):-4])
            data = self.bridge.generated_bytes(track_id)
            if data is None:
                self.send_json({"ok": False, "error": "subtitle not found"}, 404)
            else:
                self.send_bytes(data, "text/vtt; charset=utf-8")
            return
        self.send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self.authorized():
            return
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/playback-error":
            accepted = self.bridge.report_playback_error(self.read_json())
            self.send_json({"ok": True, "accepted": accepted})
            return
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
            try:
                self.send_json(self.bridge.select_subtitle(track_id))
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if u.path == "/api/subtitles/generate":
            body = self.read_json()
            track_id = str(body.get("id") or "")
            try:
                self.send_json(self.bridge.start_generation(track_id))
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
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
        f"--filesystem={ROOT}:ro",
        "--command=python3",
        f"--env=MEDIA_PLAYER_SESSION_URL=http://127.0.0.1:{http_port}/api/session",
        f"--env=MEDIA_PLAYER_SESSION_TOKEN={bridge.token}",
    ]
    if env.get("KUNAI_SHOWTIME_TRACE")=="1":
        cmd.append("--env=KUNAI_SHOWTIME_TRACE=1")
    # Only grant host files explicitly selected for playback. The subtitle
    # generator runs in the host bridge; the Flatpak has no need for host:ro.
    for item in (bridge.url, bridge.subtitle):
        if not isinstance(item, str) or not item:
            continue
        parsed = urllib.parse.urlsplit(item)
        path = (
            urllib.parse.unquote(parsed.path)
            if parsed.scheme == "file" and not parsed.netloc
            else item if not parsed.scheme and item.startswith("/") else None
        )
        if path:
            media_file = Path(path)
            if media_file.is_file():
                cmd.append(f"--filesystem={media_file.resolve()}:ro")
    if bridge.subtitle:
        cmd.append(f"--env=MEDIA_PLAYER_SUBTITLE_URI={bridge.subtitle}")
    # Showtime retrieves the media URL from authenticated /api/session itself.
    # Keep signed URLs and the loopback media bearer out of process argv.
    cmd += ["org.gnome.Showtime", str(SHOWTIME_BOOTSTRAP), "--new-window"]
    debug_log=env.get("KUNAI_SHOWTIME_DEBUG_LOG")
    if debug_log:
        # Explicit diagnostics only. No default media URLs or credentials in logs.
        log_path=Path(debug_log)
        log_path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(str(log_path),os.O_CREAT | os.O_WRONLY | os.O_APPEND,0o600)
        with os.fdopen(fd,"ab",buffering=0) as log:
            return subprocess.Popen(cmd,env=env,stdout=log,stderr=log)
    return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def is_vixsrc_native_fallback_url(url):
    """Use native mpv only for VixSrc HLS that currently stalls in GstPlay."""
    if not isinstance(url, str):
        return False
    parsed = urllib.parse.urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "vixsrc.to"
        and re.fullmatch(r"/playlist/[0-9]+", parsed.path) is not None
    )


def vixsrc_native_player_command(argv, env=None):
    """Keep GPU-problematic Hyprland playback on the tested Wayland SHM path."""
    environment = env if env is not None else os.environ
    prefix = [str(REAL_MPV), "--no-config"]
    if environment.get("WAYLAND_DISPLAY"):
        prefix += ["--vo=wlshm", "--hwdec=no"]
    return [*prefix, *argv]


def main(argv):
    cfg = parse_args(argv)
    if is_vixsrc_native_fallback_url(cfg.get("url")):
        # Do not touch Showtime for any other host or non-playback mpv command.
        return subprocess.call(vixsrc_native_player_command(argv))
    if cfg.get("passthrough"):
        return subprocess.call([str(REAL_MPV), *argv])
    if not cfg.get("url"):
        # Non-playback mpv invocation: retain compatibility for tooling.
        return subprocess.call([str(REAL_MPV), *argv])

    bridge = Bridge(cfg)
    httpd = HTTPServer(("127.0.0.1", 0), bridge)
    http_port = httpd.server_address[1]
    bridge.base_url = f"http://127.0.0.1:{http_port}"
    bridge.media_proxy_port = http_port
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
        with bridge.lock:
            ai_processes = list(bridge.ai_processes.values())
        for ai_proc in ai_processes:
            if ai_proc.poll() is None:
                ai_proc.terminate()
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
