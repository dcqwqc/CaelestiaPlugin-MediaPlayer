"""Verified user-local Showtime runtime; never mix host and Flatpak libraries."""
from __future__ import annotations

import hashlib
import json
import os
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
              for p in (root / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    if actual != set(files): raise RuntimeError('Showtime runtime file set changed')
    for relative, digest in files.items():
        path = root / relative
        if path.is_symlink() or root not in path.resolve().parents:
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
