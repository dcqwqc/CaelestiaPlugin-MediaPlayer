# CaelestiaPlugin-MediaPlayer

A customized GNOME Showtime media-player integration for Mirai/Caelestia.

There is no separate dashboard media-control surface. The actual player is GNOME Showtime, extended by this repository for the playback behavior we want.

## What it does

- launches `ani-cli` episodes in GNOME Showtime
- attaches external English WebVTT subtitles through Showtime/GStreamer native subtitle support
- keeps Showtime seeking working for protected HLS streams
- remembers playback progress by logical title/episode and resumes on reopen
- automatically opens Showtime fullscreen for ani-cli playback
- cleans HiAnime/Zoro-style disguised MPEG-TS segments before GStreamer sees them
- preserves Showtime's normal GTK/libadwaita UI and subtitle controls
- registers Showtime as the default app for common video/HLS MIME types

## Architecture

`~/.local/bin/ani-cli` points to `scripts/ani-cli-showtime`. That wrapper patches ani-cli's player dispatch at runtime without modifying `/usr/bin/ani-cli`.

`scripts/ani-showtime-player` starts a localhost-only HLS bridge, launches the customized Showtime bootstrap in `scripts/showtime-player.py`, restores the saved position, and tracks progress through Showtime's MPRIS interface.

`scripts/ani-showtime-proxy.py` injects the source referrer, rewrites HLS URLs, strips fake image prefixes from disguised MPEG-TS segments, supports byte ranges, and serves external subtitles to Showtime.

Resume state lives under `~/.local/state/media-player/resume/`.

## Install

Run:

```bash
./scripts/install-showtime-integration
```

The installer ensures GNOME Showtime is present, points `~/.local/bin/ani-cli` at the wrapper, and registers Showtime for supported video/HLS MIME types.


## Opt-in embedded libmpv backend

`SHOWTIME_BACKEND=libmpv` selects the host-side backend through the Kunai
`scripts/showtime-mpv-shim.py` launcher. It embeds libmpv in the genuine
Showtime 50.0 GTK interface, uses original source URLs and native mpv/FFmpeg
network/decoder behavior, and retains the existing authenticated session bridge.
No separate mpv window is opened. The default remains GStreamer until full live
validation passes; `SHOWTIME_BACKEND=gstreamer` is the rollback switch.

Host dependencies and trusted Showtime resources/modules must be prepared first.
The offline nonroot `scripts/provision-showtime-libmpv` verifies package and
Flatpak signatures and creates a versioned runtime without changing the installed
Flatpak or desktop defaults. Native launch fails closed on missing dependencies.

See [runtime setup, test evidence and manual release gates](docs/libmpv-validation.md).
Wayland, audible output, the complete Showtime UI/MPRIS, real provider coverage,
and multi-minute Kunai playback still require scheduled desktop validation.
