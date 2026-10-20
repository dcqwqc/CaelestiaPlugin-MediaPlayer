#!/usr/bin/env python3
"""Opt-in real GTK/libmpv exercise; run under a private display, never a live seat.

Usage: xvfb-run -a python tests/native_gtk_smoke.py /path/to/generated-av.mp4
Audio output uses mpv's null sink for a headless host; audio decoding is verified.
"""
import sys
import os
import subprocess
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gi
gi.require_version('Gtk', '4.0'); gi.require_version('GstPlay', '1.0'); gi.require_version('Adw', '1')
from gi.repository import GLib, Gtk
from mpv import MPV
from showtime_libmpv import Player
from showtime_libmpv_gtk import Surface

loop = GLib.MainLoop()
window = Gtk.Window(default_width=640, default_height=360)
area = Surface()
window.set_child(area)
events, failures, retired = [], [], []
area.failure = lambda: failures.append('render-failed')
def dispatch(callback):
    GLib.idle_add(lambda: (callback(), False)[1])
def factory(**kwargs):
    kwargs['ao'] = 'null'
    return MPV(**kwargs)
# Smoke intentionally terminates synchronously so process completion proves
# cores were released; production keeps teardown off the GTK thread.
def retire(core, done):
    core.terminate()
    retired.append(core)
    done()
p = Player(factory, dispatch, lambda *args: events.append(args), area.attach, retire)
window.present()
p.props.uri = Path(sys.argv[1]).resolve().as_uri()
p.play()
start = time.monotonic()
stage = [0]
positions = []
verified_frames = [0]
verified_api = [None]

def tick():
    p.sample()
    elapsed = time.monotonic() - start
    if p.loaded:
        positions.append(p.position)
        if stage[0] == 0 and p.position > 500_000_000:
            assert p.info.get_number_of_audio_streams() == 1
            assert p.info.get_number_of_video_streams() == 1
            p.pause(); assert p.core.pause
            p.props.volume = .37; p.props.mute = True; p.props.rate = 1.25
            assert abs(p.core.volume - 37) < .1 and p.core.mute and p.core.speed == 1.25
            p.seek(1_500_000_000)
            p.play()
            stage[0] = 1
        elif stage[0] == 1 and any(k == 'seek-done' for k, _ in events):
            p.props.uri = Path(sys.argv[1]).resolve().as_uri()
            p.play()
            stage[0] = 2
        elif stage[0] == 2 and p.position > 600_000_000:
            assert area.frames > 5
            assert p.generation == 2
            verified_frames[0] = area.frames
            verified_api[0] = area.proc.api
            screenshot = os.environ.get('SHOWTIME_SMOKE_SCREENSHOT')
            if screenshot:
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'x11grab',
                                '-video_size', '640x360', '-i', os.environ['DISPLAY'],
                                '-frames:v', '1', '-y', screenshot], check=True, timeout=10)
            p.close()
            window.destroy()
            loop.quit()
            print(f'PASS native GTK: {verified_frames[0]} frames ({verified_api[0]}), audio+video, controls, seek, source replacement, cleanup')
            return False
    if elapsed > 15 or failures or any(k == 'error' for k, _ in events):
        print('FAIL native GTK', failures, [k for k, _ in events])
        p.close()
        loop.quit()
        return False
    return True
GLib.timeout_add(100, tick)
loop.run()
assert stage[0] == 2 and verified_frames[0] > 5 and not failures
