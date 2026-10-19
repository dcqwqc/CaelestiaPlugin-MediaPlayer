"""Readiness requires current GstPlay state and advancing media, never a URL."""
import ast
import importlib.util
import os
import sys
import threading
import unittest
import urllib.parse
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def player_function(name, env):
    tree = ast.parse((SCRIPTS / 'showtime-player.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SCRIPTS / 'showtime-player.py'), 'exec'), env)
    return env[name]


class DecoderTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('decoder_shim', SCRIPTS / 'showtime-mpv-shim.py')
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.b = self.m.Bridge({'url': 'https://example.invalid/movie.mpd'})
        self.events = []
        self.b.broadcast = self.events.append
        self.clock = patch.object(self.m.time, 'monotonic', return_value=100)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.b._reset_decoder()

    def sample(self, pos, **values):
        body = {'media_revision': self.b.media_revision, 'position_ns': pos * 1e9,
                'duration_ns': 100 * 1e9, 'state': 'playing', 'video_streams': 1,
                'seek_revision': self.b.seek_revision}
        body.update(values)
        return self.b.update_progress(body)

    def ready(self, **values):
        self.sample(0, **values)
        self.now.return_value = 101
        self.sample(1, **values)
        self.assertTrue(self.b.decoder_ready)

    def ends(self):
        return [x for x in self.events if x.get('event') == 'end-file']

    def test_metadata_duration_and_frozen_resume_do_not_signal_success(self):
        self.assertFalse(self.b.properties['vo-configured'])
        self.assertTrue(self.b.properties['core-idle'])
        self.sample(60, state='paused')
        self.now.return_value = 102
        self.sample(60)
        self.now.return_value = 104
        self.sample(60)
        self.assertFalse(self.b.decoder_ready)
        self.assertFalse(any(x.get('event') in ('file-loaded', 'playback-restart') for x in self.events))
        self.assertTrue(self.b.check_watchdog(146))
        self.assertEqual(self.ends(), [{'event': 'end-file', 'reason': 'error', 'file_error': 'loading_failed'}])

    def test_video_progress_is_conservative_and_emitted_once(self):
        self.ready()
        self.now.return_value = 102
        self.sample(2)
        self.assertFalse(self.b.properties['vo-configured'])
        self.assertEqual(self.b.properties['user-data/kunai-readiness'], 'position-progress')
        self.assertEqual(sum(x.get('event') == 'file-loaded' for x in self.events), 1)
        self.assertEqual(sum(x.get('event') == 'playback-restart' for x in self.events), 1)
        self.assertEqual(self.b.properties['demuxer-cache-duration'], 0)

    def test_audio_only_can_play_without_claiming_video(self):
        self.ready(video_streams=0, audio_streams=1)
        self.assertFalse(self.b.properties['vo-configured'])
        self.assertTrue(self.b.report_eof({'media_revision': 1}))
        self.assertEqual(self.ends()[-1]['reason'], 'eof')
        self.assertFalse(self.sample(3))
        self.assertFalse(self.b.report_eof({'media_revision': 1}))

    def test_missing_or_stale_identity_and_malformed_progress_are_rejected(self):
        for value in (None, 0, 2, '1', True):
            self.assertFalse(self.sample(1, media_revision=value))
        for value in (float('nan'), float('inf'), -1, 'bad'):
            self.assertFalse(self.sample(1, position_ns=value))
        self.assertFalse(self.sample(1, video_streams='bad'))
        self.assertFalse(self.b.decoder_ready)
        self.assertEqual(self.b.position, 0)

    def test_replacement_resets_duration_and_rejects_old_progress_error_and_eof(self):
        self.ready()
        old = self.b.media_revision
        self.b.set_media(self.b.url, {'start': '10'})
        self.assertEqual(self.b.duration, 0)
        self.assertEqual(self.b.position, 10)
        self.assertFalse(self.b.decoder_ready)
        self.assertFalse(self.b.properties['vo-configured'])
        self.assertFalse(self.sample(100, media_revision=old))
        self.assertFalse(self.b.report_playback_error({'media_revision': old}))
        self.assertFalse(self.b.report_eof({'media_revision': old}))
        self.assertIsNone(self.b.failed_media_revision)
        self.assertEqual(self.b.position, 10)
        self.ready()

    def test_error_is_terminal_deduplicated_and_retry_is_new_generation(self):
        for bad in ({}, {'revision': 1}, {'media_revision': 99}):
            self.assertFalse(self.b.report_playback_error(bad))
        self.assertTrue(self.b.report_playback_error({'media_revision': 1}))
        self.assertFalse(self.b.report_playback_error({'media_revision': 1}))
        self.assertFalse(self.sample(1))
        self.assertEqual(len(self.ends()), 1)
        self.b.set_media(self.b.url)
        self.ready()

    def test_eos_without_progress_is_failure(self):
        self.assertTrue(self.b.report_eof({'media_revision': 1}))
        self.assertEqual(self.ends()[-1]['reason'], 'error')
        self.assertFalse(self.b.properties['eof-reached'])

    def test_watchdog_pauses_but_buffering_and_stalls_are_bounded(self):
        self.ready()
        self.b.handle_command(['set_property', 'pause', True])
        self.assertFalse(self.b.check_watchdog(200))
        self.b.handle_command(['set_property', 'pause', False])
        self.assertFalse(self.b.check_watchdog(220))
        self.assertTrue(self.b.check_watchdog(231))
        self.assertFalse(self.b.check_watchdog(300))
        self.assertEqual(len(self.ends()), 1)

    def test_seek_jump_and_old_seek_samples_are_not_readiness(self):
        self.sample(1)
        self.b.handle_command(['seek', 60, 'absolute'])
        self.assertFalse(self.sample(1, seek_revision=0))
        self.sample(60)
        self.now.return_value = 101
        self.sample(90)  # discontinuity, not decoded progress
        self.assertFalse(self.b.decoder_ready)
        self.b.handle_command(['seek', -5, 'relative'])
        self.assertEqual(self.b.position, 85)
        self.b.handle_command(['seek', 0, 'absolute'])
        self.assertEqual(self.b.position, 0)

    def test_headers_are_replaced_without_leaking_old_credentials(self):
        self.b.set_media('https://other.invalid/new.mp4', {'referrer': 'https://old.invalid',
                         'http-header-fields': 'Authorization: Bearer test'})
        self.b.set_media('https://third.invalid/new.mp4')
        self.assertEqual(self.b.header_dict(), {})
        self.assertEqual(self.b.referrer, '')

    def test_new_credentials_can_retry_after_explicit_access_denial(self):
        self.b.report_playback_error({'media_revision': 1, 'reason': 'access-denied'})
        self.b.set_media(self.b.url, {'http-header-fields': 'Authorization: Bearer renewed'})
        self.assertEqual(self.b.media_revision, 2)
        self.assertEqual(self.b.header_dict()['Authorization'], 'Bearer renewed')
        self.ready()

    def test_native_pause_and_unpause_update_watchdog_without_stale_control_ack(self):
        self.ready()
        self.b.handle_command(['set_property', 'pause', True])
        self.sample(1, state='playing', pause_revision=0)
        self.assertTrue(self.b.paused)
        self.sample(1, state='paused', pause_revision=1)
        self.assertTrue(self.b.paused)
        self.sample(1, state='playing', pause_revision=1)
        self.assertFalse(self.b.paused)
        self.assertTrue(self.b.check_watchdog(140))


    def test_stop_rejects_late_success_and_watchdog(self):
        self.b.handle_command(['stop'])
        self.assertFalse(self.sample(1))
        self.assertFalse(self.b.check_watchdog(1000))
        self.assertEqual(self.ends()[-1]['reason'], 'stop')

    def test_replacement_cannot_interleave_current_report_property_events(self):
        entered, release = threading.Event(), threading.Event()
        original = self.b.prop
        def held_prop(name, value):
            if name == 'time-pos':
                entered.set()
                release.wait(2)
            original(name, value)
        self.b.prop = held_prop
        report = threading.Thread(target=lambda: self.sample(1))
        report.start()
        self.assertTrue(entered.wait(1))
        replaced = threading.Event()
        replace = threading.Thread(target=lambda: (self.b.set_media('https://new.invalid/v.mp4'), replaced.set()))
        replace.start()
        self.assertFalse(replaced.wait(0.05))
        release.set()
        report.join(2); replace.join(2)
        self.assertTrue(replaced.is_set())
        self.assertEqual(self.b.position, 0)
        self.assertEqual(self.b.properties['time-pos'], 0)


class PlayerGenerationTests(unittest.TestCase):
    def test_uri_identity_is_unique_and_preserves_signed_query(self):
        fn = player_function('_generation_uri', {'urllib': urllib, 'uuid': uuid})
        first = fn('https://cdn.invalid/v.mp4?signature=abc&x=1')
        second = fn('https://cdn.invalid/v.mp4?signature=abc&x=1')
        self.assertNotEqual(first, second)
        self.assertEqual(urllib.parse.urlsplit(first)._replace(fragment='').geturl(),
                         'https://cdn.invalid/v.mp4?signature=abc&x=1')

    def test_bus_drops_old_error_position_and_eos_before_ui_translation(self):
        window = SimpleNamespace(_decoder_uri='https://cdn.invalid/v#new')
        forwarded, reported = [], []
        kinds = SimpleNamespace(STATE_CHANGED='state', MEDIA_INFO_UPDATED='info',
                                POSITION_UPDATED='position', DURATION_CHANGED='duration',
                                ERROR='error', END_OF_STREAM='eos', SEEK_DONE='seek-done')
        gst = SimpleNamespace(PlayMessage=kinds, play_message_get_uri=lambda m: m.uri,
                              play_message_parse_type=lambda m: m.kind,
                              play_message_parse_error=lambda m: (SimpleNamespace(message='codec failure'), None))
        env = {'_SESSION_WINDOWS': {'bus': window}, 'SESSION_URL': 'session', 'GstPlay': gst,
               '_original_play_bus_message': lambda *args: forwarded.append(args[-1].kind),
               '_report_decoder_event': lambda *args: reported.append(args[1:]),
               '_is_source_access_denied': lambda msg: False}
        handler = player_function('_guarded_play_bus_message', env)
        for kind in ('error', 'eos', 'position', 'info', 'state'):
            handler(None, 'bus', SimpleNamespace(uri='https://cdn.invalid/v#old', kind=kind))
        self.assertEqual(forwarded, [])
        self.assertEqual(reported, [])
        handler(None, 'bus', SimpleNamespace(uri=window._decoder_uri, kind='error'))
        self.assertEqual(forwarded, ['error'])
        self.assertEqual(reported, [('playback-error', 'playback-failed')])

    def test_stale_session_response_does_not_replace_new_media(self):
        window = SimpleNamespace(_media_session={'media_revision': 3, 'revision': 10})
        setter = player_function('_set_session', {})
        setter(window, {'media_revision': 2, 'revision': 20})
        setter(window, {'media_revision': 3, 'revision': 9})
        self.assertEqual(window._media_session, {'media_revision': 3, 'revision': 10})

    def test_audio_language_choice_follows_new_stream_index_once(self):
        selected=[]
        window=SimpleNamespace(_preferred_audio_language='de', play=SimpleNamespace(set_audio_track=selected.append))
        info=SimpleNamespace(get_audio_streams=lambda: [
            SimpleNamespace(get_language=lambda: 'en', get_index=lambda: 0),
            SimpleNamespace(get_language=lambda: 'de', get_index=lambda: 2)])
        env={'os':os,'sys':sys,'SESSION_URL':'', '_original_media_info_updated':lambda *a: None}
        fn=player_function('_premium_media_info_updated',env)
        fn(window,None,info)
        fn(window,None,info)
        self.assertEqual(selected,[2])
        self.assertTrue(window._media_stream_ready)

    def test_seek_waits_for_completion_and_nonseekable_stream_is_untouched(self):
        callbacks=[]
        glib=SimpleNamespace(SOURCE_REMOVE=False,SOURCE_CONTINUE=True,
                             timeout_add=lambda ms,fn: callbacks.append((ms,fn)))
        calls=[]
        window=SimpleNamespace(_media_generation=1,_media_session={'media_revision':1},
            _media_stream_ready=True,_decoder_seekable=True,
            play=SimpleNamespace(props=SimpleNamespace(duration=10_000_000_000),seek=calls.append))
        env={'GLib':glib,'sys':sys,'_safe_seek_target_ns':player_function('_safe_seek_target_ns',{})}
        schedule=player_function('_schedule_media_seek',env)
        schedule(window,3_000_000_000,1)
        self.assertFalse(callbacks.pop(0)[1]())
        self.assertEqual(calls,[3_000_000_000])
        self.assertTrue(window._initial_resume_pending)
        self.assertEqual(callbacks[0][0],6000)
        callbacks.pop(0)[1]()
        self.assertFalse(window._initial_resume_pending)
        window._decoder_seekable=False
        schedule(window,4_000_000_000,1)
        callbacks.pop(0)[1]()
        self.assertEqual(calls,[3_000_000_000])
        self.assertFalse(window._initial_resume_pending)

    def test_zero_seek_supported_and_negative_rejected(self):
        fn = player_function('_safe_seek_target_ns', {})
        self.assertEqual(fn(0, 10000000000), 0)
        self.assertIsNone(fn(-1, 10000000000))


if __name__ == '__main__':
    unittest.main()
