import importlib.util
from pathlib import Path

path = Path(__file__).parents[1] / 'scripts' / 'ani-showtime-proxy.py'
spec = importlib.util.spec_from_file_location('ani_showtime_proxy', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def ts_packet(pid_byte=0):
    pkt = bytearray(188)
    pkt[0] = 0x47
    pkt[1] = pid_byte
    return bytes(pkt)


def test_disguised_png_prefix_is_removed():
    fake_png = b'\x89PNG\r\n\x1a\n' + b'x' * 62
    junk = b'junk' * 45 + b'zz'
    ts = b''.join(ts_packet(i) for i in range(8))
    data = fake_png + junk + ts
    off = mod.Proxy.ts_offset(data)
    assert off == len(fake_png) + len(junk)
    clean, stripped = mod.Proxy('', '', '').clean_segment(data, 'segment.ts.jpg')
    assert stripped == off
    assert clean == ts
    assert clean[0] == 0x47


def test_plain_ts_is_not_damaged():
    ts = b''.join(ts_packet(i) for i in range(8))
    clean, stripped = mod.Proxy('', '', '').clean_segment(ts, 'segment.ts')
    assert stripped == 0
    assert clean == ts


def test_non_media_blob_is_left_alone():
    data = b'hello world'
    clean, stripped = mod.Proxy('', '', '').clean_segment(data, 'poster.jpg')
    assert clean == data
    assert stripped is None


if __name__ == '__main__':
    for name in sorted(n for n in globals() if n.startswith('test_')):
        globals()[name]()
        print('PASS', name)
