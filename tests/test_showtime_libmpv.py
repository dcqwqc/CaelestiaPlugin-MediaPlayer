"""Native controller tests with deterministic event-thread/main-thread boundaries."""
import importlib.util
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from showtime_libmpv import Player, MediaInfo, nanoseconds, validated_headers
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
    p = Player(Core, queue.append, lambda *args: events.append(args), attached.append, retired.append)
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
