"""GTK4 render API and original Showtime 50 UI integration (no video window)."""
from __future__ import annotations

import ctypes
import threading
from types import SimpleNamespace

from gi.repository import Adw, Gdk, GLib, GObject, GstPlay, Gtk
from OpenGL import GL
from mpv import MPV, MpvGlGetProcAddressFn, MpvRenderContext

from showtime_libmpv import Player


class ProcAddress:
    """Use the API owning GTK's current context; don't require both GL stacks."""
    def __init__(self, display):
        wayland = 'Wayland' in type(display).__name__
        self.libraries = []
        # X11 GTK can create an EGL context too. Resolve against the actual
        # current GL context, rather than assuming X11 always means GLX.
        for api, library, current, lookup in (
            ('egl', 'libEGL.so.1', 'eglGetCurrentContext', 'eglGetProcAddress'),
            ('glx', 'libGLX.so.0', 'glXGetCurrentContext', 'glXGetProcAddressARB'),
        ):
            if wayland and api != 'egl': continue
            try: lib = ctypes.CDLL(library)
            except OSError: continue
            self.libraries.append(lib)
            active = getattr(lib, current)
            active.argtypes = []
            active.restype = ctypes.c_void_p
            if not active(): continue
            self.api = api
            self.lookup = getattr(lib, lookup)
            self.lookup.argtypes = [ctypes.c_char_p]
            self.lookup.restype = ctypes.c_void_p
            self.callback = MpvGlGetProcAddressFn(lambda _ctx, name: self.lookup(name))
            break
        else:
            raise RuntimeError('No current EGL/GLX context for libmpv')



class Surface(Gtk.GLArea):
    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.set_auto_render(False)
        self.set_required_version(3, 2)
        self.set_use_es(False)
        self.core = self.context = self.proc = None
        self.frames = 0
        self.pending = False
        self.lock = threading.Lock()
        self.failure = lambda: None
        self.connect('realize', self._realize)
        self.connect('render', self._render)
        self.connect('unrealize', self._unrealize)

    def attach(self, core):
        self._free_context()
        self.core = core
        self.frames = 0
        if core and self.get_realized(): self._realize(self)

    def _realize(self, _area):
        self.make_current()
        if self.get_error():
            self.failure()
            return
        if not self.core or self.context: return
        try:
            self.proc = ProcAddress(self.get_display())
            self.context = MpvRenderContext(self.core, 'opengl', opengl_init_params={
                'get_proc_address': self.proc.callback})
            self.context.update_cb = self._request
            self.queue_render()
        except Exception:
            self.failure()

    def _request(self):
        # The render callback runs on an mpv thread. Coalesce rather than queue
        # an unbounded idle per decoded frame when the window is hidden.
        with self.lock:
            if self.pending: return
            self.pending = True
        def draw():
            with self.lock: self.pending = False
            if self.context and self.get_realized(): self.queue_render()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(draw)

    def _render(self, _area, _context):
        if not self.context: return False
        scale = self.get_scale_factor()
        width, height = self.get_width() * scale, self.get_height() * scale
        if width <= 0 or height <= 0: return False
        try:
            self.context.update()
            self.context.render(flip_y=True, opengl_fbo={
                'w': width, 'h': height,
                'fbo': int(GL.glGetIntegerv(GL.GL_DRAW_FRAMEBUFFER_BINDING))})
            self.frames += 1
            return True
        except Exception:
            self.failure()
            return False

    def _free_context(self):
        if self.context:
            self.make_current()
            self.context.update_cb = None
            self.context.free()
            self.context = None
            self.proc = None

    def _unrealize(self, _area):
        self._free_context()
        # An unrealize can be temporary; Window removal owns core destruction.


class Paintable(GObject.Object):
    __gsignals__ = {'invalidate-size': (GObject.SignalFlags.RUN_LAST, None, ())}
    orientation = GObject.Property(type=int, default=0)
    def __init__(self, player):
        super().__init__()
        self.player = player
        self.width = self.height = 0
        self.connect('notify::orientation', self._rotate)
    def _rotate(self, *_args):
        if self.player.core:
            self.player.core.video_rotate = {0: 0, 1: 0, 2: 90, 3: 180, 4: 270}.get(self.orientation, 0)
    def get_intrinsic_width(self): return self.width
    def get_intrinsic_height(self): return self.height


class Signals(GObject.Object):
    __gsignals__ = {
        name: (GObject.SignalFlags.RUN_LAST, None, args)
        for name, args in {
            'state-changed': (object,), 'duration-changed': (object,),
            'position-updated': (object,), 'seek-done': (),
            'media-info-updated': (object,), 'volume-changed': (),
            'end-of-stream': (), 'error': (object,), 'warning': (object,),
            'missing-plugin': (object,),
        }.items()
    }


def install(window_module, report, progress, session_active):
    """Replace the upstream factory BEFORE any Window creates a decoder."""
    def setup(picture):
        area = Surface()
        signals = Signals()
        owner = [None]
        last_state = [None]
        last_duration = [None]

        def dispatch(callback):
            def run():
                try: callback()
                except Exception: receive('error', 'playback-failed')
                return GLib.SOURCE_REMOVE
            GLib.idle_add(run)

        def receive(kind, value):
            window = owner[0] or getattr(player, 'window', None) or area.get_root() or picture.get_root()
            if window is None: return
            owner[0] = window
            if kind == 'loading':
                window._media_stream_ready = False
                window._decoder_state = 'loading'
                window._decoder_position = window._decoder_duration = 0
                window._decoder_video_streams = window._decoder_audio_streams = 0
                window._decoder_seekable = False
                last_state[0] = last_duration[0] = None
                signals.emit('state-changed', GstPlay.PlayState.BUFFERING)
            elif kind == 'sample':
                window._decoder_backend = 'libmpv'
                window._decoder_rendered_frames = area.frames
                window._decoder_audio_ready = bool(player.core.audio_params)
                title = getattr(window, '_session_display_title', None)
                if title: value.info.title = title
                window.seek_scale.set_sensitive(value.info.is_seekable() and value.duration > 0)
                window._decoder_position, window._decoder_duration = value.position, value.duration
                window._decoder_state = value.state
                window._decoder_video_streams = value.info.get_number_of_video_streams()
                window._decoder_audio_streams = value.info.get_number_of_audio_streams()
                window._decoder_seekable = value.info.is_seekable()
                window._decoder_pause_revision = (getattr(window, '_media_session', {}) or {}).get('pause_revision', 0)
                if last_duration[0] != value.duration:
                    last_duration[0] = value.duration
                    signals.emit('duration-changed', value.duration)
                if last_state[0] != value.state:
                    last_state[0] = value.state
                    signals.emit('state-changed', getattr(GstPlay.PlayState, value.state.upper()))
                # Upstream divides position by duration. Live streams can have
                # no duration; keep the UI alive without inventing a timeline.
                if value.duration > 0:
                    signals.emit('position-updated', value.position)
                if not value.seeking: progress(window)
            elif kind == 'loaded':
                if not session_active:
                    previous = window._get_previous_play_position()
                    if previous and previous > 60_000_000_000 and previous < player.duration - 1_000_000_000:
                        player.seek(previous)
            elif kind == 'media-info':
                signals.emit('media-info-updated', value)
                video = player.core.video_params or {}
                width, height = int(video.get('dw', 0)), int(video.get('dh', 0))
                if (width, height) != (paintable.width, paintable.height):
                    paintable.width, paintable.height = width, height
                    paintable.emit('invalidate-size')
            elif kind == 'subtitle-error':
                window.toast_overlay.add_toast(Adw.Toast.new('Could not load subtitles'))
            elif kind == 'volume': signals.emit('volume-changed')
            elif kind == 'seek-done':
                window._initial_resume_pending = False
                if player.duration: signals.emit('seek-done')
                window.emit('seeked')
            elif kind == 'stopped':
                window._media_stream_ready = False
                window._decoder_state = 'stopped'
                signals.emit('state-changed', GstPlay.PlayState.STOPPED)
            elif kind in ('eof', 'error'):
                window._decoder_state = 'failed' if kind == 'error' else 'stopped'
                report(window, 'playback-error' if kind == 'error' else 'playback-eof',
                       value if kind == 'error' else None)
                if kind == 'eof': signals.emit('end-of-stream')
                else: signals.emit('error', GLib.Error('Native playback failed'))

        def retire(core, done):
            # terminate joins python-mpv's event thread; don't block GTK while
            # a network demuxer tears down. GL is already detached on GTK.
            def terminate():
                try: core.terminate()
                except Exception:
                    dispatch(lambda: receive('error', 'teardown-failed'))
                    return
                dispatch(done)
            threading.Thread(target=terminate, daemon=True).start()

        player = Player(MPV, dispatch, receive, area.attach, retire)
        player.native_signals = signals
        player.surface = area
        paintable = Paintable(player)
        def render_failed():
            if not player.terminal and not player.closed:
                player.terminal = True
                receive('error', 'render-failed')
        area.failure = render_failed
        parent = picture.get_parent()
        if not isinstance(parent, Gtk.GraphicsOffload):
            raise RuntimeError('Unsupported Showtime 50 video hierarchy')
        # Keep picture in its original hierarchy until session overlays have
        # been installed. Replace only the video child on the next main turn.
        def mount():
            parent.set_child(area)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(mount)
        sink = SimpleNamespace(props=SimpleNamespace(window_width=0, window_height=0))
        return paintable, player, player, sink

    window_module.gst_play_setup = setup
    window_module.Messenger = lambda play, _pipeline: play.native_signals

    def open_video(window, gfile):
        app = window.get_application()
        if not app: return
        if not session_active: app.save_play_position(window)
        window._playing_gfile = gfile
        window.media_info_updated = False
        window.stack.props.visible_child = window.video_page
        window.placeholder_stack.props.visible_child = window.error_status_page
        window.play.props.uri = gfile.get_uri()
        if not session_active: window.unpause()
        window._on_motion()
        # Screenshot helper snapshots Gtk.Picture rather than the GLArea.
        # Do not claim it captures native frames until implemented/validated.
        action = window.lookup_action('screenshot')
        if action: action.set_enabled(False)
    return open_video


def bind_window(window):
    player = window.play
    player.window = window
    source = [None]
    def sample():
        if player.closed: return GLib.SOURCE_REMOVE
        try: player.sample()
        except Exception:
            player.emit('error', 'playback-failed')
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE
    source[0] = GLib.timeout_add(250, sample)
    def removed(_app, removed_window):
        if removed_window is not window: return
        if source[0]:
            GLib.source_remove(source[0])
            source[0] = None
        player.close()
        app.disconnect(handler)
    app = window.get_application()
    handler = app.connect('window-removed', removed)
