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
| `Media/plaingplayer.sh`, `focusedplayer.sh`, and `skip-int-out*.sh`: find a playing MPRIS player, poll status/position, seek at configured thresholds, and associate playback with the focused window | MPRIS player list/inspect and typed transport requests are supported, including relative seek and bounded absolute seek-to. Window records expose app ID, title, and PID. | No playback watcher, configurable skip policy, or reliable MPRIS-player-to-window association. Player names and window IDs remain separate selectors. |
| `Media/*fullscreen*.sh`, `way-fulls*.sh`, and `Windows/*`: discover windows, test fullscreen/geometry, activate a target, manipulate pointer location, and enter/recover fullscreen | Window list/find/inspect/activate and bounded wait for fullscreen or display-filling geometry are supported. Input can send a key, literal text, or a normalized click to an already-active selected window. | No typed fullscreen/maximize/placement action, pointer-position or movement API, or fullscreen-recovery workflow. The existing click operation is not a general-purpose pointer API. |
| `sunshine-virtual-display/*`: create/tear down an extra output on the user's existing KWin desktop, alongside Sunshine-specific streaming hooks | A managed private session creates its own hidden virtual KWin output; `display list` only reads logical geometry on the selected target. | Creating an additional **visible desktop-extension output** on the explicitly selected `user-desktop` is a project-fit feature, not streaming integration. It is not implemented yet. A future create operation should take bounded dimensions and verify/report the effective output; removal must be limited to outputs created and tracked by this tool. Phone/client connection, capture, and Sunshine lifecycle orchestration are out of scope. Generic physical-output disable/mode changes are separate, higher-risk features. |

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
2. **Media workflow primitives.** Absolute seek is now available through
   `media seek-to` / `kde_media_seek_to`. The relative seek still shares
   `media control` with transport actions, so its conditional `seconds` argument
   is clearer in help than in a generic JSON schema; consider a dedicated
   relative-seek operation next. Then consider bounded waits for player/status/
   position conditions. For player/window association, first define a trustworthy
   identity contract; do not guess from titles or pick whichever player happens
   to be playing.
3. **Safe audio profiles.** If graph patchbay workflows are in scope, add typed
   list/preview/apply operations over exact PipeWire ports, with a validated
   diff and per-link results. Keep one-stream routing separate and avoid
   unscoped graph reset/removal.
4. **Explicit window state actions.** Add desired-state operations such as
   fullscreen on/off, maximize/restore, or move/resize only after their KWin
   semantics and verification are defined. Prefer idempotent `set` operations
   over ambiguous toggles. Pointer movement can be considered separately.
5. **User-visible virtual displays.** Consider a typed create/remove operation
   for an extra output attached to the existing KWin `user-desktop`. This extends
   the user's visible desktop; it is not the hidden output of an `agent-session`.
   Require explicit live-target selection, verify and report the created output,
   accept bounded requested dimensions, avoid stealing focus or rearranging
   physical outputs, track ownership, and only remove outputs created by this
   tool. Do not add Sunshine streaming, phone connection, or capture
   orchestration. Generic physical-output disable/mode/position changes remain
   separate and higher risk.
6. **Optional event/wait support.** Replace fragile shell polling only where a
   bounded, cancellable wait materially improves a workflow. Keep long-running
   watchers opt-in and outside basic list/inspect calls.

## AI and scripting usability

- The CLI should keep global target placement visible at both group and leaf
  help levels. Use `--json` in scripts rather than parsing the human-readable
  tables, and pass the target explicitly in every multi-session workflow.
- MCP schemas should make valid choices and limits visible before a call. Prefer
  one operation per intent, required fields over action-dependent optional
  fields, and bounded waits over unbounded polling. The new absolute seek tool
  follows that pattern; relative seek remains a candidate for the same cleanup.
- Keep the discover → inspect → act → verify workflow predictable: exact listed
  selectors, versioned results, explicit `effect_verified` state, and useful
  structured errors. Never interpret a submitted input/media request as proof
  that the application changed state.
- Keep examples executable and test them with fake operations where possible.
  Script examples should pass argv arrays without `eval`, avoid guessing when
  selectors are ambiguous, and use bounded retries with a fresh state check.

For every new operation, define target semantics, preconditions, selector
freshness, output fields, bounds, and post-action verification before adding the
CLI and MCP surfaces. Implement it once in the shared operation/backend layer;
do not add a second raw-command interface just to mirror an individual shell
script.
