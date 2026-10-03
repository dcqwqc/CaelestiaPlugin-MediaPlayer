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


## ani-cli / mpv player skin

PremiumMedia now also owns the actual player experience used by `ani-cli`, rather than only the Caelestia dashboard controller. `scripts/install-player-ui` installs uosc and Thumbfast when missing and then applies the bundled `mpv/` configuration.

The player disables mpv's legacy OSC and replaces it with a black/white proximity-based uosc surface: large touch-friendly transport buttons, a clean timeline, volume control, subtitle/audio access, fullscreen controls, restrained window chrome, and Thumbfast timeline previews. Controls disappear while watching and reveal near the relevant edge. The existing mpv MPRIS plugin is preserved so Media+ can control the same playback session.

On Mirai the player configuration lives in `~/.config/mpv`. A timestamped backup was created before the first migration.

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
