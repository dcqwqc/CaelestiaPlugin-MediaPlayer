import importlib.util
import json
import socket
import tempfile
import threading
from pathlib import Path


def load_shim():
    path = Path(__file__).parents[1] / "scripts" / "showtime-mpv-shim.py"
    spec = importlib.util.spec_from_file_location("showtime_mpv_shim", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_load_seek_and_subtitles():
    m = load_shim()
    b = m.Bridge({"url": "https://example.invalid/a.m3u8", "title": "Demo", "persistent": True})
    b.handle_command(["seek", 42, "absolute"])
    b.handle_command(["sub-add", "https://example.invalid/en.vtt", "select", "English", "en"])
    b.handle_command(["loadfile", "https://example.invalid/b.m3u8", "replace", -1, {"start": "3"}])
    assert b.url.endswith("b.m3u8")
    assert b.position == 3
    assert b.subtitle.endswith("en.vtt")
    assert b.session()["next_episode"]


def test_mpv_ipc_json_protocol():
    m = load_shim()
    b = m.Bridge({"url": "https://example.invalid/a.m3u8", "title": "Demo", "persistent": True})
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "mpv.sock")
        server = m.IPCServer(path, b)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        stream = sock.makefile("rwb", buffering=0)
        stream.write((json.dumps({"command": ["get_property", "media-title"], "request_id": 7}) + "\n").encode())
        response = json.loads(stream.readline())
        assert response["request_id"] == 7
        assert response["error"] == "success"
        assert response["data"] == "Demo"
        sock.close()
        server.shutdown()
        server.server_close()


def test_showtime_playback_error_reaches_kunai_ipc():
    """GStreamer errors must be mpv end-file failures, not silent UI errors."""
    import http.client

    m = load_shim()
    b = m.Bridge({"url": "https://example.invalid/a.m3u8", "title": "Demo", "persistent": True})
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "mpv.sock")
        ipc = m.IPCServer(path, b)
        http_server = m.HTTPServer(("127.0.0.1", 0), b)
        ipc_thread = threading.Thread(target=ipc.serve_forever, daemon=True)
        http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        ipc_thread.start()
        http_thread.start()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        try:
            sock.connect(path)
            stream = sock.makefile("rwb", buffering=0)
            conn = http.client.HTTPConnection("127.0.0.1", http_server.server_address[1], timeout=3)

            def send_failure(revision, media_revision=None):
                body = {"revision": revision, "reason": "access-denied"}
                if media_revision is not None:
                    body["media_revision"] = media_revision
                conn.request("POST", "/api/playback-error", body=json.dumps(body), headers={"Content-Type": "application/json", "X-Showtime-Session-Token": b.token})
                response = conn.getresponse()
                assert response.status == 200
                return json.loads(response.read())

            assert send_failure(1)["accepted"] is True
            event = json.loads(stream.readline())
            assert event == {"event": "end-file", "reason": "error", "file_error": "kunai_access_denied"}
            assert send_failure(1)["accepted"] is False, "duplicate event must be ignored"

            b.set_media("https://example.invalid/b.m3u8")
            assert json.loads(stream.readline())["event"] == "property-change"
            assert json.loads(stream.readline())["event"] == "property-change"
            assert json.loads(stream.readline())["event"] == "file-loaded"
            assert send_failure(1)["accepted"] is False, "old-source failure must be ignored"
            assert send_failure(2, media_revision=1)["accepted"] is False, "stale media must be rejected"
            # General revision may have advanced due to title or seek updates:
            # stable media_revision is the authoritative load identity.
            assert send_failure(900, media_revision=2)["accepted"] is True
            while True:
                event = json.loads(stream.readline())
                if event.get("event") == "end-file":
                    assert event["reason"] == "error"
                    break
            conn.close()
        finally:
            sock.close()
            http_server.shutdown()
            http_server.server_close()
            ipc.shutdown()
            ipc.server_close()


def test_showtime_poll_reloads_media_on_new_loadfile():
    """Session polling must reopen the actual video even for same-URL retry."""
    import ast
    source = (Path(__file__).parents[1] / "scripts" / "showtime-player.py").read_text()
    tree = ast.parse(source)
    method = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_session_needs_media_reload")
    scope = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "showtime-player.py", "exec"), scope)
    needs_reload = scope["_session_needs_media_reload"]
    before = {"media_url": "https://media.test/denied.m3u8", "media_revision": 1, "revision": 1}
    assert needs_reload({}, before) is False, "startup is already launching the video"
    assert needs_reload(before, {**before, "revision": 2}) is False, "metadata must not cause replay"
    assert needs_reload(before, {**before, "media_revision": 2}) is True, "same URL loadfile must replay"
    assert needs_reload(before, {**before, "media_url": "https://media.test/working.m3u8", "media_revision": 2}) is True
    assert needs_reload({"media_url": "before"}, {"media_url": "after"}) is True, "legacy source changes still work"
    assert needs_reload({"media_url": "before"}, {"media_url": "before"}) is False


def test_showtime_reload_handler_does_not_recurse():
    """A new loadfile must enter the playback function exactly once."""
    import ast
    from types import SimpleNamespace
    source = (Path(__file__).parents[1] / "scripts" / "showtime-player.py").read_text()
    tree = ast.parse(source)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_session_needs_media_reload", "_set_session")]
    called = []
    scope = {
        "os": __import__("os"),
        "_apply_session_title": lambda *_: None,
        "_set_external_subtitle": lambda *_: None,
    }
    exec(compile(ast.Module(body=funcs, type_ignores=[]), "showtime-player.py", "exec"), scope)

    def playback(window, session):
        called.append(session["media_revision"])
        # Like production _play_session, update session display information.
        scope["_set_session"](window, session, refresh_menu=False)

    scope["_play_session"] = playback
    before = {"media_url": "https://example.invalid/video.m3u8", "media_revision": 1, "selected_subtitle_id": "off", "revision": 1}
    after = {**before, "media_revision": 2, "revision": 2}
    window = SimpleNamespace(_media_session=before, _initial_resume_scheduled=True, lookup_action=lambda _: None)
    scope["_set_session"](window, after, refresh_menu=False)
    assert called == [2], f"expected one media reload, got {called}"
    assert window._media_session is after
    scope["_set_session"](window, after, refresh_menu=False)
    assert called == [2], "polling identical sessions must not reload"


def test_forbidden_source_does_not_repeatedly_reload_showtime():
    """403 is not repairable by retrying the identical stream URL."""
    m = load_shim()
    denied = "https://example.invalid/access-denied.m3u8"
    working = "https://example.invalid/other-authorized-source.mp4"
    b = m.Bridge({"url": denied, "title": "Test", "persistent": True})
    events = []
    b.broadcast = lambda event: events.append(event)
    assert b.report_playback_error({"revision": 1, "media_revision": 1,
                                    "reason": "access-denied"}) is True
    assert denied in b.access_denied_urls
    events.clear()
    before_revision = b.media_revision
    assert b.set_media(denied) is False
    assert b.media_revision == before_revision
    assert b.url == denied
    assert events == [{"event": "end-file", "reason": "error",
                       "file_error": "kunai_access_denied"}]
    assert b.set_media(working) is True, "a different source must still be allowed"
    assert b.url == working
    assert b.media_revision == before_revision + 1
    assert b.report_playback_error({"media_revision": b.media_revision,
                                    "reason": "playback-failed"}) is True
    assert working not in b.access_denied_urls, "temporary decoder failures may be retried"


def test_showtime_rejects_only_upstream_access_errors_without_modal():
    """Pure classifier guard: avoid swallowing actual decoder/player errors."""
    import ast
    src = (Path(__file__).parents[1] / 'scripts' / 'showtime-player.py').read_text()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == '_is_source_access_denied')
    import re
    context={'re':re}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'source-classifier','exec'),context)
    classify=context['_is_source_access_denied']
    assert classify('Not authorized to access resource.')
    assert classify('Forbidden (403), URL: https://example.invalid/denied')
    assert classify('HTTP 403')
    assert not classify('Codec not found / unplayable video')
    assert not classify('Connection reset by peer')


def test_media_http_headers_follow_the_selected_source():
    m = load_shim()
    cfg = m.parse_args([
        "--referrer=https://player.example/",
        "--user-agent=GstIntegration/1.0",
        "--http-header-fields=Cookie: signed=abc,Origin: https://player.example",
        "https://video.example/master.m3u8",
    ])
    b = m.Bridge(cfg)
    assert b.session()["http_headers"] == {
        "Cookie": "signed=abc", "Origin": "https://player.example",
        "Referer": "https://player.example/", "User-Agent": "GstIntegration/1.0",
    }
    b.set_media("https://video.example/next.m3u8", {
        "referrer": "https://new.example/",
        "http-header-fields": "Cookie: other=123",
    })
    assert b.session()["http_headers"] == {
        "Cookie": "other=123", "Referer": "https://new.example/",
    }
    b.set_media("file:///tmp/sample.webm", {})
    assert b.session()["http_headers"] == {}, "provider cookies must not leak to the next file"
    assert m.normalize_media_headers(fields="Bad"+chr(10)+"Header: injected,Host: override,Cookie: ok=1") == {"Cookie": "ok=1"}


def test_local_control_api_requires_session_token():
    import http.client
    m = load_shim()
    b = m.Bridge({"url": "file:///tmp/test.webm", "persistent": True})
    server = m.HTTPServer(("127.0.0.1", 0), b)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    try:
        conn.request("GET", "/api/session")
        result = conn.getresponse()
        assert result.status == 403
        result.read()
        conn.request("GET", "/api/session", headers={"X-Showtime-Session-Token": b.token})
        result = conn.getresponse()
        assert result.status == 200
        assert json.loads(result.read())["media_url"].startswith("file:")
        conn.request("POST", "/api/playback-error", body=json.dumps({"reason":"access-denied", "media_revision":1}),
                     headers={"Content-Type":"application/json"})
        result = conn.getresponse()
        assert result.status == 403
        result.read()
        assert b.failed_media_revision is None
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
