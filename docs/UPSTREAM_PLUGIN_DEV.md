# Native Caelestia plugin development migration

Experimental target: https://github.com/caelestia-dots/shell/pull/1703 (unmerged). This branch does not alter the original default branch.

- Identity: `dcqwqc/MediaPlayer`; shell version requirement: >=2.1.0.
- Entry-point types: none; external integration only.
- Settings: none; custom settings UI: none.
- The upstream development parser accepts custom, bar-entry, bar-popout, quick-toggle, status-icon, dashboard-tab. Only bar and quick toggles have completed host placements; `custom` and `properties.slot` do not automatically recreate private integrations.

## Before deployment
1. Test against an isolated PR #1703 build; inspect manifest/QML errors, all declared source paths, enable/disable/reload.
2. Verify settings persist, actual placement, backend and hardware behaviors.
3. Preserve previous installed plugin/configuration and rollback to the default branch if the shell is unstable.
4. Do not deploy without tested working UI and automatic rollback.

**State:** Manifest gated to the upstream development API; no live build or deploy validation yet.
