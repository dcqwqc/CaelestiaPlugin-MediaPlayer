# Showtime 50 native backend: release validation

Status (Mirai, 2026-10-20): **enabled through the user-local Showtime
launcher** after an isolated full-window playback acceptance run. Existing
GStreamer remains an explicit rollback. Other devices are not automatically
configured. This is not a claim of universal provider/DRM compatibility. This repository ships integration Python, not a fork
of Showtime's assets or a replacement window.

## Mirai release verification and rollback

On Mirai, the ~/.local/bin/mpv symlink points to
scripts/showtime-mpv-launcher. For real playback it selects the embedded
libmpv backend after verifying the installed runtime manifest and host
GI/GTK/libadwaita/libmpv ABI. If either fails, new sessions fall back to the
original Showtime GStreamer Flatpak. SHOWTIME_BACKEND=gstreamer always
overrides native selection; SHOWTIME_BACKEND=libmpv explicitly forces native.

The installed runtime is user-local under
~/.local/share/showtime-libmpv/50.0-20261020, provisioned from the installed
Showtime 50 Flatpak and an Arch-signed python-mpv module. No system libraries
were copied from the Flatpak into the host loader path. The original Flatpak
was not replaced.

**Actual installed-launcher test:** in a private Xvfb and private D-Bus session
using synthetic 35-second H.264/AAC test content on Mirai, the deployed
~/.local/bin/mpv launched the original Showtime 50 UI and rendered video.
Kunai's IPC reported rendered-frame-progress after native output and advancing
position. Pause, absolute seek to 4.0 s, resume, and a loadfile switch to a
second synthetic video all passed; readiness reset to pending for the new
source and returned only after new frames advanced. No original playback session
was stopped or altered during this test.

The explicit GStreamer rollback also opened the existing Flatpak in an
isolated session, advanced video, paused, sought and resumed. Its preexisting
source-switching path did not pass the native readiness assertion, so the
GStreamer path is a functional fallback, not an equivalent backend. The private
test sets FLATPAK_USER_DIR to the existing user install because the private
XDG_DATA_HOME would otherwise hide it.

**Remaining limits:** actual Wayland/Hyprland rendering, audible audio output,
MPRIS end-to-end, hardware decode, streaming services and DRM have not all
been certified in this release. No claims of universal playback support should
be made from synthetic media alone.

## Implementation and runtime boundaries

- `scripts/showtime_libmpv.py`: independently testable controller, source
  generations, original Showtime time/track/volume/rate interface, native
  subtitle loading, bounded sanitized error classification, EOF and cleanup.
- `scripts/showtime_libmpv_gtk.py`: factory replacement before window creation,
  original signal contract and GLArea inside the original GraphicsOffload.
  GTK owns window, overlays, shortcuts, fullscreen and MPRIS. GstPlay enums
  express the original UI's state vocabulary; no GstPlay decoder is created.
- `scripts/showtime-player.py`: existing Kunai session poll/authentication,
  progress, resume, source revisions, pause, seek, episode navigation, generated
  and study subtitles remain in use. Native external subtitles are downloaded
  without source credentials and decoded by mpv from a private temporary file.
- `scripts/showtime-mpv-shim.py`: native sessions return original source URLs
  and per-source headers directly. Existing origin-scoped GStreamer proxies
  are retained for rollback. No new network proxy is introduced.
- `scripts/showtime_host.py` and `scripts/provision-showtime-libmpv`: offline
  host-runtime snapshot, signature verification, integrity manifest and host
  ABI preflight. No extracted Flatpak shared library is added to the host
  loader path. Source modules/resources/schemas are kept outside Git.

Every media load gets a fresh mpv core and replaces all headers. GL context
release happens with GTK's context current; core termination runs off GTK.
Successor creation waits for termination: overlapping libmpv cores can route
FFmpeg diagnostics to the wrong core. Replacements arriving during teardown
retain only the latest source. All event payloads are copied on the event
thread and generation-checked on GTK. Queued rendering is coalesced.

The active EGL or GLX context determines proc-address lookup. Wayland only
uses EGL. Physical framebuffer dimensions account for GTK scale factor.
Audio output is enabled in production; test programs explicitly choose a null
audio sink. Native readiness requires advancing time plus actual render calls
for video, or configured native audio for an audio-only source. Metadata alone
is not readiness.

The player uses mpv/FFmpeg codecs, demuxers, HTTP and adaptive-stream handling.
It disables mpv user configuration, scripts/OSC, keyboard capture and yt-dlp:
GTK owns those surfaces. Host mpv build features and system audio/video drivers
still determine support. This does **not** claim every provider, codec, DRM,
redirect, or native mpv user configuration is supported or tested.

## Offline host preparation (future desktop validation)

Do not run this against a busy desktop as an automatic deployment. Keep the
current GStreamer player running until an explicitly scheduled validation.
The production Flatpak remains unchanged.

Requirements: host Python with GI, GTK >=4.18, libadwaita >=1.8, Gst/GstPlay/
GstAudio/GstPbutils typelibs (UI contracts), PyOpenGL, libmpv.so.2, GPGv,
GI OSTree, Flatpak and glib-compile-schemas. Host distribution packages supply
native libraries; a signed Arch python-mpv package supplies just `mpv.py`.
Python must support the archive compression (Python 3.14 supports zstd).

Copy a cached signed python-mpv package and its detached signature to a local
folder, then choose a **new** runtime destination:

```sh
python3 scripts/provision-showtime-libmpv \
  --mpv-package /path/to/python-mpv.pkg.tar.zst \
  --signature /path/to/python-mpv.pkg.tar.zst.sig \
  --destination "$XDG_DATA_HOME/showtime-libmpv-50-validation"
```

Use `${XDG_DATA_HOME:-$HOME/.local/share}` if XDG_DATA_HOME is unset. GPGv
uses `/usr/share/pacman/keyrings/archlinux.gpg` by default. A different trusted
system distributor keyring may be selected with `--keyring`; never trust a key
merely supplied alongside an archive. The installed Showtime 50.0 commit must
verify against its configured Flatpak origin through OSTree. Missing keys,
signatures, runtime files or unexpected archive contents fail closed. Existing
destinations are not overwritten. The manifest is local integrity evidence;
it is not a new distributor signature or a defense against an attacker who can
rewrite the user's entire runtime and manifest together.

For a later isolated Kunai playback session:

```sh
SHOWTIME_BACKEND=libmpv \
SHOWTIME_HOST_RUNTIME=/path/to/showtime-libmpv-50-validation \
  scripts/showtime-mpv-shim.py [the usual Kunai mpv arguments]
```

The bridge starts the genuine Showtime UI with session credentials in its
environment, not argv. `SHOWTIME_HOST_PYTHON` selects the host interpreter;
default `/usr/bin/python3`. `-s` excludes user site packages. The runtime must
pass file-set and SHA-256 checks each launch. A missing dependency is a launch
failure, never a silent fallback to a separate mpv window.

Rollback for subsequent launches: unset `SHOWTIME_BACKEND` or explicitly set
`SHOWTIME_BACKEND=gstreamer`. No installer/default association is changed.
The existing `ani-showtime-player` Flatpak wrapper is deliberately retained;
native rollout currently enters through the Kunai shim. The screenshot action
is disabled for native playback because upstream screenshots snapshot a
Gtk.Picture rather than the new GLArea.

## Automated validation

The repository has no compiled application target. Syntax compilation covers
all production scripts and tests. Run tests with a disposable interpreter and
isolated XDG paths, including Japanese test dependencies when available:

```sh
python -m compileall -q scripts tests
MEDIA_PLAYER_AI_TEST_PYTHON=/path/to/test-venv/bin/python \
XDG_CACHE_HOME=/tmp/showtime-test-cache \
XDG_CONFIG_HOME=/tmp/showtime-test-config \
XDG_STATE_HOME=/tmp/showtime-test-state \
  python -m pytest -q
```

Dependencies: pytest; for the existing study-subtitle integration test,
SudachiPy, SudachiDict-core, jamdict, jamdict-data and pykakasi. Its default
interpreter remains the existing media-player AI environment; the environment
override permits testing without modifying that installation.

Generate a disposable five-second AV fixture using ffmpeg, then run the three
opt-in real native tests with libmpv/python-mpv available:

```sh
ffmpeg -f lavfi -i testsrc2=size=320x180:rate=25 \
  -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -t 5 -c:v libx264 -pix_fmt yuv420p -c:a aac /tmp/showtime-av.mp4
python tests/native_engine_smoke.py /tmp/showtime-av.mp4
xvfb-run -a python tests/native_gtk_smoke.py /tmp/showtime-av.mp4
GDK_DISABLE=egl:vulkan xvfb-run -a python tests/native_gtk_smoke.py /tmp/showtime-av.mp4
xvfb-run -a dbus-run-session -- python tests/native_bridge_gtk_smoke.py /tmp/showtime-av.mp4
```

The engine test checks actual authenticated HTTP requests with comma-bearing
headers, byte ranges, audio/video tracks, external subtitle decoding without
source credentials, EOF/replay, same-URL credential removal and 403 reporting.
The GTK test checks real render calls, decoded audio/video tracks, pause,
volume, mute, rate, asynchronous seek, same-URL source replacement and cleanup.
The GTK bridge harness also checks the actual production adapter signals,
Kunai rendered readiness, pause acknowledgement, source revision and window
cleanup on private display and D-Bus sessions. These use a null audio sink.
They do not prove audible output,
Wayland rendering, or the complete Showtime UI.

## Required manual acceptance before changing the default

Use an isolated test session/display and private XDG directories. Do not input
into the user's shared desktop or stop an existing player. Provisioning and
full Showtime validation require the actual supported host ABI and trusted
package artifacts. Use a private D-Bus session during side-by-side testing to
avoid colliding with the live player's MPRIS name.

1. Launch genuine Showtime 50.0 under Wayland/EGL and X11 (EGL and GLX when
   available). Confirm one Showtime window and original controls/overlays;
   no mpv window, blank video, wrong scale, rotated image or excessive CPU.
2. Play local H.264/AAC, HEVC, AV1, audio-only, Matroska with multiple audio and
   subtitle tracks, and representative HLS/DASH sources. Compare each with
   the same host's native mpv using matching explicit options and headers.
   Unsupported DRM is a limitation, not evidence of general decoder failure.
3. Hear audio; verify device selection/default sink, mute, slider and keyboard
   volume, pause/resume, scrub, relative keys, exact/zero seeks, rate, looping,
   fullscreen and Escape. Test unknown-duration and nonseekable media.
4. Exercise original audio and embedded subtitle menus, local SRT/ASS/VTT,
   external controller subtitles, off, generated subtitles and study overlays.
   Check language preservation across reordered tracks and episodes. Confirm
   selecting a native track clears study overlays and supersedes pending
   controller downloads. Verify delayed old subtitle loads cannot attach.
5. Test MPRIS Get/GetAll, Play/Pause/PlayPause/Stop, Seek/SetPosition, Rate,
   Volume, fullscreen, metadata/title/length/position, Seeked and Next/Previous.
   Test the existing live player's name ownership before any normal-bus rollout.
6. Exercise authenticated Kunai IPC end to end: initial resume (including under
   five seconds), pause revisions, source revision with the same URL, seek
   during replacement, new headers/cookies, episode next/previous, subtitles,
   EOF/autoplay/cancel, stop, quit, errors and refresh/retry after 403/429.
7. Confirm readiness only after output and time advance; no old generation can
   send readiness, seek completion, error or EOF for its successor. Verify
   audio-only reports never claim a configured video output. Test stalled,
   interrupted and repeatedly replaced sources for several minutes.
8. Inspect explicitly enabled diagnostics for credentials using synthetic
   tokens. Check subtitle requests and redirected/CDN requests with disposable
   credentials; native mpv/FFmpeg's actual cross-origin policy needs observation.
   No generic credential-forwarding proxy may be added as a workaround.
9. Close during initial load, seek, buffering, source teardown and subtitle
   download. Check no leftover audio, core threads, timers or session sockets.
   Reopen and confirm logical resume. Test rollback in a fresh GStreamer session.

Record host/library versions, exact source types (without credentials),
expected/actual behavior and failures. Do not enable the native default until
all required cases pass. Full visual/native input, MPRIS and provider checks
remain release gates even when every headless test passes.

## Recorded local evidence — 2026-10-20

- Entire pytest suite: **98 passed**, including import-time assertions in the
  existing study-subtitle integration test. Python 3.13.5 in a disposable venv;
  no changes to the user's AI environment. Scripts/tests syntax compilation,
  provisioner compilation and `git diff --check` passed.
- Native HTTP smoke: passed real mpv 0.40.0, python-mpv 1.0.8 with the generated
  H.264/AAC fixture and private loopback server. Header values containing commas,
  credential-isolated native subtitles, EOF/replay, credential removal and real
  403 classification passed.
- Native GTK smoke: passed on GTK 4.18.6 using Xvfb/X11 **EGL**, with 18 frames
  rendered in the replacement source. Captured and visually inspected the
  actual 640×360 color-bar fixture: visible video, correct aspect and orientation.
  The screenshot is a temporary test artifact, not an upstream UI screenshot.
  A separate forced-GLX X11 run (`GDK_DISABLE=egl:vulkan`) also passed,
  rendering 17 frames in its replacement source.
- Production GTK-adapter/bridge harness: passed native frame readiness,
  audio/video stream counts, paused acknowledgement, new media revision,
  asynchronous source teardown/recreation and close cleanup.
- Host libraries were extracted from Debian repository packages into `/tmp`
  for tests; nothing was installed into the system. CUDA was unavailable;
  the tests decoded without it. GTK printed unavailable Vulkan/render-node
  diagnostics; the selected OpenGL rendering test still passed. Audible audio
  was deliberately not tested.
- Full Showtime 50 runtime/build validation is **blocked**: local libadwaita is
  1.7.6, below upstream's >=1.8.alpha requirement. The installed signed Showtime
  Flatpak and Arch package/signature/keyring are not present here, so real
  offline provisioning is tested with controlled signature-tool fixtures,
  not claimed as end-to-end desktop provisioning.
- Wayland, full Showtime control/input/MPRIS behavior, real HLS/DASH/provider
  coverage, multi-minute playback, host packaging on Python 3.14 and rollout
  remain unverified. The existing ani-cli wrapper stays on GStreamer.
- Connected tools do not expose Loom Working cards or SUMI project read/write
  capabilities. Visibility and SUMI synchronization could not be performed;
  this file and the local Git commit preserve the implementation evidence.

Upstream contracts checked against [Showtime 50.0](https://github.com/GNOME/showtime/tree/50.0),
[python-mpv](https://github.com/jaseg/python-mpv), and
[mpv's render API](https://github.com/mpv-player/mpv/blob/master/include/mpv/render.h).

Tested change inventory: `scripts/showtime-player.py`,
`scripts/showtime-mpv-shim.py`, `scripts/showtime_libmpv.py`,
`scripts/showtime_libmpv_gtk.py`, `scripts/showtime_host.py`,
`scripts/provision-showtime-libmpv`, `tests/test_showtime_libmpv.py`,
`tests/test_showtime_provision.py`, `tests/test_study_subtitles.py`, and the three
`tests/native_*_smoke.py` programs. Existing proxy and session regressions are
included in the full suite. `README.md` links setup/rollback to this record.
The feature commit containing this file is the recoverable implementation
checkpoint; no remote push or default change is part of it.
