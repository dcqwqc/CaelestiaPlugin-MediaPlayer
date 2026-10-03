# CaelestiaPlugin-PremiumMedia

A touch-first premium media surface for Caelestia. It adds a **Media+** dashboard tab that keeps Caelestia's native MPRIS, audio and brightness services instead of replacing them with a separate player stack.

## Media+

- album art with restrained palette-aware backdrop
- title, artist and album metadata
- touch-draggable seek bar with elapsed/remaining timing
- previous, play/pause, next, shuffle and repeat controls when supported by the active MPRIS player
- player selection when multiple MPRIS clients are available
- touch-draggable output volume and mute
- touch-draggable display brightness when an active monitor is available
- responsive compact layout and enlarged configurable touch targets
- all chrome comes from Caelestia `Colours`/`Tokens`; no hard-coded green, purple or black popup styling


## ani-cli / Showtime integration

PremiumMedia uses GNOME Showtime as the real `ani-cli` player on Mirai. The integration keeps Showtime's native GTK/libadwaita UI instead of skinning mpv.

`~/.local/bin/ani-cli` points to `scripts/ani-cli-showtime`, which transparently patches the system ani-cli player dispatch at runtime and preserves normal search, episode, quality, history and subtitle resolution. The system `/usr/bin/ani-cli` file is left untouched. If the packaged ani-cli script changes, the cached patched copy is regenerated automatically from the new system version.

Protected HLS streams are opened through `ani-showtime-player` and `ani-showtime-proxy.py`. The proxy binds only to `127.0.0.1`, injects ani-cli's required upstream referrer, rewrites the HLS playlist/segments locally, and exposes the subtitle track as HLS WebVTT. Showtime therefore retains native HLS seeking instead of receiving a non-seekable remux. The proxy exits automatically after playback becomes idle.

Showtime is also registered as the default app for common video/HLS MIME types. Its MPRIS interface exposes play/pause and seeking to Caelestia Media+.

### Fullscreen system controls

PremiumMedia does not draw a second fullscreen control surface. Fullscreen playback uses Caelestia's original OSD unchanged, so volume and brightness keep the native shell shape, stacking, icons and slider behavior.

### Stream reliability

The local HLS proxy stays alive for the complete Showtime process lifetime instead of trying to infer idleness from gaps between HLS requests. Upstream playlist, subtitle and segment requests are retried with backoff. Before Showtime opens, the launcher preflights the local HLS master with retries; a known-broken stream therefore fails in the launcher instead of opening Showtime directly on its generic `Unable to play video` screen. A new ani-cli episode also closes any stale Showtime application instance before handing over the new local stream.

HiAnime's current CDN disguises some MPEG-TS segments as `.ts.jpg` by prepending a valid 1×1 PNG plus junk bytes before the real 188-byte TS packet alignment. mpv tolerates that wrapper, but GStreamer's `hlsdemux2` rejects it with an internal data-stream error. The proxy detects the first stable TS sync alignment, strips the fake image wrapper, serves clean `video/mp2t`, and applies client Range requests in the cleaned byte space. Client-side connection cancellations are treated as normal adaptive-stream behavior rather than converted into false 502 errors.

Run `scripts/install-showtime-integration` to install/repair this integration.

## Theme polish

The optional **Match Hyprland chrome** setting applies the current Caelestia palette to Hyprland's active/inactive borders and shadows at runtime. It is intentionally non-destructive: no Hyprland config file is edited. Before its first change, the plugin snapshots the four affected runtime colours. Disabling or unloading it restores only those values, leaving unrelated runtime plugin settings untouched.

This fixes stale system chrome such as overly green/purple borders and shadows. It cannot recolor arbitrary third-party application windows that draw their own black dialogs; those need to be fixed in the owning application/theme. Every surface owned by this plugin itself is palette-derived.

## Settings

Nexus → Plugins exposes artwork backdrop/intensity, volume/brightness visibility, touch-target scale, Hyprland chrome sync, and shadow strength.

## Install

Clone or symlink this repository to:

```text
~/.local/share/caelestia/plugins/premium-media
```

Then enable **PremiumMedia** in Nexus → Plugins. The new **Media+** tab appears in the dashboard.

License: GPL-3.0-or-later.
