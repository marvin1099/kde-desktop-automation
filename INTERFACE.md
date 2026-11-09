# KDE desktop interaction interface — proposal

## Goal

Provide an AI-friendly, scriptable interface for inspecting and operating a KDE
Plasma session. The interface should expose useful desktop concepts—windows,
virtual desktops, displays, and input targets—instead of making callers compose
raw `dotool`, `kdotool`, and screenshot commands themselves.

The CLI is the first transport. A future MCP server should expose the same
operations and data types, not a separate set of semantics. The interface is a
capability-aware facade over KDE and system tools; it should not claim that an
operation is supported when the active session or installed tools cannot do it.

## Design principles

1. **Inspect before acting.** Make it easy to get a compact, structured view of
   the current session and then act on an object from that view.
2. **Target explicitly.** A window, desktop, display, and whole session are
   different targets. Never silently interpret a window-relative point as a
   screen coordinate.
3. **Return useful context.** Include geometry, coordinate spaces, capabilities,
   and action results so an AI can decide what to do next.
4. **Be safe around ambiguity.** If a title or selector matches multiple
   windows, return the matches and ask the caller to choose. Do not guess.
5. **Keep backend details behind adapters.** Native window IDs and tool-specific
   commands are useful diagnostics, but are not the main public interface.
6. **Make capabilities visible.** Report whether the session is Wayland or X11,
   which operations are available, and why any are unavailable.
7. **Report the real input context.** Keyboard focus, pointer location, and a
   requested target are not necessarily the same thing. Show all three.
8. **Verify transitions.** Desktop actions are not transactions; report partial
   completion and verify important postconditions instead of assuming success.

## Prototype status

The current CLI has a working isolated-session slice: create/list/show/stop a
private virtual KWin session, inspect its output, capture a screenshot, launch a
process into its private Wayland and D-Bus environment, and submit key, text, or
click events through that session's KWin fake-input protocol. Every action names
the managed session; none falls back to the live desktop. Input is best-effort:
KWin can ignore it and gives no authorization or application-delivery
acknowledgement, so receipts report submission and leave effect verification to
a fresh screenshot or other independent observation.

Window enumeration and window-specific focus/targeting are still unavailable in
the standalone compositor. The current launch command accepts an executable and
argument vector (never a shell string) and is not a security boundary; a future
user-facing app launcher should prefer validated desktop-entry IDs. Real-user-
desktop attachment and input remain separate, unimplemented capabilities.

## Session model and isolation

The interface should treat the **compositor session** as the outermost scope.
A Wayland display socket (for example, `WAYLAND_DISPLAY=wayland-99`) selects a
compositor connection; it does not create a monitor. Outputs are owned and
created by the compositor/backend. There is no portable Wayland command that
means “add output 99”; virtual-output creation is compositor- and version-
specific.

Keep these modes distinct:

| Mode | What it means | Isolation |
| --- | --- | --- |
| `user-desktop` | The user's normal KWin/Plasma session and physical outputs | Shared with the user; input, focus, clipboard, and windows can be affected |
| `shared-virtual-output` | An extra/headless output attached to that same KWin session | Visually separate, but may still share KWin focus and the input seat; not sufficient to promise the user can keep playing undisturbed |
| `agent-session` | A separate KWin/Wayland session with its own hidden output, window tree, input route, and capture | Preferred default; use a restricted OS identity/sandbox and explicit file access if actual security isolation is required |
| `exclusive-remote` | A remote session that takes over/locks the host display while connected | Useful for privacy, but displaces the local user's display session instead of running alongside them |

The intended default is exactly a private display that the agent can explore
while the user continues using the physical desktop—for example, moving their
mouse and playing a game without the agent stealing focus or sending input to
the game. To guarantee that, the agent display needs its own compositor/session
and input route (or a verified KWin per-seat mechanism). An extra output in the
user's existing KWin session is not enough by itself: it may still share global
keyboard focus, session services, clipboard, and input devices. The agent input
adapter must be bound to the agent session, not the host's physical-session
`dotool` device.

The agent should use `agent-session` by default and must never fall back to
`user-desktop` just because its private session is unavailable. Every window,
display, desktop, screenshot, and input reference belongs to a `session_ref`;
commands either name the session or use the configured isolated default. An
observation should report the isolation properties (separate compositor,
physical-screen visibility, input route, OS identity, and filesystem scope),
not just a vague `isolated: true` flag.

An explicit user-desktop mode is still valuable. It should require an explicit
attach/authorization step, use the compositor's supported remote-control or
input-emulation mechanism, show that the live user session is being controlled,
and return to the isolated session on detach. Permissions should be scoped and
revocable. A hidden output alone is not enough to claim isolation: applications
may still share focus, clipboard, session services, and input with the user's
desktop.

### Candidate KDE implementations

Upstream Plasma 6.8 documents KRDP **Exclusive mode**, which can lock the
server's screen and provide a virtual screen matching the remote client's
window. The KDE developer's description also says the physical host screens are
removed for the remote session and the user's windows are restored to their
prior positions after logging back in. This is a strong candidate for a
private/remote mode, but should be treated as exclusive access, not assumed to
be a concurrently running second desktop. A local RDP client could be explored
as the transport, subject to testing authentication, capture, input routing,
and whether local use is actually displaced.

Plasma 6.8's screen-sharing portal can also create a configurable virtual
screen. That is useful for capture/streaming, but by itself does not create a
separate user session. KWin's nested/virtual backend is another candidate for a
headless agent compositor; test it with its own Wayland socket and D-Bus session
before relying on it for a full Plasma desktop. Plasma's per-screen virtual
desktops organize windows but are not a security boundary either.

The private KWin experiment verified output capture, keyboard/text/click
submission, and a Kate save/read-back workflow. A separately and explicitly
scoped manual test also confirmed that this machine's live KWin v6 delivered
text and Ctrl+S to a dedicated Kate test file; this was not exposed as a CLI
host-desktop mode and does not establish a portable authorization guarantee.
KWin still provides no delivery acknowledgement, so every live-desktop action
must be independently verified and fail safely if ignored. When the user
explicitly asks the agent to operate their real desktop, attach to
`user-desktop` as a separate, visible, authorized mode; return to the private
session afterward. Never “borrow” the user's mouse to control the private
display.

## Public concepts

### Targets

Every operation that acts on a desktop object takes a target. Initial target
kinds:

| Kind | Examples | Typical operations |
| --- | --- | --- |
| `session` | isolated agent session or explicitly attached user session | inspect, capabilities, active window, pointer position |
| `window` | active window, a window reference, a selector | inspect, focus, move, resize, minimize, close, input, screenshot |
| `desktop` | current or a virtual desktop | inspect, switch to, list its windows |
| `display` | connector/name or a display reference | inspect, screenshot, configure where supported |
| `region` | a rectangle in an explicit coordinate space | screenshot, input |

An observation returns an opaque `ref` for each object. References are intended
to be used across the agent's next few commands, not persisted indefinitely.
Every observation should include a `snapshot_id` and `observed_at` time. The
implementation must define how a reference remains resolvable across separate
CLI processes (for example, a session-local reference cache or a verifiable
reference that includes its backend identity); a random in-memory-only handle
is not sufficient. Actions should revalidate the target and return
`stale_target` if identity or relevant geometry has changed. The response may
also include the backend's native ID for debugging. Never silently retarget a
stale reference to a different window with the same title.

Selectors may use exact IDs, the active window, app ID, or title. Exact IDs and
references resolve to at most one object. Fuzzy title/app matching returns all
matches unless the caller explicitly opts into a unique-match policy.

### Geometry and coordinate spaces

Window geometry should include its frame rectangle, content rectangle when
available, owning display, and virtual desktop. Use a consistent logical-pixel
coordinate system with a top-left origin; allow negative desktop coordinates
for displays placed left of or above the primary display. Include display scale
and transform information where available.

Input points must name their space:

- `window`: normalized `x` and `y` from 0 to 1, relative to the window content
  area. This is the preferred AI-facing form.
- `display`: normalized point relative to a named display.
- `desktop`: logical desktop pixels.
- `virtual-desktop`: normalized point across the bounding canvas of all enabled
  displays. This is a backend coordinate space, not the preferred AI-facing
  space.
- `screen-percent`: deprecated compatibility alias for `virtual-desktop`; never
  interpret it as percentages local to one display.

The caller should not have to convert a window point to a global screen point.
The input adapter uses fresh window/display geometry to do that conversion and
reports the resolved point. If the window moved between observation and action,
re-resolve the target and recompute. If the backend cannot perform this safely,
return an explicit unsupported/error result rather than click an approximate
location.

### `dotool` pointer mapping and state

The public interface should use logical desktop coordinates from display
discovery, not physical-pixel screenshot coordinates. In particular, `dotool`
`mouseto` is absolute over the **virtual canvas spanning all displays**: its
`x` and `y` are normalized from 0 to 1 across the full desktop bounds, not
relative to the selected monitor. Use the logical `x`, `y`, `width`, and
`height` reported by `wlpdisplays` to compute the bounding rectangle of enabled
outputs. Convert a logical point `(x, y)` to dotool coordinates as:

```text
u = (x - virtual_left) / virtual_width
v = (y - virtual_top) / virtual_height
```

For a display-relative or window-relative point, first resolve it to logical
desktop coordinates, then apply this conversion. Refresh display geometry after
monitor changes. If the resulting point is in a gap between outputs or outside
the target's current bounds, fail or ask for a new observation; do not silently
send it to a nearby display. Account for negative display origins, scaling, and
rotations using the logical geometry returned by the display adapter.

Do not treat `mouseto` like an idempotent “set cursor here” operation. dotool's
absolute tablet-style device remembers its **last commanded absolute position**
separately from the desktop cursor. If a user moves the real mouse while the
tablet remains at `(0.5, 0.5)`, sending `mouseto 0.5 0.5` again may emit no
movement, leaving the real pointer where the user moved it. Therefore:

1. Query the actual pointer position when possible. If it is already at the
   requested point, do not move it before clicking.
2. Track the tablet's last commanded normalized position separately from the
   actual pointer position; do not infer one from the other. Serialize access
   to this state and invalidate/reconcile it after backend restarts or external
   dotool use.
3. If the requested absolute point is the same as the tablet's last command but
   the real cursor is elsewhere, force a distinct tablet event (for example, a
   one-logical-pixel safe nudge followed by the requested point) and verify the
   resulting cursor location. Never click during the intermediate nudge. If no
   safe distinct point exists or the pointer cannot be verified, return an
   explicit failure instead of clicking at a possibly wrong location.
4. Use relative `mousemove` only as a controlled fallback. It can be clipped or
   caught at a screen corner, so check the actual final pointer position and
   report any shortfall; do not assume the requested delta was fully applied.

The input result should include the requested target point, resolved logical
desktop point, normalized virtual-canvas point sent to dotool, actual pointer
position before and after, last absolute tablet command (if known), whether a
nudge or relative fallback was used, and whether the final position was
verified. If actual pointer position or tablet state cannot be queried or
reconciled, surface that limitation through `capabilities` and avoid claiming
precise target-directed input.

## CLI shape

Use a single command with noun/verb subcommands, human-readable output by
default, and a versioned JSON mode for agents. The command name below is
illustrative (`kde-ai`; choose a final name separately).

Every command should accept an optional global `--session SESSION`. If omitted,
it uses the configured private `agent-session`; it must never silently fall
back to `user-desktop`. Session management could look like:

```text
kde-ai session list
kde-ai session create --kind agent --display 1920x1080
kde-ai --session agent-default observe --format json
kde-ai session attach --target user-desktop
```

Attaching to `user-desktop` should trigger the required authorization and make
the shared-session scope visible in subsequent results.

```text
kde-ai observe [--target session|window:REF|desktop:REF|display:REF] [--format json]
kde-ai observe --include interaction-context [--format json]
kde-ai window list [--desktop current|all] [--format json]
kde-ai window get --target REF [--format json]
kde-ai window focus --target REF
kde-ai window move --target REF --to DISPLAY --at 0.2,0.1 --space display --anchor top-left
kde-ai window resize --target REF --size 1000x700
kde-ai window minimize --target REF
kde-ai desktop list [--format json]
kde-ai desktop switch --target REF
kde-ai display list [--format json]
kde-ai display get --target REF [--format json]
kde-ai display configure --target REF --mode 2560x1440@60 --dry-run
kde-ai input click --target window:REF --at 0.5,0.5 --space window
kde-ai input type --target window:REF --text 'search query'
kde-ai input key --target window:REF --keys Ctrl+L
kde-ai input scroll --target window:REF --at 0.5,0.5 --delta 0,-3 --space window
kde-ai screenshot --target window:REF --output /tmp/window.png
kde-ai wait --until window:REF.focused --timeout 3s
kde-ai capabilities [--format json]
# Optional, capability-conditional operations:
kde-ai application launch --desktop-id org.kde.kate
kde-ai notification dnd --duration 45m
```

`--target` is required for actions where the destination could otherwise be
unclear. `active` can be a convenient window alias. Moving the pointer or
clicking by global coordinates requires an explicit display/desktop target and
coordinate space. Destructive actions such as closing a window should be
separate commands, not hidden side effects of input or selection.

Window placement needs both a coordinate space and an anchor: `--at 0.2,0.1`
alone is ambiguous. For example, the command above means “put the window's
top-left corner at 20% across and 10% down the target display.” Placement should
also report any compositor constraints or clamping.

An optional `--dry-run` should be supported for actions that can be planned
without changing the session. It returns the resolved target and intended
effect. It must not be presented as a guarantee that the desktop will remain
unchanged between planning and execution.

An action may accept `--observed-in SNAPSHOT` to bind its target and
preconditions to a recent observation. The CLI should reject an expired or
invalidated snapshot rather than applying an action to a different object.
`wait` predicates should be bounded and limited to observable state (for
example, a window becoming focused or a display mode becoming active); they
should return the latest observation on timeout, not silently continue.

## Structured data

JSON output should be versioned and have a consistent envelope. For example:

```json
{
  "schema_version": "1",
  "ok": true,
  "snapshot_id": "snapshot:s-1042",
  "observed_at": "2026-11-09T19:53:00Z",
  "data": {
    "ref": "window:w-7f31",
    "native_id": "0x0480000a",
    "app_id": "org.kde.konsole",
    "title": "projects — Konsole",
    "state": ["active", "maximized"],
    "geometry": {
      "frame": {"x": 120, "y": 80, "width": 1280, "height": 900},
      "content": {"x": 120, "y": 110, "width": 1280, "height": 870},
      "space": "desktop-logical-pixels"
    },
    "display_ref": "display:d-1",
    "desktop_ref": "desktop:desk-2",
    "capabilities": ["focus", "move", "resize", "screenshot", "input"],
    "modal": false,
    "transient_for": null
  },
  "warnings": []
}
```

Session observations should expose a separate input-context object. For
example:

```json
{
  "active_window_ref": "window:w-7f31",
  "active_desktop_ref": "desktop:desk-2",
  "interaction_context": {
    "keyboard_focus_ref": "window:w-7f31",
    "pointer": {
      "x": 2040,
      "y": 520,
      "space": "desktop-logical-pixels",
      "display_ref": "display:d-2"
    },
    "input_backend": "dotool",
    "routing": "global-seat",
    "seat": "seat0",
    "device_state": {"last_mouseto": {"x": 0.5, "y": 0.5}},
    "modal_window_ref": null
  }
}
```

This deliberately shows keyboard focus on one display and the pointer on
another. Mouse and keyboard injection have different routing rules; the result
must say what the operation actually did, not just repeat the requested target.

Lists should include a `data` array and be deterministic where possible. Action
responses should identify the target, report the requested and resolved action,
and include any resulting state that can be verified. Input responses should
also report the actual keyboard-focus window, pointer position/display before
and after, whether the action activated or focused a window, the input backend,
and whether the postcondition was verified. A requested `window` target does
not prove that the compositor delivered input there. Errors should have a
machine-readable `code`, a human-readable `message`, and optional `details`;
examples include `ambiguous_target`, `stale_target`, `unsupported`,
`permission_denied`, and `backend_unavailable`. Reserve stderr and non-zero exit
codes for failures; JSON mode should still emit the error envelope.

Screenshots should normally be written to a requested path and return that path
plus capture metadata (target, geometry, timestamp, coordinate space). Avoid
embedding large image data in normal JSON responses. A later MCP transport can
return image content using MCP's image content type while preserving the same
metadata. For visual automation, the agent must actually receive the image, not
just a path it cannot read. Include image pixel dimensions, crop/decorations
policy, and a transform between screenshot pixels and the target's logical
coordinates so points chosen from an image can be mapped correctly.

## Example agent loop

```sh
kde-ai observe --format json
kde-ai window list --format json
kde-ai screenshot --target window:w-7f31 --output /tmp/konsole.png
kde-ai input click --target window:w-7f31 --at 0.5,0.5 --space window
kde-ai observe --target window:w-7f31 --format json
```

The normal workflow is **observe → choose a target → act → verify**. The CLI
should not require an agent to scrape human-oriented output or know a KWin
script's details to do this.

## Backend mapping

Treat each external utility as an adapter that contributes capabilities to the
public model:

| Adapter/tool | Likely responsibility |
| --- | --- |
| `kdotool` and its KWin script mode | Window enumeration/properties, focus, desktop/window operations, and other KWin-specific queries/actions |
| `wlpdisplays` | Display discovery and monitor information |
| `kdocktor` | Display configuration/settings changes, where supported |
| KWin fake-input Wayland protocol | Session-bound key/text/click submission for supported private KWin sessions; best-effort with no app-delivery acknowledgement |
| `dotool` | Potential explicitly authorized live-desktop input only; its host-wide uinput device must not be used for private-session actions |
| Spectacle | Screen/display/window capture when the installed version supports the requested target |

The exact command options and available actions should be detected and verified
against the installed versions. Adapters should report their versions and
session constraints in `capabilities`; the public interface should avoid
depending on a specific tool's naming or ID format.

Window metadata is the foundation for target-aware interaction. If a backend
only supplies global pointer percentages, combine it with fresh window and
display geometry to translate a window-relative point. If geometry, scale, or
target identity is uncertain, fail clearly rather than claiming a precise
window click. Keep the KWin-script bridge narrow and typed: invoke known
operations with validated arguments, not arbitrary script text supplied by an
AI.

## Capabilities and future accessibility

The first milestone can use window metadata plus coordinates. A capability
report should make the current boundary clear, for example:

```json
{
  "session_type": "wayland",
  "backends": {"windows": "kdotool", "input": "dotool", "screenshots": "spectacle"},
  "operations": ["window.list", "window.focus", "input.click", "input.type", "screenshot.window"],
  "limitations": ["ui_element_tree_unavailable"]
}
```

Coordinate automation alone is brittle. A later accessibility adapter should
expose an optional UI element tree (role, accessible name, bounds, actions,
parent/child relationships) and permit targets such as a button by accessible
name. Keep that additive: tools without accessibility support must still offer
window-level inspection, screenshots, and coordinate input. OCR/vision can
also be layered above screenshots without changing window/display semantics.

## MCP mapping

Expose the same operations as typed tools, for example `kde_observe`,
`kde_window_list`, `kde_window_focus`, `kde_input_click`,
`kde_screenshot`, and `kde_capabilities`. Schemas should reuse the CLI's target
and coordinate-space types. Do not create an MCP tool that accepts an arbitrary
shell command; tool execution should go through validated operations and
structured results. Keep CLI and MCP as thin transports over one shared
operation layer.

## Suggested implementation order

1. Build `capabilities`, `observe`, `window list/get`, and `display list` with
   structured output, input context, snapshot IDs, and references that actually
   work across CLI invocations.
2. Add focus, desktop switching, window move/resize, screenshots, and bounded
   waits; verify results by re-reading state and return screenshot images to the
   agent rather than only returning paths.
3. Add target-relative click, type, key, and scroll operations with explicit
   coordinate conversion, focus/delivery reporting, and stale-geometry checks.
4. Add display settings as a separate, higher-impact operation family, with
   validation and a way to restore the prior configuration where possible.
5. Add accessibility plus narrowly scoped Plasma/app operations (such as DND
   and app launch) only where safe, testable providers exist; add MCP as another
   transport over the same operation layer.

This order delivers useful desktop awareness early, while postponing fragile
coordinate input and monitor reconfiguration until their target and safety
contracts are established.

## Stress test: prepare a bug-triage workspace

Consider this request:

> Prepare my KDE desktop for triaging bug 123456. Keep the code editor and
> terminal on the laptop display and put the Firefox window with that bug on the
> external display. Mute Plasma notifications for 45 minutes. In the bug page,
> set the status to In Progress and add the note “Reproduced on Wayland.” Do not
> discard unsaved work. Take a screenshot of the final layout. If the page or a
> dialog is ambiguous, stop and ask me.

This is intentionally more than a sequence of clicks: it combines desktop
discovery, ambiguous app windows, cross-display placement, user-facing input,
an external side effect in a web app, notification policy, and a no-data-loss
constraint.

### How an agent should approach it

1. **Inspect the whole session before acting.** Request a compact observation
   containing session type, displays and their layouts/scales, virtual desktops,
   windows, modal/transient relationships, and input context. The input context
   must say which window has keyboard focus, where the pointer is (including
   which display), which input backend is active, and how that backend routes
   input. “Current display” is not a sufficient field: on a multi-display
   desktop, keyboard focus and pointer display can differ.
2. **Resolve the requested windows.** Find editor and terminal windows by app
   ID, and candidate Firefox windows by app ID/title. If multiple Firefox
   windows match, do not choose the active one just because it is active. Take
   screenshots of the candidates and inspect the page identity. A screenshot
   result must be delivered to the model as image content (or through a host
   image-reading tool), not merely returned as an inaccessible filesystem path.
3. **Check the safety constraint.** Window metadata cannot generally tell
   whether an editor has unsaved changes. The agent should not infer “saved”
   from the title bar. It can leave windows open and avoid close/discard actions;
   if it must interact with a save prompt, the dialog is a stop-and-ask case.
4. **Plan the layout.** Use explicit display references and display-relative
   placement with a named anchor. Keep the editor and terminal on the laptop
   output, place Firefox on the external output, and do not change display mode
   unless the user asked for that. Re-read window geometry after moves because
   KWin may clamp, maximize, or otherwise constrain placement.
5. **Check what can be automated before touching the ticket.** If a UI element
   tree is available, find the exact bug page and named controls and invoke
   supported actions. Without it, inspect a fresh screenshot, click using
   screenshot-to-window coordinate mapping, and verify after every meaningful
   step. Do not type into “the Firefox window” until the interface confirms
   which window actually owns keyboard focus; a modal dialog or browser prompt
   may have taken it. If the page identity or control is not clear, stop as
   requested.
6. **Treat the ticket update as an external side effect.** The user explicitly
   authorized the requested status and note. Still verify the exact bug ID and
   read back the resulting status/comment. A successful click or keystroke is
   not proof that the web application accepted the change.
7. **Handle notification policy as its own capability.** Check whether a
   supported Plasma Do Not Disturb action exists. If not, report it as
   unsupported instead of guessing at a settings UI. Do not silently claim the
   request is complete because the other steps succeeded.
8. **Capture and report the final state.** Observe again, verify placement and
   ticket state, capture the requested desktop screenshot, and report each
   requested item as completed, failed, or not attempted—with reasons. The
   screenshot should be attached to the response when the transport supports
   images.

### Example interaction trace

The exact output is backend-dependent, but the intended shape is roughly:

```sh
kde-ai observe --include displays,desktops,windows,interaction-context,capabilities --format json
kde-ai window list --desktop all --format json
kde-ai screenshot --target window:firefox-candidate-1 --output /tmp/firefox-1.png
kde-ai screenshot --target window:firefox-candidate-2 --output /tmp/firefox-2.png
# Agent inspects both images and selects the one showing bug 123456.
kde-ai window move --target window:editor --to display:laptop --at 0.05,0.05 --space display --anchor top-left
kde-ai window move --target window:terminal --to display:laptop --at 0.05,0.52 --space display --anchor top-left
kde-ai window move --target window:firefox-bug-123456 --to display:external --at 0.05,0.05 --space display --anchor top-left
kde-ai observe --target session --format json
# If an accessibility provider exists, use its named controls; otherwise use
# a fresh screenshot and explicit window-relative clicks, verifying each step.
kde-ai screenshot --target window:firefox-bug-123456 --output /tmp/bug-page.png
# ...select the status control, verify the chosen status, enter the note,
# submit, then re-observe and verify the page state...
kde-ai observe --target session --format json
kde-ai screenshot --target session --output /tmp/triage-layout.png
```

This trace exposes an important boundary: the current CLI proposal has no
application-level control for Plasma notification mode, no reliable UI-element
tree, and no explicit “wait until the page reflects this state” operation.
Those should be reported as capability gaps rather than disguised as generic
click support. The task may therefore finish partially: layout and screenshot
can succeed while DND or the ticket update remains unverified.

## Gaps exposed and proposed refinements

The scenario surfaces several places where a simple `target + click` model is
not enough:

| Gap | How an agent can trip | Refinement |
| --- | --- | --- |
| Input context is implicit | Agent assumes the active window or pointer display is the target; keyboard input lands in a modal or another app | Add `interaction_context` to observations and before/after input results: focused window, pointer coordinates and display, seat/backend, routing model, and modal owner |
| Window references cross CLI calls | An opaque random ref from one process cannot be resolved by the next process, or a reused native ID points at a different window | Define reference lifetime and cross-process resolution; include `snapshot_id`, revalidate identity, and return `stale_target` rather than retargeting |
| Similar windows/tabs | Two windows share app ID/title; the agent guesses which contains the requested page | Return all candidates and use a screenshot or accessible page metadata to disambiguate; never default to active window |
| Screenshot is only a path | Agent cannot inspect the image, or clicks based on a resized/cropped image map to the wrong coordinates | Deliver image content to the agent and include pixel dimensions, crop/decorations, and pixel-to-logical-coordinate mapping |
| Focus and delivery are not guaranteed | `--target window:X` sounds direct even though `dotool` sends to the globally focused window | Define whether input activates/focuses first; report actual focus and delivery route, and fail if the intended target cannot be confirmed |
| No readiness/wait contract | Agent clicks a control before a dialog/page transition completes, or uses arbitrary sleeps | Add bounded `wait --until <observable predicate> --timeout ...`; after timeout return the latest state/screenshot rather than continuing blindly |
| UI semantics are missing | Coordinates break after layout changes; “click status” has no stable meaning | Add an optional accessibility provider with element refs, roles, names, bounds, and supported actions; retain screenshot/coordinate fallback |
| GUI actions are not transactional | A workflow can move two windows and then fail on the third step, leaving a partial layout | Have the agent plan, execute one checkpoint at a time, verify, and report a per-step outcome. Add compensating actions only where safe; do not promise atomic rollback |
| Capabilities stop at tool plumbing | DND, launching known apps, or other Plasma settings are unavailable even though they are common automation requests | Add narrowly scoped, typed application/Plasma operations (for example launch by desktop-entry ID and notification mode with duration), each advertised only when supported; do not expose arbitrary shell execution |
| Changes have different risk | Changing resolution or submitting a ticket update is more consequential than taking a screenshot | Classify operations by impact, support dry-run for settings changes, preserve prior values where possible, and require confirmation when the user has not clearly authorized a consequential action |

For multi-step tasks, the agent should maintain a small plan with each step's
precondition, action, expected postcondition, and result. A useful action result
therefore includes `verified: true|false`, the observed postcondition, and any
partial side effects. A blanket `ok: true` for “the click was injected” must not
be confused with “the requested desktop or application state was achieved.”

## Other useful automation families

Once the basic target and verification model works, common requests could be
served by additional typed capabilities rather than increasingly elaborate
coordinate scripts:

- **Workspaces:** launch an installed application by desktop-entry ID, collect
  its new window, arrange a named set of windows across virtual desktops, and
  optionally restore a saved layout. Never launch an arbitrary shell command.
- **Presentations and focus time:** enable Do Not Disturb for a duration,
  arrange presentation windows, and restore the previous notification/display
  state afterward. Make changes reversible and report whether restoration
  succeeded.
- **Display setup:** list connectors, modes, scale and layout; preview a mode
  change; apply only a supported configuration; and provide a timed rollback
  for changes that could make the desktop unusable.
- **Meeting setup:** inspect the selected audio output/input and volume, switch
  devices or mute where a reliable system API exists, then report the effective
  device. Do not infer microphone state from a window title or an unverified
  keypress.
- **File dialogs:** identify open/save dialogs as modal windows, expose the
  selected path and filename when accessible, and operate within user-approved
  locations. A dialog should not be mistaken for the parent application's
  main window.
- **Troubleshooting snapshots:** gather display/window/session metadata and
  selected screenshots into a concise report, with explicit controls over
  sensitive content and whether images are attached.

These are candidates, not promises that KDE exposes every operation in every
session. `capabilities` should list typed operations and limitations, while
privacy-sensitive or high-impact operations (clipboard reads, audio capture,
locking/logging out, display-mode changes) should be opt-in and clearly
identified in plans and results.

### Optional integration notes

These are future, capability-conditional adapters—not part of the current
prototype or required for the core desktop interface:

- **PipeWire:** explore typed audio-device/routing operations and, where the
  installed portals and policy support it, audio or camera/video capture.
  Microphone and camera use must be explicit, visibly reported, and subject to
  the platform's permission flow; never infer that capture is available merely
  because PipeWire is installed.
- **`playerctl`:** optionally inspect supported players and expose current
  metadata plus transport controls (play/pause, next, previous, seek). If more
  than one player is available, require or return an explicit player target.
- **Plasma settings:** add narrowly scoped, typed settings operations only where
  a supported API exists. Report the effective value, provide dry-run and
  restoration where practical, and do not expose arbitrary shell or raw config
  file editing as a settings API.
