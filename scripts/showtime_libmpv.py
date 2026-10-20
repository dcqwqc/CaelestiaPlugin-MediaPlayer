"""Engine-independent Showtime 50 control contract for libmpv.

No GI imports: the GTK integration supplies dispatch, rendering and signals.
Every source owns a separate mpv core. Retired cores cannot acknowledge controls
or publish progress for their successors, even when the URL is unchanged.
"""
from __future__ import annotations

import math
import re
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from types import SimpleNamespace

NS = 1_000_000_000
HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
BLOCKED_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "proxy-authorization"}


def validated_headers(headers):
    result = []
    for name, value in (headers or {}).items():
        if (not isinstance(name, str) or not HEADER_NAME.fullmatch(name)
                or name.lower() in BLOCKED_HEADERS or not isinstance(value, str)
                or any(c in value for c in '\r\n\0')):
            raise ValueError('Invalid media request header')
        result.append(f'{name}: {value}')
    return result


def nanoseconds(value):
    try:
        number = float(value or 0)
        return int(number * NS) if math.isfinite(number) and number > 0 else 0
    except (TypeError, ValueError):
        return 0


def subtitle_request(uri, source_headers):
    """Build a minimal subtitle request without disclosing media credentials.

    Subtitle CDNs for anime sources require the embedding page's Referer and
    User-Agent. Never forward Cookie, Authorization or arbitrary media headers.
    """
    parsed = urlsplit(uri)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Invalid subtitle URL')
    allowed = {}
    for key, value in (source_headers or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if not value or len(value) > 2048 or any(ch in value for ch in '\r\n\0'):
            continue
        if key.lower() == 'user-agent':
            allowed['User-Agent'] = value
        elif key.lower() in ('referer', 'referrer'):
            ref = urlsplit(value)
            if ref.scheme in ('https', 'http') and ref.hostname and not ref.username and not ref.password:
                allowed['Referer'] = value
    return urllib.request.Request(uri, headers=allowed)


def subtitle_suffix(payload, uri=''):
    """Preserve subtitle format for mpv's demuxer, rather than using .sub.

    A generic .sub extension causes mpv/FFmpeg to probe an incompatible
    subtitle codec for WebVTT from HLS anime providers.
    """
    lead = bytes(payload[:4096]).lstrip(b'\xef\xbb\xbf\x20\t\r\n')
    if lead.startswith(b'WEBVTT'):
        return '.vtt'
    if lead.startswith((b'[Script Info]', b'[V4+ Styles]', b'[Events]')):
        return '.ass'
    if b'-->' in lead:
        return '.srt'
    suffix = Path(urlsplit(uri).path).suffix.lower()
    return suffix if suffix in {'.vtt', '.srt', '.ass', '.ssa'} else '.txt'


class Track:
    def __init__(self, data, index):
        self.data, self.index = data, index

    def get_index(self): return self.index
    def get_language(self): return self.data.get('lang')
    def get_channels(self): return int(self.data.get('demux-channel-count') or 0)
    def get_codec(self): return self.data.get('codec', '')
    def get_tags(self): return self
    def get_string(self, key):
        value = self.data.get(key)
        return bool(value), value


class MediaInfo:
    def __init__(self, uri, tracks, title='', seekable=False, duration=0):
        self.uri, self.title, self.seekable, self.duration = uri, title, seekable, duration
        self.audio = [Track(t, i) for i, t in enumerate(t for t in tracks if t['type'] == 'audio')]
        self.subs = [Track(t, i) for i, t in enumerate(t for t in tracks if t['type'] == 'sub')]
        self.video = [Track(t, i) for i, t in enumerate(t for t in tracks if t['type'] == 'video')]

    def get_uri(self): return self.uri
    def get_title(self): return self.title
    def get_duration(self): return self.duration
    def is_seekable(self): return self.seekable
    def get_audio_streams(self): return self.audio
    def get_subtitle_streams(self): return self.subs
    def get_video_streams(self): return self.video
    def get_number_of_audio_streams(self): return len(self.audio)
    def get_number_of_subtitle_streams(self): return len(self.subs)
    def get_number_of_video_streams(self): return len(self.video)
    def get_tags(self): return Track({'title': self.title}, 0)


class Properties:
    def __init__(self, owner): self.owner = owner
    @property
    def uri(self): return self.owner.uri
    @uri.setter
    def uri(self, value): self.owner.load(value)
    @property
    def position(self): return self.owner.position
    @property
    def duration(self): return self.owner.duration
    @property
    def volume(self): return self.owner.volume
    @volume.setter
    def volume(self, value): self.owner.set_volume(value)
    @property
    def mute(self): return self.owner.mute
    @mute.setter
    def mute(self, value): self.owner.set_setting('mute', bool(value))
    @property
    def rate(self): return self.owner.speed
    @rate.setter
    def rate(self, value):
        value = float(value)
        if not math.isfinite(value) or not 0.01 <= value <= 100:
            raise ValueError('Invalid playback speed')
        self.owner.set_setting('speed', value)
    @property
    def suburi(self): return self.owner.subtitle
    @suburi.setter
    def suburi(self, value): self.owner.external_subtitle(value)


class Player:
    """Main-thread controller; all mpv callbacks cross an injected dispatcher."""
    def __init__(self, factory, dispatch, emit, attach, retire):
        self.factory, self.dispatch, self.emit = factory, dispatch, emit
        self.attach, self.retire = attach, retire
        self.core = None
        self.retiring = False
        self.pending_start = None
        self.generation = 0
        self.closed = False
        self.uri = None
        self.headers = {}
        self.volume, self.mute, self.speed = 1.0, False, 1.0
        self.volume_reported = False
        self.paused = True
        self.position = self.duration = 0
        self.loaded = False
        self.terminal = False
        self.pending_error = None
        self.last_error_type = None
        self.eof = False
        self.info = None
        self.subtitle = None
        self.selected_subtitle = None
        self.seek_pending = False
        self.subtitle_serial = 0
        self.subtitle_files = tempfile.TemporaryDirectory(prefix="showtime-subtitles-")
        self.props = Properties(self)

    def load(self, uri):
        if self.closed: raise RuntimeError('Player is closed')
        self.generation += 1
        generation = self.generation
        old, self.core = self.core, None
        self.attach(None)  # Release GL render context before destroying its core.
        self.uri = uri
        self.position = self.duration = 0
        self.loaded = self.terminal = self.seek_pending = self.eof = False
        self.info = None
        self.volume_reported = False
        self.pending_error = None
        self.subtitle = None
        self.subtitle_serial += 1
        self.selected_subtitle = None
        self.emit('loading', None)
        if not uri:
            self.pending_start = None
            if old: self._retire(old)
            return
        headers = dict(self.headers)
        def start():
            if self.closed or generation != self.generation: return
            fields = validated_headers(headers)
            denied = [False]
            def log(_level, _component, message):
                # Native diagnostics can contain signed URLs and cookies. Classify
                # in memory only; never send them to Python or terminal logging.
                if re.search(r'\b(?:401|403|429)\b|forbidden|unauthorized', message, re.I):
                    denied[0] = True
            try:
                core = self.factory(vo='libmpv', config=False, input_default_bindings=False,
                                    input_vo_keyboard=False, osc=False, ytdl=False,
                                    idle=True, keep_open=True, pause=self.paused,
                                    volume=self.volume * 100, mute=self.mute, speed=self.speed,
                                    terminal=False, log_handler=log, loglevel='warn')
                self.core = core
                def callback(event):
                    # ctypes event payload is valid only during the callback.
                    kind = event.event_id.value
                    reason = int(event.data.reason) if kind == 7 else None
                    self.dispatch(lambda: self._event(generation, kind, reason, denied))
                core.register_event_callback(callback)
                # A native string-list property preserves commas in Cookie/header values.
                # The fresh core guarantees headers never persist across sources.
                core.http_header_fields = fields
                self.attach(core)
                core.play(uri)
            except Exception as exc:
                self.last_error_type = type(exc).__name__
                failed, self.core = self.core, None
                self.attach(None)
                if failed: self._retire(failed)
                self.terminal = True
                self.emit('error', 'playback-failed')
        self.pending_start = start
        if old:
            self._retire(old)
        elif not self.retiring:
            self._retired()

    def _retire(self, core):
        self.retiring = True
        self.retire(core, self._retired)

    def _retired(self):
        self.retiring = False
        start, self.pending_start = self.pending_start, None
        if start and not self.closed: start()

    def _event(self, generation, kind, reason, denied):
        if self.closed or generation != self.generation: return
        if kind == 8:  # MPV_EVENT_FILE_LOADED
            self.loaded = True
            self.sample()
            self.emit('loaded', None)
            if self.subtitle: self.core.command('sub-add', self.subtitle, 'select')
        elif kind == 21:  # MPV_EVENT_PLAYBACK_RESTART, including completed seek
            self.sample()
            if self.seek_pending:
                self.seek_pending = False
                self.emit('seek-done', None)
        elif kind == 7 and not self.terminal:  # MPV_EVENT_END_FILE
            if reason in (0, 4):
                if reason == 4:
                    # FFmpeg log messages can arrive just after END_FILE. Give
                    # the event thread a bounded drain interval before choosing
                    # Kunai's denial/error class. No raw log text is retained.
                    self.pending_error = (time.monotonic() + .15, denied)
                    self.loaded = False
                else:
                    self.terminal = self.eof = True
                    self.emit('eof', None)

    def sample(self):
        if self.pending_error and not self.closed:
            deadline, denied = self.pending_error
            if time.monotonic() >= deadline:
                self.pending_error = None
                self.terminal = True
                self.emit('error', 'access-denied' if denied[0] else 'playback-failed')
            return
        if not self.core or not self.loaded or self.closed or self.terminal: return
        c = self.core
        self.position, self.duration = nanoseconds(c.time_pos), nanoseconds(c.duration)
        tracks = c.track_list or []
        info = MediaInfo(self.uri, tracks, c.media_title or '', bool(c.seekable), self.duration)
        changed = self.info is None or [t.data for t in self.info.audio + self.info.subs + self.info.video] != [t.data for t in info.audio + info.subs + info.video]
        self.info = info
        self.paused = bool(c.pause)
        state = 'buffering' if c.paused_for_cache else 'paused' if self.paused else 'playing'
        self.emit('sample', SimpleNamespace(position=self.position, duration=self.duration,
                  state=state, info=info, seeking=bool(c.seeking)))
        if changed: self.emit('media-info', info)
        volume = max(0.0, min(1.0, float(c.volume or 0) / 100))
        if not self.volume_reported or volume != self.volume:
            self.volume_reported = True
            self.volume = volume
            self.emit('volume', None)
        if c.eof_reached and not self.seek_pending:
            self.terminal = self.eof = True
            self.emit('eof', None)

    def play(self):
        self.paused = False
        if self.core: self.core.pause = False
    def pause(self):
        self.paused = True
        if self.core: self.core.pause = True
    def stop(self):
        self.generation += 1
        self.loaded = False
        self.pending_error = None
        self.terminal = True
        if self.core: self.core.stop()
        self.emit('stopped', None)
    def seek(self, value):
        value = float(value) / NS
        if not math.isfinite(value) or value < 0: raise ValueError('Invalid seek')
        if self.core and self.loaded:
            if self.eof:
                self.terminal = self.eof = False
            self.seek_pending = True
            self.core.seek(value, reference='absolute+exact')
    def set_setting(self, name, value):
        setattr(self, name, value)
        if self.core: setattr(self.core, name, value)
    def set_volume(self, *args):
        value = float(args[-1])  # GstAudio cubic format argument is UI-only.
        if not math.isfinite(value): raise ValueError('Invalid volume')
        self.volume = max(0.0, min(1.0, value))
        if self.core: self.core.volume = self.volume * 100
        self.emit('volume', None)
    def get_volume(self, *_args): return self.volume
    def get_position(self): return self.position
    def get_duration(self): return self.duration
    def get_uri(self): return self.uri
    def get_media_info(self): return self.info
    def get_message_bus(self): return None
    def get_bus(self): return None
    def set_audio_track(self, index):
        if not self.core or not self.info or not isinstance(index, int):
            return False
        if not 0 <= index < len(self.info.audio):
            return False
        self.core.aid = self.info.audio[index].data['id']
        return True
    def get_current_audio_track(self):
        return next((t for t in self.info.audio if t.data.get('selected')), None) if self.info else None
    def set_subtitle_track(self, index):
        # Menus can outlive a source revision for a few GTK frames.
        # Do not let stale actions index outside the new track list or
        # turn an unrelated healthy video into a fatal playback error.
        if not self.core or not self.info or not isinstance(index, int):
            return False
        if not 0 <= index < len(self.info.subs):
            return False
        native_id = self.info.subs[index].data.get('id')
        if native_id is None:
            return False
        try:
            self.core.sid = native_id
        except Exception:
            self.emit('subtitle-error', None)
            return False
        self.selected_subtitle = native_id
        return True
    def set_subtitle_track_enabled(self, enabled):
        if not self.core:
            return False
        try:
            self.core.sid = (self.selected_subtitle if self.selected_subtitle is not None else 'auto') if enabled else 'no'
        except Exception:
            self.emit('subtitle-error', None)
            return False
        return True
    def external_subtitle(self, uri):
        if uri == self.subtitle: return
        if self.core and self.info:
            for track in self.info.subs:
                if track.data.get('external'): self.core.command('sub-remove', track.data['id'])
        self.subtitle = uri
        if self.core and self.loaded and uri: self.core.command('sub-add', uri, 'select')
    def fetch_subtitle(self, uri):
        """Fetch subtitle with source Referer/UA, never with media credentials.

        Decode/render from a private local file, not a remote mpv URL. The
        source's auth cookies/tokens stay with the media request only.
        """
        self.subtitle_serial += 1
        serial, generation = self.subtitle_serial, self.generation
        self.external_subtitle(None)
        if not uri: return
        source_headers = dict(self.headers)
        def worker():
            try:
                request = subtitle_request(uri, source_headers)
                with urllib.request.urlopen(request, timeout=8) as response:
                    data = response.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise ValueError('Subtitle is too large')
                def apply():
                    if self.closed or generation != self.generation or serial != self.subtitle_serial:
                        return
                    suffix = subtitle_suffix(data, uri)
                    path = Path(self.subtitle_files.name) / f'{generation}-{serial}{suffix}'
                    try:
                        path.write_bytes(data)
                        self.external_subtitle(str(path))
                    except Exception:
                        # Invalid VTT, a withdrawn track, or a native demuxer
                        # error should never terminate the video itself.
                        self.emit('subtitle-error', None)
                self.dispatch(apply)
            except Exception:
                # A subtitle error must not terminate otherwise healthy video.
                self.dispatch(lambda: self.emit('subtitle-error', None)
                              if not self.closed and generation == self.generation
                              and serial == self.subtitle_serial else None)
        threading.Thread(target=worker, daemon=True).start()

    def close(self):
        if self.closed: return
        self.closed = True
        self.generation += 1
        self.attach(None)
        self.pending_start = None
        if self.core: self._retire(self.core)
        self.core = None
        self.subtitle_files.cleanup()
