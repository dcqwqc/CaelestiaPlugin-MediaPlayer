#!/usr/bin/env python3
"""Opt-in native HTTP, credential-isolated subtitles, EOF and denial regression.

Uses a generated local AV file and a private loopback server. No GUI/input/audio
hardware, provider credentials or external network access are involved.
"""
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from mpv import MPV
from showtime_libmpv import Player

media = Path(sys.argv[1]).read_bytes()
requests = []
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_GET(self):
        requests.append((self.path, self.headers.get('Cookie'), self.headers.get('Authorization')))
        if self.path == '/subtitle':
            assert self.headers.get('Cookie') is None
            assert self.headers.get('Authorization') is None
            body = b'WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nNative subtitle\n'
            self.send_response(200)
        elif self.headers.get('Cookie') == 'one=two; three=four' and self.headers.get('Authorization') == 'Bearer one,two':
            span = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers.get('Range', ''))
            if span:
                start = int(span[1]); end = min(len(media) - 1, int(span[2]) if span[2] else len(media) - 1)
                body = media[start:end + 1]
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{end}/{len(media)}')
            else:
                body = media
                self.send_response(200)
            self.send_header('Accept-Ranges', 'bytes')
        else:
            body = b'denied'
            self.send_response(403)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{server.server_port}'
pending = queue.SimpleQueue()
events, retired = [], []
def factory(**kwargs):
    kwargs.update(vo='null', ao='null')
    return MPV(**kwargs)
def retire(core, done):
    core.terminate()
    retired.append(core)
    done()
p = Player(factory, pending.put, lambda *args: events.append(args), lambda _core: None, retire)
def pump(until, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while not pending.empty(): pending.get()()
        p.sample()
        if until(): return
        time.sleep(.05)
    raise AssertionError('Native event deadline exceeded: ' + repr([k for k, _ in events]))
try:
    p.headers = {'Cookie': 'one=two; three=four', 'Authorization': 'Bearer one,two'}
    p.props.uri = base + '/media'
    p.play()
    pump(lambda: p.loaded)
    assert p.info.get_number_of_audio_streams() == p.info.get_number_of_video_streams() == 1
    p.fetch_subtitle(base + '/subtitle')
    pump(lambda: bool(p.info and p.info.get_subtitle_streams()))
    assert ('/subtitle', None, None) in requests
    p.props.rate = 3
    pump(lambda: any(k == 'eof' for k, _ in events))
    assert sum(k == 'eof' for k, _ in events) == 1
    p.seek(0); p.play()
    pump(lambda: not p.eof and p.position > 100_000_000)
    p.headers = {}
    p.props.uri = base + '/media'
    pump(lambda: any(k == 'error' for k, _ in events))
    errors = [v for k, v in events if k == 'error']
    assert errors == ['access-denied'], errors
    assert requests[-1][1:] == (None, None)
    print('PASS native HTTP: auth headers with commas, subtitle isolation, EOF/replay, header reset, 403 classification')
finally:
    p.close()
    server.shutdown(); server.server_close()
