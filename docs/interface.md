# KDE Desktop Automation — interface and design

## Goal

Provide a scriptable interface for inspecting and automating a KDE Plasma
session. It should expose useful desktop concepts—windows, virtual desktops,
displays, and input targets—instead of making callers compose raw `dotool`,
`kdotool`, and screenshot commands themselves. MCP makes the same operations
available to AI agents as one of several client types.

The CLI and stdio MCP server expose the same target-aware operations and data
types, not separate semantics. The interface is a capability-aware facade over
KDE and system tools; it should not claim that an operation is supported when
the active session or installed tools cannot do it.

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

The CLI has a working isolated-session slice: create/list/show/stop multiple
private virtual KWin sessions, inspect outputs, capture screenshots, launch
processes into each session's private Wayland and D-Bus environment, and submit
key, text, or click events through session-bound KWin fake input. KWin's private
Plasma window-management protocol also provides window UUIDs, title/app ID,
geometry, fullscreen/active state, and activation requests when protocol v13+
is available. The live desktop uses kdotool/KWin D-Bus for corresponding window
operations. Shared commands select either target with `--session ID` or the
explicit `--session user-desktop`; without `--session`, `KDE_DESKTOP_SESSION` may pin
a private target, otherwise operations use the configured private default. An
unset/stopped private target never falls back to the live desktop.

The CLI's environment and persisted defaults are process-wide/shared selectors,
not per-agent ownership. MCP desktop tools always require an explicit `session`
argument; environment and persisted defaults do not apply to MCP calls. Session
IDs are not authorization tokens: clients under one server share the same
Unix-user authority over session lifecycle.

The current shared subset includes window list/find/wait/inspect/activate,
targeted key/text/normalized click input, display list, screenshots, per-target
capability reports, and MPRIS player list/inspect/control. Media operations use
the selected target's D-Bus session and exact player names; they require
`playerctl` and do not infer a player when several are present. Playback
controls include relative seek and a separate bounded absolute seek-to operation.
The current live-desktop subset also includes PipeWire device/default inspection,
verified default selection, volume/mute, and single-stream output routing. Audio
controls require an explicit `user-desktop` target and a PipeWire Pulse
compatibility server; private KWin sessions do not provide an isolated audio
server. The launch commands accept executable argument vectors (never shell
strings) and are not security boundaries; a future user-facing launcher should
prefer validated desktop-entry IDs. Both backends' input remains best-effort:
KWin gives no application-delivery acknowledgement, so verify effects
independently. X11, accessibility targets, and semantic app-level actions remain
unimplemented.

MPRIS inspection returns playback status, available title/artist/album, current
position, and duration. Missing optional metadata is `null`; controls report
request submission, not verified playback effect. Absolute seek is bounded to
24 hours and is a request, not verified playback effect. Player volume, PipeWire
device selection, and per-application audio routing are not implemented.

Targeted input currently means **active-window guarded**, not compositor-atomic
per-window routing. Key, text, and click requests require the target to already
be active; an inactive target is rejected and is not automatically activated.
Explicitly activating an inactive live window changes user focus and may switch
virtual desktops. Whether KWin offers a reliable focus-independent delivery path
is unresolved and must be evaluated; do not promise that live input preserves
focus. If it is not supported, controlling an inactive window requires explicit
activation and may steal focus. Input delivery is not acknowledged. Private
window records retain frame and content geometry separately. The live KWin D-Bus
path exposes its reported geometry basis rather than asserting that it is
identical to the private content geometry.

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
| `shared-virtual-output` | An extra virtual output attached to the user's existing KWin desktop; it extends the same visible desktop | Not a separate session: windows and desktop content may appear there, while KWin focus, input, clipboard, and session services remain shared |
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

A user-visible shared virtual output is a distinct use case: it adds screen area
to the user's existing desktop rather than hiding a separate desktop from them.
It can be useful when the user independently arranges a remote client or another
display consumer. That connection/streaming path is not a goal of this tool; the
potential feature is creating, verifying, tracking, and safely removing the
additional KWin output itself, with bounded requested dimensions and a report of
the effective geometry. Evaluate and report any focus effects; do not promise
focus preservation before it has been tested. Avoid changing physical outputs.

The agent should use `agent-session` by default and must never fall back to
`user-desktop` just because its private session is unavailable. Every window,
display, desktop, screenshot, and input reference belongs to a `session_ref`;
commands either name the session or use the configured isolated default. An
observation should report the isolation properties (separate compositor,
physical-screen visibility, input route, OS identity, and filesystem scope),
not just a vague `isolated: true` flag.

The current CLI makes each live operation explicit with `--session user-desktop`
and revalidates the active Wayland/KWin environment on each invocation; it does
not persistently attach. A future persistent attach mode should have a visible,
revocable authorization lifecycle. A hidden output alone is not enough to claim
isolation: applications may still share focus, clipboard, session services, and
input with the user's desktop.

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

For a shared virtual output, the Garuda `sunshine-virtual-display` scripts
separate output creation from Sunshine connection management and describe
KRFB's virtual-monitor helper (a compositor-level output) and EVDI (a DRM
connector) as alternative backends. These are implementation candidates, not
current support commitments. The project goal would be the explicitly selected
live-desktop output lifecycle only; Sunshine setup, streaming, and phone/client
connection remain outside scope. This must stay separate from `agent-session`,
whose virtual output is hidden and belongs to its own compositor.

Private KWin's Plasma window protocol and live KWin's kdotool/D-Bus path now
provide matching window metadata and activation commands; both targets use the
same CLI syntax and selected-session Wayland socket for input. Opt-in tests
exercise private Kate window targeting and read-only live window discovery on
this machine. A prior manual test also confirmed live KWin v6 delivered text and
Ctrl+S to a dedicated Kate test file, but neither that nor a server round trip
establishes a portable authorization guarantee. KWin provides no
application-delivery acknowledgement; verify meaningful live actions from a
fresh screenshot or other app state. `--session user-desktop` is the visible
opt-in; private `agent-session` remains the default and never falls back.

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

**Current prototype limitation:** private input uses content geometry when
KWin provides `client_geometry`, otherwise frame geometry. Live input uses the
geometry returned by KWin D-Bus, and reports `geometry_basis: "kwin-dbus-reported"`.
Until the live adapter can provide and verify content bounds, normalized clicks
are not guaranteed to hit the same semantic point across the two targets.

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

Use noun/verb subcommands, human-readable output by default, and a versioned JSON
mode for agents. Target selection is shared across operation groups:

```text
kde-desktop session create --name agent-a --default --json
kde-desktop window list --json                           # private default only
kde-desktop --session agent-0123456789ab window list --json
kde-desktop --session user-desktop window list --json
kde-desktop --session user-desktop window find --app-id org.kde.kate --json
kde-desktop --session user-desktop window wait --app-id org.kde.kate --timeout 10 --json
kde-desktop --session agent-0123456789ab window inspect WINDOW_UUID --json
kde-desktop --session user-desktop window activate WINDOW_UUID --json
kde-desktop --session user-desktop input key WINDOW_UUID CTRL+END --json
kde-desktop --session user-desktop input type WINDOW_UUID $'\nTODO' --json
kde-desktop --session user-desktop input click WINDOW_UUID 0.8 0.9 --json
kde-desktop --session agent-0123456789ab display list --json
kde-desktop --session user-desktop screenshot --output /tmp/desktop.png
kde-desktop --session user-desktop capabilities --json
```

The first created private session becomes the default. `session use ID` changes
it, and `session create --default` selects a new one. Selection precedence is
`--session ID`, then private-only `KDE_DESKTOP_SESSION`, then the configured default.
Additional agents should pass their session ID explicitly or receive a
per-agent environment; omission never chooses the most recently created
session. If no running private target is configured, shared commands fail with
setup guidance rather than targeting another desktop. The old
`session key/type/click/screenshot` and `user-desktop ...` command forms remain
compatibility aliases while scripts migrate.

```text
kde-desktop session list
kde-desktop session create --name agent-b --width 1920 --height 1080
kde-desktop session use agent-0123456789ab
```

The global `--session` selector is parsed before the operation group. Both
targets revalidate the calling process's Wayland context on each invocation;
`user-desktop` is never stored as the private default and does not have a
persistent attach mode. The command syntax matches, while target scope and
capabilities remain explicit in result envelopes.

The following block is a **proposed future CLI sketch, not runnable with the
current implementation**. In particular, `observe`, window placement, display
configuration, window screenshots, and semantic notification/application
commands shown here are not implemented. See “What the current implementation
can do” below for the executable subset.

```text
kde-desktop observe [--target session|window:REF|desktop:REF|display:REF] [--format json]
kde-desktop observe --include interaction-context [--format json]
kde-desktop window list [--desktop current|all] [--format json]
kde-desktop window get --target REF [--format json]
kde-desktop window focus --target REF
kde-desktop window move --target REF --to DISPLAY --at 0.2,0.1 --space display --anchor top-left
kde-desktop window resize --target REF --size 1000x700
kde-desktop window minimize --target REF
kde-desktop desktop list [--format json]
kde-desktop desktop switch --target REF
kde-desktop display list [--format json]
kde-desktop display get --target REF [--format json]
kde-desktop display configure --target REF --mode 2560x1440@60 --dry-run
kde-desktop input click --target window:REF --at 0.5,0.5 --space window
kde-desktop input type --target window:REF --text 'search query'
kde-desktop input key --target window:REF --keys Ctrl+L
kde-desktop input scroll --target window:REF --at 0.5,0.5 --delta 0,-3 --space window
kde-desktop screenshot --target window:REF --output /tmp/window.png
kde-desktop wait --until window:REF.focused --timeout 3s
kde-desktop capabilities [--format json]
# Optional, capability-conditional operations:
kde-desktop application launch --desktop-id org.kde.kate
kde-desktop notification dnd --duration 45m
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

`window wait` currently supports a bounded wait for a unique app-ID/title
match. Future `wait` predicates may include other observable state (such as a
window becoming focused or fullscreen); they should return the latest
observation on timeout, not silently continue. An action may later accept
`--observed-in SNAPSHOT` to bind target and preconditions to a recent observation
and reject stale IDs or geometry.

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
plus capture metadata (target, geometry, timestamp, coordinate space). The
current shared screenshot command captures the full selected output, not an
individual window. Avoid
embedding large image data in normal JSON responses. A later MCP transport can
return image content using MCP's image content type while preserving the same
metadata. For visual automation, the agent must actually receive the image, not
just a path it cannot read. Include image pixel dimensions, crop/decorations
policy, and a transform between screenshot pixels and the target's logical
coordinates so points chosen from an image can be mapped correctly.

## Example agent loop

```sh
kde-desktop observe --format json
kde-desktop window list --format json
kde-desktop screenshot --target window:w-7f31 --output /tmp/konsole.png
kde-desktop input click --target window:w-7f31 --at 0.5,0.5 --space window
kde-desktop observe --target window:w-7f31 --format json
```

The normal workflow is **observe → choose a target → act → verify**. The CLI
should not require an agent to scrape human-oriented output or know a KWin
script's details to do this.

## Backend mapping

Treat each external utility as an adapter that contributes capabilities to the
public model:

| Adapter/tool | Likely responsibility |
| --- | --- |
| KWin's Plasma window-management Wayland protocol | Private-session window UUIDs, metadata, state, geometry, and activation requests, scoped to that private Wayland socket |
| `kdotool` plus KWin's `org.kde.KWin.getWindowInfo` D-Bus method | Live-user window enumeration, properties, and activation, scoped to the explicit user target |
| `wlpdisplays` | Display discovery and monitor information |
| `kdocktor` | Display configuration/settings changes, where supported |
| KWin fake-input Wayland protocol | Session-bound key/text/click submission for either target when its protocol version supports the operation; best-effort with no app-delivery acknowledgement |
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

The stdio server exposes typed tools over `DesktopOperations`: `kde_window_list`,
`kde_window_find`, `kde_window_wait`, `kde_window_inspect`,
`kde_window_activate`, `kde_input_key`, `kde_input_type`, `kde_input_click`,
`kde_display_list`, `kde_media_list`, `kde_media_inspect`, `kde_media_control`,
`kde_audio_list`, `kde_audio_set_default`, `kde_audio_set_volume`,
`kde_audio_set_mute`, `kde_audio_route_stream`, `kde_screenshot`, and
`kde_capabilities`. Private lifecycle tools cover
list/create/show/use/stop/launch. Every MCP desktop tool requires a
`session` field containing a managed ID or explicit `user-desktop`; CLI defaults
do not apply. `kde_session_use` changes only the persisted CLI default. The
server instructions repeat the selector rule and shared-UID lifecycle trust
model to MCP clients. Domain errors retain stable error codes and structured
details; both transports use a `schema_version` 1 envelope, and screenshots
return PNG image content alongside target metadata.

No MCP tool accepts arbitrary shell commands. Launch is limited to argv in a
managed private session, while window targeting, focus checks, normalized
coordinates, capability reporting, and input receipts use the same operation
layer as the CLI.

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
5. Add accessibility plus narrowly scoped Plasma/app operations (such as DND)
   only where safe, testable providers exist; expose them in both transports.

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

### What the current implementation can do

The principles above are the safe way to approach the task, not a claim that the
current CLI can complete it. The implemented read/interaction subset is
capability-scoped: list/find/wait/inspect/activate windows, list displays,
capture a **full selected output** screenshot, and submit active-window-guarded
key/text/click input. Use an explicit target in scripts. For a shared MCP server,
pass the private session ID on each call; pass `user-desktop` only when explicitly
choosing the physical desktop.

Run these commands in the same interactive Bash shell, replacing the example
session ID and app IDs with values from the selected target. Set
`TARGET='user-desktop'` only when explicitly opting into the physical desktop;
otherwise use a managed private session ID. Inspect every result. A `window
find` ambiguity is an error with candidate metadata, not a selection. The
screenshot is the whole output, not a per-window capture. In MCP,
`kde_screenshot` returns that image as content; the CLI writes it to the path.
For a private target, first create a running session and launch any apps inside
it; the commands below cannot see host windows. Its home/browser profile is
separate from the host's, so do not assume the ticket site is already signed in.

```sh
TARGET='agent-0123456789ab'
kde-desktop --session "$TARGET" capabilities --json
kde-desktop --session "$TARGET" display list --json
kde-desktop --session "$TARGET" window list --json
kde-desktop --session "$TARGET" window find --app-id org.kde.kate --json
kde-desktop --session "$TARGET" window find --app-id org.kde.konsole --json
kde-desktop --session "$TARGET" window find --app-id org.mozilla.firefox --title 123456 --json
kde-desktop --session "$TARGET" screenshot --output "/tmp/kde-desktop-triage-before-$$.png" --json
```

After identifying one exact window and reviewing a fresh screenshot, these are
the current input command forms. The UUID and coordinates are placeholders, not
values to paste literally. For a ticket update, separately locate and verify the
status control, the intended status option, the note field, and the submit
control from fresh screenshots; the example below only shows syntax and cannot
verify that any control or page state is correct.

```sh
kde-desktop --session "$TARGET" window inspect FIREFOX_WINDOW_UUID --json
kde-desktop --session "$TARGET" window activate FIREFOX_WINDOW_UUID --json
# Illustrative syntax only. Reinspect after each UI transition and replace each
# coordinate with a point measured for the currently visible control.
kde-desktop --session "$TARGET" input click FIREFOX_WINDOW_UUID STATUS_CONTROL_X STATUS_CONTROL_Y --json
kde-desktop --session "$TARGET" input click FIREFOX_WINDOW_UUID IN_PROGRESS_OPTION_X IN_PROGRESS_OPTION_Y --json
kde-desktop --session "$TARGET" input click FIREFOX_WINDOW_UUID NOTE_FIELD_X NOTE_FIELD_Y --json
kde-desktop --session "$TARGET" input type FIREFOX_WINDOW_UUID 'Reproduced on Wayland.' --json
# Verify the note/status visually before deciding to submit:
kde-desktop --session "$TARGET" input click FIREFOX_WINDOW_UUID SUBMIT_X SUBMIT_Y --json
kde-desktop --session "$TARGET" screenshot --output "/tmp/kde-desktop-triage-after-$$.png" --json
```

These commands do **not** make the requested workflow achievable end-to-end:

| Part of request | Live `user-desktop` | Hidden `agent-session` |
| --- | --- | --- |
| Inspect windows/displays and capture a screenshot | Yes, when live Wayland/KWin prerequisites are available; reads the real desktop | Yes, scoped to the private session and its virtual output |
| Put windows on laptop and external displays | Not implemented: current CLI has no window move/placement operation | Not possible in this prototype: the managed session has its own virtual output, not the host's laptop/external displays; move is also unimplemented |
| Change Plasma Do Not Disturb | Unsupported | Unsupported |
| Update the ticket | Only generic coordinate input is available; no accessibility or app-level operation, and app state must be verified independently | Only if Firefox is launched and the page is available/authenticated in this session's separate profile; it does not operate on the host's existing Firefox window |
| Preserve/verify unsaved work and ticket state | Unsaved state and app-level postconditions are not exposed | Same limitation; avoid closing windows and independently inspect the app |

Thus the current safe result is a partial assessment: the agent can report
observations, activate a confirmed window, and attempt carefully verified
input. It must report layout changes and DND as unsupported, and stop if the bug
page or control is ambiguous. A successful input receipt is not proof that the
ticket was updated. The shell trace is a current CLI example, **not** a promise
that the original multi-display triage request can be completed.

## Gaps exposed and proposed refinements

The scenario surfaces several places where a simple `target + click` model is
not enough:

| Gap | How an agent can trip | Refinement |
| --- | --- | --- |
| Input context is partial | Results identify the intended and observed active window, but not pointer location/display, modal ownership, or focused child control | Add `interaction_context` with pointer coordinates/display and modal/seat context; distinguish active-window focus from widget focus |
| Window references can go stale | UUIDs resolve across CLI calls in one session, but a window may close or its geometry/state may change after observation | Return `stale_target` for closed IDs and optionally bind actions to a recent `snapshot_id` or expected identity/geometry |
| Similar windows/tabs | `window find` rejects ambiguous window matches and returns candidates, but browser tabs inside one window are not identified | Keep failing closed on ambiguity; add browser/accessibility metadata only through an explicit provider rather than guessing from title |
| Screenshot mapping is incomplete | MCP delivers image content and CLI writes a path, but neither offers per-window crops or a complete pixel/logical transform for every scale/decoration case | Include screenshot dimensions, target geometry basis, display scale/transform, and an explicit crop-to-window mapping where supported |
| Focus and delivery are not guaranteed | KWin fake input checks the named window against active focus but is routed through compositor focus; focus can change during submission and KWin may ignore events | Preserve the no-implicit-activation rule for input; report focus before/after and submission status, and require independent postcondition checks |
| UI readiness is limited | `window wait` supports unique app/title matches plus KWin fullscreen or display-filling geometry, but cannot wait for a page, dialog, or control to become ready | Add bounded, typed observable UI-state predicates where a reliable provider exists; return the last observation on timeout |
| UI semantics are missing | Coordinates break after layout changes; “click status” has no stable meaning | Add an optional accessibility provider with element refs, roles, names, bounds, and supported actions; retain screenshot/coordinate fallback |
| GUI actions are not transactional | A workflow can change several controls and then fail, leaving partial state | Have the agent plan, execute one checkpoint at a time, verify, and report a per-step outcome. Add compensating actions only where safe; do not promise atomic rollback |
| Capabilities stop at tool plumbing | Managed sessions can launch argv, but launch-by-desktop-entry, DND, and other typed Plasma/app operations are unavailable | Add narrowly scoped typed operations only when supported; do not expose arbitrary shell execution |
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

- **PipeWire capture and advanced routing:** add permission-gated audio capture
  through an installed portal where supported, plus richer stream identity and
  routing workflows. Current device and stream controls affect live user audio,
  so they require an explicit live target, enumerate streams before routing, and
  verify the effective state. PipeWire being installed does not imply that
  capture is authorized.
- **Plasma settings:** add narrowly scoped, typed settings operations only where
  a supported API exists. Report the effective value, provide dry-run and
  restoration where practical, and do not expose arbitrary shell or raw config
  file editing as a settings API.
