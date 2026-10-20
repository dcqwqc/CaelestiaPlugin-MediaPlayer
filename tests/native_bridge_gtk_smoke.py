#!/usr/bin/env python3
"""Actual GTK factory/session-readiness integration on a private display.

This exercises the production adapter in a small GTK harness; it is not the
complete upstream Showtime window or its D-Bus/MPRIS interface.
"""
import importlib.util
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import gi
for name, version in [('Gtk','4.0'),('Adw','1'),('GstPlay','1.0')]: gi.require_version(name, version)
from gi.repository import Gio, GLib, GObject, Gtk
import showtime_libmpv_gtk as native
from mpv import MPV

spec = importlib.util.spec_from_file_location('native_smoke_bridge', SCRIPTS / 'showtime-mpv-shim.py')
shim = importlib.util.module_from_spec(spec); spec.loader.exec_module(shim)
os.environ['SHOWTIME_BACKEND'] = 'libmpv'
uri = Path(sys.argv[1]).resolve().as_uri()
bridge = shim.Bridge({'url': uri})
bridge.broadcast = lambda _event: None
reports, states = [], []
loop = GLib.MainLoop()
app = Gtk.Application(application_id='org.example.ShowtimeBackendTest', flags=Gio.ApplicationFlags.NON_UNIQUE)
app.register(None)
class Harness(Gtk.ApplicationWindow):
    __gsignals__ = {'seeked': (GObject.SignalFlags.RUN_LAST, None, ())}
window = Harness(application=app, default_width=640, default_height=360)
window.seek_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
window._media_session = bridge.session()
window._media_stream_ready = False
picture = Gtk.Picture()
offload = Gtk.GraphicsOffload()
offload.set_child(picture)
window.set_child(offload)

def report(_window, endpoint, reason=None):
    reports.append((endpoint, reason))
    body = {'media_revision': window._media_session['media_revision'], 'reason': reason}
    (bridge.report_eof if endpoint == 'playback-eof' else bridge.report_playback_error)(body)

def progress(_window):
    if not window._media_stream_ready: return
    bridge.update_progress(dict(media_revision=window._media_session['media_revision'],
        state=window._decoder_state, position_ns=window._decoder_position,
        duration_ns=window._decoder_duration, video_streams=window._decoder_video_streams,
        audio_streams=window._decoder_audio_streams, backend='libmpv',
        rendered_frames=window._decoder_rendered_frames, audio_ready=window._decoder_audio_ready,
        seekable=window._decoder_seekable, pause_revision=bridge.pause_revision,
        seek_revision=bridge.seek_revision))

factory_module = SimpleNamespace()
native.install(factory_module, report, progress, True)
real_factory = native.MPV
def factory(**kwargs):
    kwargs['ao'] = 'null'
    return real_factory(**kwargs)
native.MPV = factory
window.paintable, window.play, window.pipeline, window.sink = factory_module.gst_play_setup(picture)
native.bind_window(window)
signals = factory_module.Messenger(window.play, window.pipeline)
signals.connect('media-info-updated', lambda *_: setattr(window, '_media_stream_ready', True))
signals.connect('state-changed', lambda _s, state: states.append(state.value_nick))
window.present()
window.play.props.uri = uri
window.play.play()
start = time.monotonic()
stage = [0]
verified = [False]

def tick():
    if reports: raise AssertionError(reports)
    if bridge.decoder_ready and stage[0] == 0:
        assert bridge.properties['vo-configured']
        assert window._decoder_audio_streams == window._decoder_video_streams == 1
        assert bridge.properties['user-data/kunai-readiness'] == 'rendered-frame-progress'
        window.play.pause()
        stage[0] = 1
    elif stage[0] == 1 and bridge.paused:
        bridge.set_media(uri)
        window._media_session = bridge.session()
        window.play.props.uri = uri
        window.play.play()
        stage[0] = 2
    elif stage[0] == 2 and bridge.decoder_ready:
        assert window.play.generation == 2
        assert 'playing' in states and 'paused' in states
        window.destroy()
        assert window.play.closed
        verified[0] = True
        # Allow the asynchronous destruction callback to finish on GTK.
        GLib.timeout_add(200, lambda: (loop.quit(), False)[1])
        print('PASS native GTK bridge: real adapter signals, rendered readiness, pause, source revision, window cleanup')
        return False
    if time.monotonic() - start > 15:
        window.destroy(); loop.quit(); return False
    return True
GLib.timeout_add(100, tick)
loop.run()
assert verified[0]
