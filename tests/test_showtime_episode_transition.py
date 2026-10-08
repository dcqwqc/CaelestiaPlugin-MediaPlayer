"""Regression tests for seamless episode navigation, subtitles and safe seeks."""
import ast
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(ROOT))

class EpisodeTransitionTests(unittest.TestCase):
    def bridge(self):
        spec = importlib.util.spec_from_file_location("episode_bridge", ROOT / "showtime-mpv-shim.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        b = m.Bridge({"url": "https://cdn.example.org/episode1.mpd"})
        return b

    def test_english_intent_survives_episode_and_track_order(self):
        b = self.bridge()
        ar = b.add_subtitle("https://sub.example/ar-1.vtt", "Arabic", "ar", selected=True)
        self.assertEqual(b.selected_subtitle_id, "off", "default should not activate Arabic")
        en = b.add_subtitle("https://sub.example/en-1.vtt", "English", "eng", selected=False)
        self.assertEqual(b.selected_subtitle_id, en["id"])
        b.select_subtitle(en["id"])
        self.assertEqual(b.subtitle_preference, ("original", "en"))
        previous_revision = b.media_revision
        b.set_media("https://cdn.example.org/episode2.mpd")
        self.assertEqual(b.media_revision, previous_revision + 1)
        self.assertEqual(b.session()["subtitles"][0]["source"], "generated")
        self.assertEqual(b.session()["selected_subtitle_id"], "off")
        self.assertEqual(b.properties["track-list"], [])
        arabic2 = b.add_subtitle("https://sub.example/ar-2.vtt", "Arabic", "ara", selected=True)
        self.assertEqual(b.selected_subtitle_id, "off")
        english2 = b.add_subtitle("https://sub.example/en-2.vtt", "English CC", "en-US", selected=False)
        b.add_subtitle("https://sub.example/ar-2.vtt", "Arabic", "ara", selected=True)
        self.assertEqual(b.selected_subtitle_id, english2["id"])
        self.assertEqual(b.session()["subtitle_url"], english2["url"])
        self.assertNotEqual(arabic2["id"], english2["id"])

    def test_changed_preference_wins_over_default_english(self):
        b = self.bridge()
        de = b.add_subtitle("https://sub.example/de1.vtt", "German", "deu")
        b.select_subtitle(de["id"])
        b.set_media("https://cdn.example.org/episode2.mpd")
        b.add_subtitle("https://sub.example/en2.vtt", "English", "en", selected=True)
        self.assertEqual(b.selected_subtitle_id, "off")
        de2 = b.add_subtitle("https://sub.example/de2.vtt", "Deutsch", "de")
        self.assertEqual(b.selected_subtitle_id, de2["id"])

    def test_explicit_off_stays_off_even_if_provider_auto_selects(self):
        b = self.bridge()
        b.select_subtitle("off")
        b.set_media("https://cdn.example.org/episode2.mpd")
        b.add_subtitle("https://sub.example/eng.vtt", "English", "en", selected=True)
        self.assertEqual(b.selected_subtitle_id, "off")
        self.assertIsNone(b.subtitle)

    def test_old_progress_does_not_change_new_episode(self):
        b = self.bridge()
        b.set_media("https://cdn.example.org/episode2.mpd")
        b.update_progress({
            "media_revision": 1,
            "position_ns": 1350739509270,
            "duration_ns": 1500000000000,
        })
        self.assertEqual(b.position, 0)
        b.update_progress({
            "media_revision": 2,
            "position_ns": 3500000000,
            "duration_ns": 1318800000000,
        })
        self.assertEqual(b.position, 3.5)

    def test_subtitle_preference_survives_player_restart_in_private_config(self):
        from tempfile import TemporaryDirectory
        import os, stat
        with TemporaryDirectory() as td, patch.dict(os.environ, {"XDG_CONFIG_HOME":td}):
            from showtime_subtitle_preference import preference_path
            b=self.bridge()
            # A persistent player is how Kunai launches Showtime (--idle=yes).
            b.persist_subtitle_choice=True
            de=b.add_subtitle("https://sub.example/de.vtt","German","deu")
            b.select_subtitle(de["id"])
            path=preference_path()
            self.assertTrue(path.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            again=self.bridge()
            again.persist_subtitle_choice=True
            # Bridge constructor in a persistent session reads the saved choice.
            spec = importlib.util.spec_from_file_location("persistent_bridge", ROOT / "showtime-mpv-shim.py")
            m=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            persisted=m.Bridge({"url":"https://cdn.example.org/ep2.mpd","persistent":True})
            self.assertEqual(persisted.subtitle_preference,("original","de"))
            persisted.add_subtitle("https://sub.example/en2.vtt","English","en",selected=True)
            self.assertEqual(persisted.selected_subtitle_id,"off")
            de2=persisted.add_subtitle("https://sub.example/de2.vtt","German","de")
            self.assertEqual(persisted.selected_subtitle_id,de2["id"])

    def test_ai_selection_intent_survives_track_change(self):
        from showtime_subtitle_preference import subtitle_intent, matches_intent, language_tag
        self.assertEqual(language_tag("Japanese Romaji"), "ja-romaji")
        self.assertEqual(language_tag("eng"), "en")
        self.assertTrue(matches_intent(("original", "en"), {"source":"original", "language":"English"}))
        self.assertFalse(matches_intent(("original", "en"), {"source":"original", "language":"Arabic"}))
        self.assertTrue(matches_intent(("generated", "ja-romaji"), {"source":"generated", "language":"Japanese Romaji"}))

    def test_invalid_resume_is_not_seeked_to_end_of_shorter_episode(self):
        helper = self.showtime_fn("_safe_seek_target_ns")
        # User reported failed seek to 22:30.739 with 21:58.8 episode.
        self.assertIsNone(helper(1350739509270, 1318800000000))
        self.assertEqual(helper(250000000000, 1318800000000), 250000000000)
        self.assertIsNone(helper(-1, 1318800000000))

    def test_navigation_recovery_never_flashes_a_transient_error(self):
        import urllib
        import urllib.parse
        toasts=[]
        scheduled=[]
        win=SimpleNamespace(_media_busy=False,_media_session={"media_revision":1})
        def later(ms,fn):
            scheduled.append(fn)
            return len(scheduled)
        glib=SimpleNamespace(SOURCE_REMOVE=False,idle_add=lambda fn:fn(),timeout_add=later)
        fake_threading=SimpleNamespace(Thread=lambda target,**kwargs:SimpleNamespace(start=target))
        state={"recover":True}
        def refresh(_window):
            if state["recover"]:
                win._media_session={"media_revision":2}
        def fail(*args,**kwargs):
            raise TimeoutError("controller delayed")
        env={"SESSION_BASE":"http://127.0.0.1:9999",
             "GLib":glib,"threading":fake_threading,"urllib":urllib,
             "_http_json":fail,"_toast":lambda _w,msg:toasts.append(msg),
             "_refresh_session_async":refresh,"_set_session":lambda w,s:None}
        navigate=self.showtime_fn("_navigate",env)
        navigate(win,"next")
        self.assertEqual(len(scheduled),1)
        scheduled.pop()()
        self.assertEqual(toasts,[])
        state["recover"]=False
        win._media_session={"media_revision":2}
        navigate(win,"next")
        scheduled.pop()()
        self.assertEqual(len(toasts),1)

    def showtime_fn(self,name,environment=None):
        module = ast.parse((ROOT / "showtime-player.py").read_text())
        node = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name==name)
        ns = {} if environment is None else environment
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(ROOT/"showtime-player.py"),"exec"),ns)
        return ns[name]

    def test_deferred_seek_ignores_stale_generation_and_waits_for_media_info(self):
        callbacks=[]
        gst=SimpleNamespace(SOURCE_REMOVE=False,SOURCE_CONTINUE=True,timeout_add=lambda ms,cb:callbacks.append(cb))
        seeked=[]
        player=SimpleNamespace(props=SimpleNamespace(duration=1318800000000),seek=lambda pos:seeked.append(pos))
        window=SimpleNamespace(_media_session={"media_revision":2},_media_generation=2,_media_stream_ready=False,play=player)
        env={"GLib":gst,"_safe_seek_target_ns":self.showtime_fn("_safe_seek_target_ns"),"sys":sys}
        schedule=self.showtime_fn("_schedule_media_seek",env)
        schedule(window,1350739509270,2)
        self.assertTrue(callbacks[0]())
        self.assertEqual(seeked,[])
        window._media_stream_ready=True
        self.assertFalse(callbacks[0]())
        self.assertEqual(seeked,[], "out-of-range previous episode seek must never execute")
        schedule(window,250000000000,2)
        window._media_generation=3
        self.assertFalse(callbacks[1]())
        self.assertEqual(seeked,[], "old generation's seek must be discarded")
        schedule(window,250000000000,2)
        self.assertFalse(callbacks[2]())
        self.assertEqual(seeked,[250000000000])

if __name__=="__main__":
    unittest.main()
