# Showtime + libmpv: experimental video backend

**Status (October 20, 2026): working rendering proof, not production-ready.** No
changes to the user's existing Flatpak, window, default player, or settings.

## Why

GNOME Showtime 50.0 uses GStreamer/GstPlay. Native mpv has different HTTP,
adaptive playback, decoder, subtitle and recovery behavior. Trying to make all
GStreamer streams identical to native mpv is not a durable strategy.

The robust goal is to embed **libmpv into the original Showtime GTK4 window**,
with the native mpv render API drawing frames to GTK4 GLArea and retaining
Showtime's original GTK overlay and controls. No separate video window, no
Wayland-unsupported foreign-window embedding.

Upstream references:
- https://github.com/mpv-player/mpv/blob/master/include/mpv/render.h
- https://github.com/mpv-player/mpv-examples/blob/master/libmpv/README.md
- https://docs.gtk.org/gtk4/class.GLArea.html

## What was demonstrated on Mirai

1. Native GTK4 plus libmpv GLArea rendered 105 frames and advanced 4.7 seconds.
2. The genuine GNOME Showtime 50.0 Python UI ran in an isolated host sandbox
   using its unmodified upstream Python package, UI resources and GSettings
   schema copied from the user's installed Flatpak.
3. The video surface inside the real Showtime overlay was replaced with
   libmpv's GLArea; it rendered frames while Showtime's chrome remained.
4. A small initial adapter accepted play/pause, absolute seeking, and volume
   changes without a crash. Seeking/playback state synchronization is NOT
   correct yet; some UI-driven updates reset playback position.

## Files

- showtime_libmpv_area.py: prototype GLArea and incomplete GstPlay-facing
  adapter, intended only for isolated host smoke tests.
- gtk_glarea_smoke.py: standalone GTK4/libmpv rendering smoke. Requires
  python-mpv and PyOpenGL in Python's import path.

To run the smoke, generate a short MP4 using ffmpeg and launch the script
under a GTK display (Xvfb works). The final script argument is that MP4 path.

## Release blockers

- A packaged libmpv dependency: Showtime Flatpak DOES NOT ship libmpv. Host
  and Flatpak libraries must not be mixed without checking the ABI.
- Full GstPlay-to-mpv event adaptation: clock, progress, buffering, source
  generation, seeking, state transitions and quality switching.
- Playback control compatibility: pause, resume, volume, seek, shortcuts,
  video orientation and fullscreen.
- Actual subtitle/audio track selection, external subtitles, MPRIS and DRM
  boundary handling.
- Real Kunai IPC session lifecycle, source-switching, per-source HTTP headers
  and error classes, audio and video readiness signals.
- Stable multiple-minute live playback and automated device tests on Mirai
  (and Tachibana when it comes online).
- Opt-in rollout, a proven rollback path and no switching of the default
  until all regressions pass.

Do not install this prototype as production Showtime. The existing
GStreamer-based Flatpak remains unchanged.
