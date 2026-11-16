# Automation support and roadmap

This is a focused, read-only comparison of audio, media, window/input, and
display automation patterns with the current CLI/MCP operations. Unrelated
scripts were out of scope, and no external script was run. The roadmap records
capability gaps; it does not promise that every legacy script should become a
built-in operation.

## Support matrix

| Garuda pattern | Current support | Gap / implication |
| --- | --- | --- |
| `Audio/*status.sh`: identify a named device, report mute state, poll for changes, or coordinate mic/speaker mute state | `audio list` discovers sinks, sources, defaults, volume/mute, and output streams. Typed operations set default, volume, explicit mute state, and route one stream. These are live-desktop-only. | No event subscription, status watcher, or coupled mute policy. A script can poll the JSON CLI, but the tool does not own a monitoring loop. |
| `Audio/pipewire-script*.sh`, `pw-savewires.sh`, `pw-loadwires.py`, `pw-dewire.sh`: inspect, save, restore, and remove PipeWire port links | Device and output-stream inspection plus single-stream routing are supported. | No arbitrary port/link graph inspection, saved routing profile, or graph-wide unlink. A future profile operation should preview and validate exact endpoints, then apply only the selected changes; do not expose a broad `dewire` action. |
| `Media/plaingplayer.sh`, `focusedplayer.sh`, and `skip-int-out*.sh`: find a playing MPRIS player, poll status/position, seek at configured thresholds, and associate playback with the focused window | MPRIS player list/inspect and typed transport requests are supported, including dedicated relative seek, bounded absolute seek-to, and a bounded wait for one selected player's playback status. Window records expose app ID, title, and PID. | No long-running playback watcher, configurable skip policy, or reliable MPRIS-player-to-window association. Player names and window IDs remain separate selectors. |
| `Media/*fullscreen*.sh`, `way-fulls*.sh`, and `Windows/*`: discover windows, test fullscreen/geometry, activate a target, manipulate pointer location, and enter/recover fullscreen | Structured `observe` returns windows/displays plus active-window and pointer context; frame geometry and explicit minimized, fullscreen, and maximized states can be set and verified by KWin readback. Bounded global pointer clicks/drags are supported. | No named-display placement abstraction or standalone pointer-move operation. Enabling fullscreen/maximized requires restoring minimized and conflicting state first. Explicitly activating, minimizing, restoring, or changing the state of a live window may affect the user's workspace. Global pointer actions remain best-effort and are not window-bound. |
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
2. **Keep input semantics explicit.** KWin fake input is compositor-routed and
    does not acknowledge app delivery or provide atomic window routing. Keep
    window-targeted input active-window guarded; global pointer operations use
    logical desktop coordinates and can move the pointer or change active focus.
    Drag pacing improves recognition for real UI gestures, but does not verify
    delivery. Do not claim focus preservation or test against the user's live
    desktop without explicit authorization.
3. **Media workflow primitives.** Dedicated relative seek and bounded
   player-status wait are available alongside absolute seek. A longer-running
   watcher and position-based thresholds remain future work. For player/window
   association, first define a trustworthy identity contract; do not guess from
   titles or pick whichever player happens to be playing.
4. **Safe audio profiles.** If graph patchbay workflows are in scope, add typed
   list/preview/apply operations over exact PipeWire ports, with a validated
   diff and per-link results. Keep one-stream routing separate and avoid
   unscoped graph reset/removal.
 5. **Explicit window state actions.** Minimize/restore, fullscreen/restore,
     maximize/restore, and frame-geometry placement use idempotent desired-state
     operations with bounded KWin readback. Prefer `set` operations over
     ambiguous toggles. Pointer position is read-only; pointer movement remains
     a separate future operation.
6. **User-visible virtual displays.** Consider a typed create/remove operation
   for an extra output attached to the existing KWin `user-desktop`. This extends
   the user's visible desktop; it is not the hidden output of an `agent-session`.
   Require explicit live-target selection, verify and report the created output,
   accept bounded requested dimensions, evaluate focus effects rather than
   promising focus preservation, and avoid rearranging physical outputs. Track
   ownership and only remove outputs created by this tool. Do not add Sunshine
   streaming, phone connection, or capture orchestration. Generic physical-output
   disable/mode/position changes remain separate and higher risk.
7. **Optional event/wait support.** Replace fragile shell polling only where a
   bounded, cancellable wait materially improves a workflow. Keep long-running
   watchers opt-in and outside basic list/inspect calls.

## Implemented: non-activating Screenshot2 capture

Full-desktop and exact-window screenshots use KWin's
`org.kde.KWin.ScreenShot2` API through the narrowly scoped
`kde-desktop-screenshot` Qt helper. `CaptureWindow` targets the UUID directly
without activating the window. KWin's restricted-interface authorization is
preserved through the helper's desktop entry; the Python process is not
authorized. The opt-in private-KWin integration test verifies valid image
metadata and unchanged focus while capturing an inactive window. Minimized and
occluded-window behavior and richer pixel/logical-coordinate scale mapping are
not yet guaranteed.

## Accessibility-provider probe and future UI control discovery

### Availability probe (2026-11-16)

The current user session has AT-SPI2 infrastructure installed and running:
`at-spi2-core` 2.62.1-1, the `org.a11y.Bus` session service, and
`at-spi2-registryd` were present. The `Atspi-2.0` GObject Introspection binding
was importable, `Atspi.get_desktop(0)` returned a desktop object, and KWin
registered `org.freedesktop.a11y.Manager`.

That confirms provider infrastructure, not usable control coverage. In a
read-only sample, KWin reported 7 windows while the AT-SPI desktop exposed 7
application roots; none had top-level accessible children, and none matched a
KWin window by PID. Thus no current window could be mapped to an accessible
control tree. This result is session- and application-dependent; it does not
prove AT-SPI is unavailable on other targets or that a future app cannot expose
a tree. No software was installed, no accessibility setting was changed, and
no semantic action was attempted. Do not advertise an accessibility capability
or expose semantic controls on the strength of a running bus alone.

A follow-up proof-of-concept used a GTK 4 test window in a fresh private KWin
session with `GTK_A11Y=atspi`. The desktop tool reported its UUID, app ID, PID,
and title; AT-SPI returned an application root with the same PID and a top-level
frame with the same title. The tree exposed a named button with a `click` action
and a labeled text field. The private session started an `org.a11y.Bus`, but its
registry did not start automatically; the test explicitly launched
`at-spi2-registryd` inside that private session before inspecting the tree. The
session was stopped after the test, and the button action was not invoked. This
proves a controlled GTK path can work when the app opts into the backend and the
registry is available; it does not demonstrate general application coverage or
automatic provider readiness for private sessions.

Control discovery is a possible future feature, not a committed implementation
plan. The current interface exposes windows, screenshots, and coordinate input,
but does not identify buttons, fields, menu items, or other controls within a
window. Possible sources for discovering them include:

- **Accessibility metadata (for example, AT-SPI):** provides semantic roles,
  accessible names, bounds, and sometimes supported actions. It is usually the
  strongest source when an application exposes a complete, trustworthy tree,
  but coverage and availability vary across applications and environments.
- **OCR over screenshots:** provides visible text and its location, including
  text missing from the accessibility tree. Text alone does not prove that a
  label is interactive, so OCR detections must not automatically be treated as
  controls.
- **Visual/icon detection:** may find unlabeled controls that neither the
  accessibility tree nor OCR describes. It can require additional models or
  image-processing dependencies and may confuse similar-looking UI elements.

These approaches could be combined for better coverage, for example by matching
OCR text boxes to nearby accessibility nodes and using visual detection for
remaining gaps. A combined pipeline also adds processing time, resource use,
dependencies, and implementation complexity. Each source can produce false
positives, and combining sources can introduce duplicate or incorrectly matched
candidates. Any implementation should be evaluated carefully on representative
applications and layouts for coverage, false-positive rate, and runtime cost;
it should retain source evidence, handle ambiguity conservatively, and never
assume a detected candidate is actionable without verification. This remains an
exploration item rather than a near-term commitment.

Any future accessibility adapter should be optional and target-scoped. It must
reliably map a fresh AT-SPI top-level window to the selected KWin window and
process identity before returning that window's tree; a PID alone is
insufficient when one process owns multiple windows, and a desktop-wide tree or
title-based guess is not an acceptable substitute. If exact mapping cannot be
proven, report the provider as unavailable for that window.
Element references should be opaque and short-lived, bound to the selected
session, window identity, and observation. Inspection should identify the source
and expose only bounded metadata needed to disambiguate elements. An action
should require one exact fresh reference, an action advertised by that element,
and a currently actionable state; stale, unsupported, or ambiguous targets must
fail closed. Read back the tree or independent application state afterward and
report request submission separately from verified effect. Keep coordinate
input available as an explicit fallback, but never silently convert OCR text or
visual detections into actionable controls. Semantic inspection and action
tools remain unimplemented until real per-application coverage and those safety
properties can be tested.

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
