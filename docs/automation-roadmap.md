# Automation support and roadmap

This is a focused, read-only comparison of the audio, media, window/input, and
display automation patterns in `/home/marvin/Documents/Coding/Bash/Garuda/`
with the current CLI/MCP operations. Unrelated scripts were out of scope, and no
Garuda script was run. The roadmap records capability gaps; it does not promise
that every legacy script should become a built-in operation.

## Support matrix

| Garuda pattern | Current support | Gap / implication |
| --- | --- | --- |
| `Audio/*status.sh`: identify a named device, report mute state, poll for changes, or coordinate mic/speaker mute state | `audio list` discovers sinks, sources, defaults, volume/mute, and output streams. Typed operations set default, volume, explicit mute state, and route one stream. These are live-desktop-only. | No event subscription, status watcher, or coupled mute policy. A script can poll the JSON CLI, but the tool does not own a monitoring loop. |
| `Audio/pipewire-script*.sh`, `pw-savewires.sh`, `pw-loadwires.py`, `pw-dewire.sh`: inspect, save, restore, and remove PipeWire port links | Device and output-stream inspection plus single-stream routing are supported. | No arbitrary port/link graph inspection, saved routing profile, or graph-wide unlink. A future profile operation should preview and validate exact endpoints, then apply only the selected changes; do not expose a broad `dewire` action. |
| `Media/plaingplayer.sh`, `focusedplayer.sh`, and `skip-int-out*.sh`: find a playing MPRIS player, poll status/position, seek at configured thresholds, and associate playback with the focused window | MPRIS player list/inspect and typed transport requests are supported, including relative seek. Window records expose app ID, title, and PID. | No playback watcher, absolute seek, configurable skip policy, or reliable MPRIS-player-to-window association. Player names and window IDs remain separate selectors. |
| `Media/*fullscreen*.sh`, `way-fulls*.sh`, and `Windows/*`: discover windows, test fullscreen/geometry, activate a target, manipulate pointer location, and enter/recover fullscreen | Window list/find/inspect/activate and bounded wait for fullscreen or display-filling geometry are supported. Input can send a key, literal text, or a normalized click to an already-active selected window. | No typed fullscreen/maximize/placement action, pointer-position or movement API, or fullscreen-recovery workflow. The existing click operation is not a general-purpose pointer API. |
| `sunshine-virtual-display/*`: create and tear down an output for a streaming lifecycle | A managed private session creates its own virtual KWin output; `display list` reads logical display geometry. | No general live-output enable/disable, mode/position control, arbitrary display lifecycle, or Sunshine integration. Keep live display changes behind explicit ownership and target checks if added. |

The current operation contract and target model are described in
[`interface.md`](interface.md). CLI recipes and scripting patterns are in
[`usage.md`](usage.md) and [`examples.md`](examples.md).

## Direction

Keep one typed operation model shared by the CLI, Python API, and MCP server.
Private sessions remain the default for shared CLI commands; live desktop access
must always be an explicit target. MCP tools must expose that same target choice
and make unsafe or unsupported combinations hard to express in their schemas.

Prioritize improvements in this order:

1. **Discoverability and machine-readable contracts.** Put target placement and
   live-only restrictions in each relevant help level. Keep JSON output
   versioned, provide exact selectors and actionable errors, and put meaningful
   bounds/enums in MCP schemas. (This pass addresses these items.)
2. **Media workflow primitives.** Consider absolute seek and a bounded wait for
   a player/status/position condition. For player/window association, first
   define a trustworthy identity contract; do not guess from titles or pick
   whichever player happens to be playing.
3. **Safe audio profiles.** If graph patchbay workflows are in scope, add typed
   list/preview/apply operations over exact PipeWire ports, with a validated
   diff and per-link results. Keep one-stream routing separate and avoid
   unscoped graph reset/removal.
4. **Explicit window state actions.** Add desired-state operations such as
   fullscreen on/off, maximize/restore, or move/resize only after their KWin
   semantics and verification are defined. Prefer idempotent `set` operations
   over ambiguous toggles. Pointer movement can be considered separately.
5. **Display lifecycle.** Treat live output changes as a high-impact feature:
   require an explicit live target, report the requested/effective state, and
   provide a recovery path before exposing enable/disable or mode changes.
6. **Optional event/wait support.** Replace fragile shell polling only where a
   bounded, cancellable wait materially improves a workflow. Keep long-running
   watchers opt-in and outside basic list/inspect calls.

For every new operation, define target semantics, preconditions, selector
freshness, output fields, bounds, and post-action verification before adding the
CLI and MCP surfaces. Implement it once in the shared operation/backend layer;
do not add a second raw-command interface just to mirror an individual shell
script.
