"""Offline provisioning contracts; signature tools are injected, not bypassed."""
from importlib.machinery import SourceFileLoader
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from showtime_host import verify_runtime


@pytest.fixture
def provision(tmp_path, monkeypatch):
    loader = SourceFileLoader('provision_native', str(SCRIPTS / 'provision-showtime-libmpv'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec); loader.exec_module(m)
    location = tmp_path / 'flatpak/deployment'
    resources = location / 'files/share/showtime'
    resources.mkdir(parents=True)
    (resources / 'showtime.gresource').write_bytes(b'fixture')
    package = location / 'files/lib/python3.14/site-packages/showtime'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('VERSION = "50.0"')
    schemas = location / 'files/share/glib-2.0/schemas'
    schemas.mkdir(parents=True)
    (schemas / 'org.gnome.Showtime.gschema.xml').write_text('fixture')
    archive = tmp_path / 'python-mpv.pkg.tar'
    with tarfile.open(archive, 'w') as tar:
        for name, data in [('.PKGINFO', b'pkgname = python-mpv\n'),
                           ('usr/lib/python3.14/site-packages/mpv.py', b'# verified fixture'),
                           ('../../unwanted', b'never extracted')]:
            entry = tarfile.TarInfo(name); entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'glib-compile-schemas':
            (Path(command[-1]) / 'gschemas.compiled').write_bytes(b'compiled')
        return SimpleNamespace(returncode=0)
    def output(command, **kwargs):
        return str(location) if '--show-location' in command else 'origin' if '--show-origin' in command else 'commit'
    monkeypatch.setattr(m.subprocess, 'run', run)
    monkeypatch.setattr(m.subprocess, 'check_output', output)
    monkeypatch.setattr(m, 'verify_showtime', lambda *args: calls.append(['verify-flatpak', *map(str, args)]))
    args = SimpleNamespace(destination=tmp_path / 'runtime', mpv_package=archive,
                           signature=tmp_path / 'package.sig', keyring=tmp_path / 'system-keyring')
    return m, args, calls


def test_snapshot_installed_site_packages_and_resources(provision):
    m, args, calls = provision
    m.provision(args)
    assert calls[0][0] == 'pacman-key' and calls[1][0] == 'verify-flatpak'
    assert verify_runtime(args.destination) == args.destination
    assert (args.destination / 'python/mpv.py').read_text() == '# verified fixture'
    assert not (args.destination.parent / 'unwanted').exists()
    assert not list(args.destination.rglob('*.so'))


def test_bad_package_signature_publishes_nothing(provision, monkeypatch):
    m, args, _calls = provision
    def bad(*a, **k): raise subprocess.CalledProcessError(1, 'gpgv')
    monkeypatch.setattr(m.subprocess, 'run', bad)
    with pytest.raises(subprocess.CalledProcessError): m.provision(args)
    assert not args.destination.exists()


def test_bad_flatpak_signature_publishes_nothing(provision, monkeypatch):
    m, args, _calls = provision
    monkeypatch.setattr(m, 'verify_showtime', lambda *a: (_ for _ in ()).throw(RuntimeError('untrusted commit')))
    with pytest.raises(RuntimeError): m.provision(args)
    assert not args.destination.exists()


def test_existing_runtime_is_not_overwritten(provision):
    m, args, calls = provision
    args.destination.mkdir()
    with pytest.raises(RuntimeError): m.provision(args)
    assert calls == []
