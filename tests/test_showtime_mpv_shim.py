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
    # The previous episode's VTT must never be attached to the next file.
    assert b.subtitle is None
    assert b.subtitle_preference == ("original", "en")
    b.handle_command(["sub-add", "https://example.invalid/en2.vtt", "select", "English", "en"])
    assert b.subtitle.endswith("en2.vtt")
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


def test_parse_args_keeps_kunai_http_headers_for_ai_input():
    m = load_shim()
    cfg = m.parse_args([
        "--http-header-fields=Origin: https://example.invalid,Authorization: Bearer token",
        "--referrer=https://ref.example/",
        "https://cdn.example/video.m3u8",
    ])
    assert "Origin: https://example.invalid" in cfg["http_headers"]
    assert "Authorization: Bearer token" in cfg["http_headers"]
    b = m.Bridge(cfg)
    assert "Referer: https://ref.example/" in b.http_headers


def test_space_user_agent_survives_initial_launch_and_episode_handoff():
    m = load_shim()
    cfg = m.parse_args([
        "--user-agent= ",
        "https://cdn.example/movie.m3u8",
    ])
    bridge = m.Bridge(cfg)
    assert bridge.header_dict()["User-Agent"] == " "
    bridge.handle_command([
        "loadfile", "https://cdn.example/next.m3u8", "replace", -1,
        {"user-agent": " "},
    ])
    assert bridge.header_dict()["User-Agent"] == " "
    assert m.media_header_dict(["User-Agent:  ", "Origin: https://example.invalid"])[
        "User-Agent"
    ] == " "


    m = load_shim()
    b = m.Bridge({"url": "https://example.invalid/a.m3u8", "title": "Demo Episode"})
    with tempfile.TemporaryDirectory() as td:
        b.subtitle_cache = Path(td)
        b.base_url = "http://127.0.0.1:12345"
        generated = b.generated_path("en")
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n")

        track = next(x for x in b.subtitle_catalog() if x["id"] == "generated-en")
        assert track["available"] is True
        session = b.select_subtitle("generated-en")
        assert session["selected_subtitle_id"] == "generated-en"
        assert "/api/subtitles/generated-en.vtt?token=" in session["subtitle_url"]
        assert b.generated_bytes("generated-en").startswith(b"WEBVTT")


def test_ai_extract_audio_passes_headers_to_ffmpeg():
    path = Path(__file__).parents[1] / "scripts" / "ai-subtitles.py"
    spec = importlib.util.spec_from_file_location("ai_subtitles_header_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def fake_run(cmd, check):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"wav")

    module.subprocess.run = fake_run
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        module.extract_audio(
            "https://example.invalid/video.m3u8",
            wav,
            ["Referer: https://example.invalid/", "Authorization: Bearer secret"],
        )
        assert wav.exists()

    cmd = calls[0]
    header_value = cmd[cmd.index("-headers") + 1]
    assert "Referer: https://example.invalid/\r\n" in header_value
    assert "Authorization: Bearer secret\r\n" in header_value


def test_loadfile_refreshes_per_episode_headers():
    m = load_shim()
    b = m.Bridge({
        "url": "https://example.invalid/a.m3u8",
        "title": "Episode A",
        "referrer": "https://old.example/",
        "http_headers": ["Origin: https://old.example"],
    })
    b.handle_command([
        "loadfile",
        "https://example.invalid/b.m3u8",
        "replace",
        -1,
        {
            "referrer": "https://new.example/",
            "user-agent": "KunaiTest/1",
            "http-header-fields": "Origin: https://new.example,Authorization: Bearer next",
        },
    ])
    assert b.referrer == "https://new.example/"
    assert "Referer: https://new.example/" in b.http_headers
    assert "User-Agent: KunaiTest/1" in b.http_headers
    assert "Origin: https://new.example" in b.http_headers
    assert "Authorization: Bearer next" in b.http_headers
    assert all("old.example" not in value for value in b.http_headers)


def test_subtitle_ids_remain_stable_across_removals_and_sid_off():
    m = load_shim()
    b = m.Bridge({"url": "https://example.invalid/a.m3u8", "title": "Demo"})
    first = b.add_subtitle("https://example.invalid/en.vtt", "English", "en", selected=True)
    second = b.add_subtitle("https://example.invalid/de.vtt", "German", "de")
    assert first["mpv_id"] == 1
    assert second["mpv_id"] == 2

    b.handle_command(["sub-remove", 1])
    assert [track["mpv_id"] for track in b.subtitles] == [2]
    b.handle_command(["set_property", "sid", 2])
    assert b.subtitle.endswith("de.vtt")
    b.handle_command(["set_property", "sid", "no"])
    assert b.subtitle is None
    assert b.selected_subtitle_id == "off"


def test_only_vixsrc_hls_uses_native_backend():
    m = load_shim()
    assert m.is_vixsrc_native_fallback_url(
        "https://vixsrc.to/playlist/475052?token=secret"
    )
    assert not m.is_vixsrc_native_fallback_url(
        "https://vixsrc.to/embed/475052?token=secret"
    )
    assert not m.is_vixsrc_native_fallback_url(
        "https://vixsrc.to.evil.example/playlist/475052"
    )
    assert not m.is_vixsrc_native_fallback_url(
        "https://vidrock.net/movie/612654"
    )
    assert not m.is_vixsrc_native_fallback_url("file:///etc/passwd")
    assert not m.is_vixsrc_native_fallback_url(None)


def test_vixsrc_player_uses_wayland_shm_without_overriding_other_sources():
    m = load_shim()
    args = [
        "--input-ipc-server=/run/user/1000/kunai/test.sock",
        "--http-header-fields=Referer: https://vixsrc.to/",
        "https://vixsrc.to/playlist/475052?token=secret",
    ]
    cmd = m.vixsrc_native_player_command(args, {"WAYLAND_DISPLAY": "wayland-1"})
    assert cmd[:4] == ["/usr/bin/mpv", "--no-config", "--vo=wlshm", "--hwdec=no"]
    assert cmd[4:] == args
    assert m.vixsrc_native_player_command(args, {}) == ["/usr/bin/mpv", "--no-config", *args]


def test_vixsrc_main_dispatch_uses_native_and_other_hosts_use_showtime():
    m = load_shim()
    calls = []
    native_call = m.subprocess.call
    original_bridge = m.Bridge
    original_launch = m.launch_showtime
    try:
        m.subprocess.call = lambda argv: (calls.append(argv), 0)[1]
        args = ["https://vixsrc.to/playlist/475052?token=secret"]
        assert m.main(args) == 0
        assert calls and calls[0][0] == "/usr/bin/mpv"
        assert "--no-config" in calls[0]
        assert calls[0][-1] == args[-1]
        assert not m.is_vixsrc_native_fallback_url("https://vidrock.net/playlist/475052")
    finally:
        m.subprocess.call = native_call
        m.Bridge = original_bridge
        m.launch_showtime = original_launch
