"""Native controller tests with deterministic event-thread/main-thread boundaries."""
import importlib.util
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from showtime_libmpv import Player, MediaInfo, nanoseconds, validated_headers, subtitle_suffix, subtitle_request
from showtime_host import verify_runtime, host_command


class Core:
    def __init__(self, **options):
        self.options = options
        self.__dict__.update(options)
        self.commands = []
        self.time_pos, self.duration = 2, 100
        self.track_list = []
        self.media_title, self.seekable = 'Movie', True
        self.paused_for_cache = self.seeking = self.eof_reached = False
    def register_event_callback(self, callback): self.callback = callback
    def play(self, uri): self.commands.append(('play', uri))
    def stop(self): self.commands.append(('stop',))
    def seek(self, value, **kwargs): self.commands.append(('seek', value, kwargs))
    def command(self, *args): self.commands.append(args)
    def event(self, kind, reason=0):
        self.callback(SimpleNamespace(event_id=SimpleNamespace(value=kind), data=SimpleNamespace(reason=reason)))


@pytest.fixture
def player():
    queue, events, attached, retired = [], [], [], []
    def retire(core, done):
        retired.append(core)
        done()
    p = Player(Core, queue.append, lambda *args: events.append(args), attached.append, retire)
    p.queue, p.events, p.attached, p.retired = queue, events, attached, retired
    def drain():
        while queue: queue.pop(0)()
    p.drain = drain
    return p


def load(p):
    p.props.uri = 'https://cdn.invalid/video?secret=token'
    p.core.event(8)
    p.drain()
    return p.core


def test_audio_enabled_and_no_separate_window(player):
    c = load(player)
    assert c.options['vo'] == 'libmpv'
    assert 'audio' not in c.options and 'ao' not in c.options
    assert not c.options['config'] and not c.options['osc']
    assert player.props.position == 2_000_000_000
    assert player.props.duration == 100_000_000_000


def test_new_core_rejects_already_queued_old_events_and_same_url(player):
    old = load(player)
    old.event(7, 4)
    old.event(8)
    player.props.uri = player.uri
    new = player.core
    player.drain()
    assert not player.loaded and not player.terminal
    assert player.position == player.duration == 0
    assert player.retired == [old]
    assert player.attached[-2:] == [None, new]
    new.event(8)
    player.drain()
    assert player.loaded
    assert not any(kind == 'error' for kind, _ in player.events)


def test_headers_with_commas_and_replacement_are_scoped_to_core(player):
    player.headers = {'Cookie': 'a=b; c=d', 'User-Agent': ' ', 'Authorization': 'Bearer one,two'}
    first = load(player)
    assert first.http_header_fields == ['Cookie: a=b; c=d', 'User-Agent:  ', 'Authorization: Bearer one,two']
    player.headers = {}
    player.props.uri = 'https://different.invalid/movie'
    assert player.core.http_header_fields == []


@pytest.mark.parametrize('headers', [{'Host': 'evil'}, {'Cookie': 'x\r\nHost: evil'}, {'Bad:Name': 'x'}, {'Cookie': '\0'}])
def test_rejects_header_injection(headers):
    with pytest.raises(ValueError): validated_headers(headers)


def test_pause_volume_mute_speed_and_seek(player):
    c = load(player)
    player.play(); assert c.pause is False
    player.pause(); assert c.pause is True
    player.props.volume = .31; assert c.volume == 31
    player.props.mute = True; assert c.mute
    player.props.rate = 1.5; assert c.speed == 1.5
    player.seek(0)
    assert c.commands[-1] == ('seek', 0, {'reference': 'absolute+exact'})
    assert player.seek_pending
    c.event(21); player.drain()
    assert not player.seek_pending
    assert ('seek-done', None) in player.events


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1])
def test_invalid_seek_rejected(player, value):
    load(player)
    with pytest.raises(ValueError): player.seek(value)


def test_track_menu_indices_map_to_native_ids(player):
    c = load(player)
    c.track_list = [{'type': 'audio', 'id': 7, 'lang': 'de', 'selected': True},
                    {'type': 'sub', 'id': 23, 'lang': 'en', 'title': 'English'},
                    {'type': 'video', 'id': 42}]
    player.sample()
    player.set_audio_track(0); assert c.aid == 7
    assert player.get_current_audio_track().get_language() == 'de'
    player.set_subtitle_track(0); assert c.sid == 23
    player.set_subtitle_track_enabled(False); assert c.sid == 'no'
    player.set_subtitle_track_enabled(True); assert c.sid == 23
    assert player.info.get_subtitle_streams()[0].get_tags().get_string('title') == (True, 'English')
    assert player.info.get_number_of_video_streams() == 1


def test_stale_or_invalid_subtitle_actions_never_stop_video(player):
    c = load(player)
    c.track_list = [{'type': 'audio', 'id': 1}, {'type': 'sub', 'id': 17, 'lang': 'en'}]
    player.sample()
    assert player.set_subtitle_track(0) is True
    assert c.sid == 17
    assert player.set_subtitle_track(4) is False
    assert player.set_subtitle_track(-1) is False
    assert player.set_subtitle_track_enabled(False) is True
    assert player.set_subtitle_track_enabled(True) is True
    # A different episode may have cleared all native track entries while
    # the GTK menu is still displayed.
    player.info = MediaInfo(player.uri, [], 'New episode')
    assert player.set_subtitle_track(0) is False
    assert player.set_audio_track(0) is False
    assert not player.terminal
    assert not any(k == 'error' for k, _ in player.events)


def test_anime_webvtt_uses_matching_file_type_not_generic_sub():
    assert subtitle_suffix(b'WEBVTT\n\n00:00:00.1 --> 00:00:01.2\nHi', 'https://test/sub') == '.vtt'
    assert subtitle_suffix(b'\xef\xbb\xbfWEBVTT\n', 'https://test/a.sub') == '.vtt'
    assert subtitle_suffix(b'1\n00:00:00,100 --> 00:00:01,000\nHi') == '.srt'
    assert subtitle_suffix(b'[Script Info]\nTitle: Anime') == '.ass'
    assert subtitle_suffix(b'unrecognized', 'https://test/a.m3u8') == '.txt'


def test_external_english_webvtt_attached_without_media_credentials(monkeypatch, player):
    import threading
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, _limit):
            return b'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nEnglish\n'
    seen = []
    completed = threading.Event()
    def fetch(request, timeout):
        seen.append((request.full_url, dict(request.header_items())))
        completed.set()
        return FakeResponse()
    monkeypatch.setattr('showtime_libmpv.urllib.request.urlopen', fetch)
    c = load(player)
    player.headers = {
        'Authorization': 'Bearer private-media-token',
        'Cookie': 'session=private',
        'Referer': 'https://embed.example/watch',
        'User-Agent': 'Showtime-Test-UA',
    }
    player.fetch_subtitle('https://subs.example/english.vtt')
    assert completed.wait(3)
    for _ in range(50):
        player.drain()
        if player.subtitle: break
        import time
        time.sleep(.01)
    assert player.subtitle is not None and player.subtitle.endswith('.vtt')
    assert Path(player.subtitle).read_bytes().startswith(b'WEBVTT')
    assert ('sub-add', player.subtitle, 'select') in c.commands
    assert seen[0][0] == 'https://subs.example/english.vtt'
    sent_headers = {k.lower(): v for k, v in seen[0][1].items()}
    assert sent_headers['referer'] == 'https://embed.example/watch'
    assert sent_headers['user-agent'] == 'Showtime-Test-UA'
    assert 'cookie' not in sent_headers and 'authorization' not in sent_headers
    assert not player.terminal
    assert not any(k == 'error' for k, _ in player.events)
    player.close()


def test_subtitle_http_failure_is_nonfatal(monkeypatch, player):
    import urllib.error
    import threading
    did_fetch = threading.Event()
    def deny(request, timeout):
        did_fetch.set()
        raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden', None, None)
    monkeypatch.setattr('showtime_libmpv.urllib.request.urlopen', deny)
    c = load(player)
    player.fetch_subtitle('https://cdn.example/subtitles.vtt')
    assert did_fetch.wait(3)
    for _ in range(40):
        player.drain()
        if any(kind == 'subtitle-error' for kind, _ in player.events):
            break
        import time
        time.sleep(.01)
    assert ('subtitle-error', None) in player.events
    assert not player.terminal
    assert not any(kind == 'error' for kind, _ in player.events)
    assert not any(command[0] == 'stop' for command in c.commands)
    player.close()


def test_subtitle_request_filters_credentials_and_invalid_urls():
    req = subtitle_request('https://subs.example/en.vtt', {
        'Cookie': 'session=private',
        'Authorization': 'Bearer private',
        'Referer': 'https://embed.example/watch',
        'User-Agent': 'UA',
        'Origin': 'https://hidden.example',
    })
    headers = {name.lower(): value for name, value in req.header_items()}
    assert headers == {'referer': 'https://embed.example/watch', 'user-agent': 'UA'}
    for url in ('file:///etc/passwd', 'javascript:alert(1)', 'https://u:p@subs.example/file.vtt'):
        with pytest.raises(ValueError):
            subtitle_request(url, {})
    assert 'referer' not in {
        k.lower(): v for k, v in subtitle_request('https://subs.example/en.vtt',
            {'Referer': 'https://x.example/watch\r\nCookie: evil'}).header_items()
    }


def test_external_subtitle_change_removes_old_external_tracks(player):
    c = load(player)
    c.track_list = [{'type': 'sub', 'id': 17, 'external': True}]
    player.sample()
    player.props.suburi = 'file:///tmp/test.srt'
    assert ('sub-remove', 17) in c.commands
    assert c.commands[-1] == ('sub-add', 'file:///tmp/test.srt', 'select')
    player.props.suburi = None
    assert player.subtitle is None


def test_stop_and_close_discard_events_and_release_render_first(player):
    c = load(player)
    c.event(7, 0)
    player.stop()
    player.drain()
    assert not any(kind == 'eof' for kind, _ in player.events)
    player.close(); player.close()
    assert player.attached[-1] is None
    assert player.retired == [c]
    c.event(8); player.drain()
    assert player.core is None


def test_eof_error_and_denial_are_deduplicated_and_sanitized(player):
    c = load(player)
    c.options['log_handler']('error', 'ffmpeg', 'HTTP error 403 https://signed/?token=secret Cookie: secret')
    c.event(7, 4); c.event(7, 4); player.drain()
    player.pending_error = (0, player.pending_error[1])
    player.sample()
    assert [v for k, v in player.events if k == 'error'] == ['access-denied']
    assert 'secret' not in repr(player.events)


def test_buffering_audio_only_and_unknown_duration(player):
    c = load(player)
    c.duration = None
    c.track_list = [{'type': 'audio', 'id': 1}]
    c.paused_for_cache = True
    player.sample()
    sample = [v for k, v in player.events if k == 'sample'][-1]
    assert sample.duration == 0 and sample.state == 'buffering'
    assert sample.info.get_number_of_video_streams() == 0
    assert sample.info.get_number_of_audio_streams() == 1


def test_time_conversion_is_finite():
    for value in (None, -1, float('nan'), float('inf'), 'bad'):
        assert nanoseconds(value) == 0


def runtime(tmp_path):
    import hashlib, json
    for relative in ('share/showtime/showtime.gresource', 'share/showtime/showtime/__init__.py',
                     'python/mpv.py', 'schemas/gschemas.compiled'):
        p = tmp_path / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('fixture')
    files = {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in tmp_path.rglob('*') if p.is_file()}
    (tmp_path / 'manifest.json').write_text(json.dumps({'format': 1, 'showtime_version': '50.0', 'files': files}))
    return tmp_path


def test_host_launch_integrity_and_secret_free_argv(tmp_path):
    root = runtime(tmp_path)
    cmd, env = host_command('/repo/showtime-player.py', 'http://127.0.0.1:1234/api/session', 'secret',
                            {'SHOWTIME_HOST_RUNTIME': str(root), 'PYTHONPATH': '/untrusted'})
    assert 'secret' not in repr(cmd)
    assert '/untrusted' not in env['PYTHONPATH']
    assert env['MEDIA_PLAYER_SESSION_TOKEN'] == 'secret'
    assert env['SHOWTIME_BACKEND'] == 'libmpv'
    (root / 'python/mpv.py').write_text('changed')
    with pytest.raises(RuntimeError): verify_runtime(root)


def test_runtime_rejects_unlisted_modules(tmp_path):
    root = runtime(tmp_path)
    (root / 'python/sitecustomize.py').write_text('bad')
    with pytest.raises(RuntimeError): verify_runtime(root)


def test_native_session_bypasses_gstreamer_relays(monkeypatch, tmp_path):
    monkeypatch.setenv('SHOWTIME_BACKEND', 'libmpv')
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    spec = importlib.util.spec_from_file_location('native_shim', SCRIPTS / 'showtime-mpv-shim.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    bridge = m.Bridge({'url': 'https://cdn.invalid/a.m3u8', 'http_headers': ['Cookie: a=b']})
    bridge.media_proxy_port = 9876
    assert bridge.session()['media_url'] == bridge.url
    assert bridge.session()['http_headers'] == {'Cookie': 'a=b'}
    bridge.set_media('https://other.invalid/a.mpd')
    assert bridge.session()['http_headers'] == {}


def test_keep_open_eof_can_replay_without_duplicate_eof(player):
    c = load(player)
    c.eof_reached = True
    player.sample(); player.sample()
    assert [k for k, _ in player.events].count('eof') == 1
    player.seek(0)
    assert not player.terminal
    c.eof_reached = False
    c.event(21); player.drain()
    player.play(); player.sample()
    assert not player.terminal


def test_readiness_requires_native_frames_or_audio_output(monkeypatch, tmp_path):
    monkeypatch.setenv('SHOWTIME_BACKEND', 'libmpv')
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    spec = importlib.util.spec_from_file_location('native_readiness', SCRIPTS / 'showtime-mpv-shim.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    bridge = m.Bridge({'url': 'https://cdn.invalid/movie'})
    clock = [100]
    monkeypatch.setattr(m.time, 'monotonic', lambda: clock[0])
    def sample(pos, **values):
        body = dict(media_revision=1, position_ns=pos * 1e9, duration_ns=100e9,
                    state='playing', video_streams=1, backend='libmpv')
        body.update(values)
        assert bridge.update_progress(body)
        clock[0] += 1
    sample(1); sample(2)
    assert not bridge.decoder_ready
    sample(3, rendered_frames=5)
    assert bridge.decoder_ready
    assert bridge.properties['vo-configured']
    assert bridge.properties['user-data/kunai-readiness'] == 'rendered-frame-progress'
    bridge.set_media(bridge.url)
    for pos in (1, 2):
        assert bridge.update_progress(dict(media_revision=2, position_ns=pos * 1e9,
                    state='playing', video_streams=0, audio_streams=1,
                    backend='libmpv', audio_ready=True))
        clock[0] += 1
    assert bridge.decoder_ready
    assert not bridge.properties['vo-configured']
    assert bridge.properties['user-data/kunai-readiness'] == 'audio-progress'


def test_rapid_replacement_waits_for_teardown_and_starts_only_latest():
    completions, retired, cores = [], [], []
    def factory(**kwargs):
        core = Core(**kwargs); cores.append(core); return core
    def retire(core, done):
        retired.append(core); completions.append(done)
    p = Player(factory, lambda cb: cb(), lambda *_: None, lambda *_: None, retire)
    p.load('file:///first.mp4')
    p.load('file:///second.mp4')
    p.load('file:///third.mp4')
    assert p.core is None and len(cores) == 1
    assert p.retiring
    completions.pop(0)()
    assert len(cores) == 2
    assert p.core.commands == [('play', 'file:///third.mp4')]
    p.close(); completions.pop(0)()
    assert p.core is None


def test_close_during_teardown_cancels_pending_source():
    completions, cores = [], []
    def factory(**kwargs):
        core = Core(**kwargs); cores.append(core); return core
    p = Player(factory, lambda cb: cb(), lambda *_: None, lambda *_: None,
               lambda core, done: completions.append(done))
    p.load('file:///one.mp4'); p.load('file:///two.mp4')
    p.close(); completions.pop(0)()
    assert len(cores) == 1


def test_clear_uri_retires_core(player):
    c = load(player)
    player.props.uri = None
    assert player.core is None and player.retired == [c]


def test_constructor_failure_is_sanitized_and_terminal():
    def fail(**kwargs): raise RuntimeError('signed URL and secret header')
    events = []
    p = Player(fail, lambda cb: cb(), lambda *args: events.append(args), lambda *_: None,
               lambda core, done: done())
    p.load('https://signed.invalid?token=secret')
    assert p.terminal and p.core is None
    assert events[-1] == ('error', 'playback-failed')
    assert 'secret' not in repr(events)
    p.close()


def test_preflight_rejects_missing_host_abi(monkeypatch):
    import showtime_host
    monkeypatch.setattr(showtime_host.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match='GTK 4.18'):
        showtime_host.preflight(['/usr/bin/python3'], {})


def test_runtime_rejects_injected_bytecode(tmp_path):
    root = runtime(tmp_path)
    cached = root / 'python/__pycache__/mpv.cpython-314.pyc'
    cached.parent.mkdir()
    cached.write_bytes(b'not signed')
    with pytest.raises(RuntimeError): verify_runtime(root)


def test_runtime_rejects_symlinked_files(tmp_path):
    root = runtime(tmp_path / 'runtime')
    external = tmp_path / 'external.py'
    external.write_text('fixture')
    module = root / 'python/mpv.py'
    module.unlink(); module.symlink_to(external)
    with pytest.raises(RuntimeError): verify_runtime(root)


def test_authenticated_session_requests_never_follow_redirects(monkeypatch):
    import ast
    import json
    import os
    import threading
    import urllib.request
    import urllib.error
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    visits = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            visits.append(self.path)
            if self.path == '/api/session':
                assert self.headers['X-Showtime-Session-Token'] == 'test-secret'
                self.send_response(302)
                self.send_header('Location', '/leak')
                self.end_headers()
            else:
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setenv('MEDIA_PLAYER_SESSION_TOKEN', 'test-secret')
    tree = __import__('ast').parse((SCRIPTS / 'showtime-player.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_http_json')
    env = dict(urllib=urllib, os=os, json=json, SESSION_BASE=base)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'session-request', 'exec'), env)
    try:
        with pytest.raises(urllib.error.HTTPError): env['_http_json'](base + '/api/session')
        assert visits == ['/api/session']
    finally:
        server.shutdown(); server.server_close(); thread.join()
