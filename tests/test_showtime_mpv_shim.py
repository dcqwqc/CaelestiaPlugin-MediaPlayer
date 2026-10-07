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
