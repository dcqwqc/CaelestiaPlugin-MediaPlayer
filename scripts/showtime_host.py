"""Verified user-local Showtime runtime; never mix host and Flatpak libraries."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


def verify_runtime(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format') != 1 or manifest.get('showtime_version') != '50.0':
        raise RuntimeError('Unsupported Showtime runtime')
    files = manifest.get('files', {})
    for required in ('share/showtime/showtime.gresource', 'share/showtime/showtime/__init__.py',
                     'python/mpv.py', 'schemas/gschemas.compiled'):
        if required not in files: raise RuntimeError('Incomplete Showtime runtime')
    # Verify all executable modules, not just the manifest's entries. Reject
    # injected importable files as well as modified or missing resources.
    actual = {str(p.relative_to(root)) for directory in ('share', 'python', 'schemas')
              for p in (root / directory).rglob('*') if p.is_file()}
    if actual != set(files): raise RuntimeError('Showtime runtime file set changed')
    for relative, digest in files.items():
        path = root / relative
        if any(p.is_symlink() for p in (path, *path.parents) if p != root) or root not in path.resolve().parents:
            raise RuntimeError('Unsafe Showtime runtime path')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError('Showtime runtime integrity check failed')
    return root


def host_command(bootstrap, session_url, token, env):
    root = verify_runtime(env.get('SHOWTIME_HOST_RUNTIME') or
                          Path(env.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'showtime-libmpv')
    child = env.copy()
    child.update(SHOWTIME_BACKEND='libmpv', SHOWTIME_DATA_DIR=str(root / 'share/showtime'),
                 MEDIA_PLAYER_SESSION_URL=session_url, MEDIA_PLAYER_SESSION_TOKEN=token,
                 GSETTINGS_SCHEMA_DIR=str(root / 'schemas'),
                 PYTHONPATH=os.pathsep.join((str(root / 'python'), str(root / 'share/showtime'))),
                 PYTHONDONTWRITEBYTECODE='1')
    # -s excludes unverified user site packages. GI, OpenGL and libmpv must be
    # supplied by the host package manager, never by an extracted Flatpak .so.
    return [child.get('SHOWTIME_HOST_PYTHON', '/usr/bin/python3'), '-s', str(bootstrap), '--new-window'], child


def preflight(command, env):
    """Check host ABIs before creating a window or starting any decoder."""
    code = """
import gi
for name, version in [('Gtk','4.0'),('Adw','1'),('GstPlay','1.0'),('GstPbutils','1.0'),('GstAudio','1.0')]:
    gi.require_version(name, version)
from gi.repository import Gtk, Adw
assert (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 18)
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 8)
from OpenGL import GL
from mpv import MPV, MpvRenderContext, MpvGlGetProcAddressFn
"""
    result = subprocess.run([command[0], '-s', '-c', code], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    if result.returncode:
        raise RuntimeError('Native Showtime requires host GTK 4.18+, libadwaita 1.8+, GI, PyOpenGL and libmpv.so.2')
