# KDE Desktop Automation — CLI and MCP usage

A CLI and Python toolkit for observing and automating KDE Plasma desktops. It
also provides an MCP server for AI agents. The CLI and MCP server share one
target-aware operation layer. Private hidden desktops are the default; accessing
the live desktop always requires the explicit `user-desktop` target.

Use `KDE_DESKTOP_SESSION` to select a private session by default.

## Current prototype

The first vertical slice creates a **private virtual KWin compositor** using
KWin's virtual framebuffer backend. It gets its own Wayland socket, private
D-Bus session, runtime directory, and application home. The user can keep
working on the physical desktop while this session is inspected or screenshotted.

```bash
uv sync
uv run kde-desktop doctor
uv run kde-desktop session create --name agent --width 1280 --height 800
uv run kde-desktop session list
uv run kde-desktop session displays SESSION_ID
uv run kde-desktop session screenshot SESSION_ID --output /tmp/agent.png
uv run kde-desktop session launch SESSION_ID --json -- kate --new /tmp/agent-note.txt
uv run kde-desktop session type SESSION_ID 'Hello from kde-desktop'
uv run kde-desktop session key SESSION_ID CTRL+S
uv run kde-desktop session screenshot SESSION_ID --output /tmp/agent-after.png
uv run kde-desktop session stop SESSION_ID
```

`session create` prints the generated ID. The isolated session is not a
security sandbox: it runs as the current Unix user, though with a private home
and session bus. Do not run untrusted applications in it.

`kde-desktop doctor` reports host prerequisites without enumerating or injecting into
the live desktop. To probe live window/input capabilities, explicitly run
`kde-desktop --session user-desktop capabilities`.

`session launch` passes the executable and arguments directly (no shell) and
records the launched process for session cleanup. The legacy `session key`,
`type`, and `click` commands keep their original current-focus/output-pixel
semantics. Use the shared `--session ... window` and `input` commands below when
you want to inspect and explicitly target a specific window.

Input receipts report the target session, the submitted event count, and whether
the Wayland server round trip completed. They deliberately set
`effect_verified: false`: KWin's fake-input protocol may ignore requests and does
not acknowledge authorization or delivery to an application. Verify meaningful
effects from a fresh screenshot or other independent state. The `doctor` and
session capability reports expose these limits; `input.effect_ack` is always
false. Layout-aware key chords and Unicode text currently require KWin's v6
fake-input protocol; the session capability report reflects the version that
session actually advertises.

### One command surface, multiple sessions

Window, input, screenshot, display, and capability commands use the same
syntax for a private hidden desktop and the real desktop. Pass a managed private
session ID, or explicitly pass `user-desktop` to opt into the physical desktop:

```bash
# The first created private session becomes the private default.
uv run kde-desktop session create --name agent-a --width 1280 --height 800 --json
uv run kde-desktop window list --json

# Create additional independent private desktops; name the target in scripts.
uv run kde-desktop session create --name agent-b --json
uv run kde-desktop --session agent-0123456789ab window list --json

# Real desktop access is never inferred: select it on each command.
uv run kde-desktop --session user-desktop window list --json
uv run kde-desktop --session user-desktop window find --title Kate --json
uv run kde-desktop --session user-desktop screenshot --output /tmp/desktop.png
```

`session create --default` makes a newly created private session the default;
`session use SESSION_ID` changes the private default. Target selection precedence
for shared commands is: explicit `--session`, then `KDE_DESKTOP_SESSION`, then the
configured private default. The environment variable can contain a managed
private session ID only; setting it to `user-desktop` is rejected. Use
`--session user-desktop` on each command to opt into the live desktop.

For a persistent selection in a shell or an agent process environment, set:

```bash
export KDE_DESKTOP_SESSION=agent-0123456789ab
kde-desktop window list --json
kde-desktop input key WINDOW_UUID CTRL+S --json
```

An explicit `--session` overrides the environment. If the selected private
session is missing or stopped, commands fail rather than switching sessions or
falling back to the live desktop. For concurrent agents, give each agent its
own environment value or, especially when sharing an MCP server, pass each
managed session ID explicitly. Session lifecycle commands (`session create`,
`list`, `show`, `use`, `stop`, and `launch`) remain under `session`.

`KDE_DESKTOP_SESSION` and `session use` select process-wide/persisted defaults; they
are not per-agent ownership mechanisms. Concurrent session creation now
serializes the first-default claim, but shared MCP clients should still pass
explicit IDs instead of changing or relying on that default.

The common window commands are:

```bash
kde-desktop --session "$TARGET" window list --json
kde-desktop --session "$TARGET" window find --app-id org.kde.kate --title notes --json
kde-desktop --session "$TARGET" window wait --app-id org.kde.kate --title notes --timeout 10 --json
kde-desktop --session "$TARGET" window inspect WINDOW_UUID --json
kde-desktop --session "$TARGET" window activate WINDOW_UUID --json
kde-desktop --session "$TARGET" input key WINDOW_UUID CTRL+END --json
kde-desktop --session "$TARGET" input type WINDOW_UUID $'\nTODO' --json
kde-desktop --session "$TARGET" input click WINDOW_UUID 0.92 0.90 --json
kde-desktop --session "$TARGET" display list --json
kde-desktop --session "$TARGET" screenshot --output /tmp/after.png --json
kde-desktop --session "$TARGET" capabilities --json
```

`window find` takes an exact `--app-id` and/or a case-insensitive `--title`
substring. It returns candidates as an error if more than one window matches;
it never chooses silently. Inspection returns title, app ID, fullscreen,
minimized/maximized and active state, geometry, and a UUID scoped to that
session. UUIDs become stale when their window closes. The private KWin protocol
now supports the same window list/find/inspect/activate flow when the session
advertises protocol v13 or newer; capabilities are recorded per session.

`input key`, `input type`, and `input click` require a window UUID and require
that window to already be active. Activate it explicitly first. Clicks use
normalized window-relative coordinates from 0 to 1. Private KWin uses content
geometry when available; the live target reports the geometry basis supplied by
KWin D-Bus, so inspect `geometry_basis` before assuming the bounds are identical.
Both use KWin fake input routed through compositor focus: the window ID is
validated and checked active, but delivery is not atomically bound to that
window and focus can change during submission. KWin can also ignore events; a
round trip is not proof the app received them. Receipts report this routing and
leave `effect_verified: false` until a fresh screenshot or independent app/file
state confirms the result.

#### Targeted player example

This example requires an explicit target. A private session ID selects a hidden
KWin session; set `TARGET=user-desktop` only when you intentionally want to
operate on the physical desktop. The app must already be running in that target.
For a private target, launch it there first with `session launch`.

The workflow is deliberately split: inspect a fresh full-output screenshot and
confirm the target/control before running the input phase. Run both phases in
the same interactive Bash shell so the selected target and window ID remain set.
`kde-desktop screenshot` returns a file path; use the MCP screenshot tool if the agent
needs the image delivered as image content.

```bash
# Set these explicitly in the same interactive Bash shell for both phases.
# Replace the examples with a running session ID and the app's exact ID.
TARGET='agent-0123456789ab'
APP_ID='org.example.Player'

# Phase 1: inspect. Requires jq.
set -euo pipefail

CAPABILITIES="$(kde-desktop --session "$TARGET" capabilities --json)"
jq -e '
  .data.capabilities["window.find"] == true and
  .data.capabilities["window.activate"] == true and
  .data.capabilities["input.click"] == true
' <<<"$CAPABILITIES" >/dev/null || {
  echo "The selected target does not support the required window operations." >&2
  exit 2
}

WINDOW_JSON="$(kde-desktop --session "$TARGET" window find --app-id "$APP_ID" --json)"
jq -e '.data.window.fullscreen == true' <<<"$WINDOW_JSON" >/dev/null || {
  echo "The unique matching window is not fullscreen; no input sent." >&2
  exit 2
}
WINDOW_ID="$(jq -er '.data.window.window_id' <<<"$WINDOW_JSON")"
BEFORE="/tmp/kde-desktop-player-before-$$.png"
kde-desktop --session "$TARGET" screenshot --output "$BEFORE" --json
printf 'Inspect %s and window %s before continuing.\n' "$BEFORE" "$WINDOW_ID"
```

Only after confirming the screenshot and calibrating coordinates for this
specific target, set `CLICK_X` and `CLICK_Y` in the same shell, then run the
second phase. The block intentionally stops if they are unset. Do not assume
private and live geometry have identical bounds.
For live use, set `TARGET='user-desktop'` explicitly and authorize the resulting
focus change before continuing.

```bash
: "${CLICK_X:?Set CLICK_X to the calibrated normalized x coordinate}"
: "${CLICK_Y:?Set CLICK_Y to the calibrated normalized y coordinate}"
ACTIVATION="$(kde-desktop --session "$TARGET" window activate "$WINDOW_ID" --json)"
jq -e '.data.active_window_verified == true' <<<"$ACTIVATION" >/dev/null
INPUT="$(kde-desktop --session "$TARGET" input click "$WINDOW_ID" "$CLICK_X" "$CLICK_Y" --json)"
printf '%s\n' "$INPUT"
AFTER="/tmp/kde-desktop-player-after-$$.png"
kde-desktop --session "$TARGET" screenshot --output "$AFTER" --json
printf 'Inspect %s or other app state to verify the effect.\n' "$AFTER"
```

`window find` fails on no match or ambiguity rather than selecting a window
silently. Window input is active-focus-guarded, not atomically routed to the UUID;
the click receipt remains `effect_verified: false`. Inspect the post-action image
or independent app state. There is no accessibility tree for controls such as
“Play All,” so if the UI or target is ambiguous, stop instead of guessing.

#### Compose GUI operations into scripts

`window wait` can wait for either KWin's reported fullscreen state or a window
whose logical geometry covers a display (useful for borderless/fake-fullscreen
games). The latter requires display inspection support and allows a 2-pixel
edge tolerance. Both conditions still require one unique app-ID/title match;
unknown fullscreen state and ambiguous matches never count as success.

The example wrapper waits in bounded 60-second intervals, re-inspects the same
window before starting, and `exec`s the timer command as argv (no `eval`). This
is one example of composing the CLI; scripts can use the same window wait,
inspect, activation, input, and screenshot operations for other GUI workflows.
See [script examples and commented snippets](examples.md) for patterns
that wait for windows, check state, submit an action, and verify separately.
Pass an exact app ID discovered from `window list`; `user-desktop` must be
supplied explicitly to monitor the physical desktop. If more than one window
has that app ID, pass `--title` and a distinguishing title substring; the
wrapper fails on any remaining ambiguity:

```bash
bash ../examples/start-timer-on-fullscreen.sh \
  agent-0123456789ab org.example.Game fullscreen -- speedrun-timer --start
bash ../examples/start-timer-on-fullscreen.sh \
  user-desktop org.example.Game fills-display -- speedrun-timer --start
bash ../examples/start-timer-on-fullscreen.sh \
  agent-0123456789ab org.example.Game --title "Any%" fullscreen -- speedrun-timer --start
```

Choose `fullscreen` for KWin's explicit state or `fills-display` for borderless
geometry. Fullscreen detection remains a window predicate; use the media
commands below when a workflow also needs playback state or track position.

#### MPRIS media players

Media commands discover and control MPRIS players on the selected session's
D-Bus, so private sessions and the live desktop do not share player targets.
They require `playerctl` (`doctor --json` reports whether it is installed).
List players first, then pass one exact name from that result to inspect or
control; the API never silently chooses the first player:

```bash
# Use --session user-desktop only to inspect/control players on the live desktop.
kde-desktop --session user-desktop media list --json

# Replace this with one exact name returned by media list.
PLAYER='firefox.instance123'
kde-desktop --session user-desktop media inspect "$PLAYER" --json

# Relative seek offsets may be negative; seek-to uses an absolute position in seconds.
kde-desktop --session user-desktop media control "$PLAYER" pause --json
kde-desktop --session user-desktop media control "$PLAYER" seek --seconds -15 --json
kde-desktop --session user-desktop media seek-to "$PLAYER" 180 --json
```

Inspection returns playback status, available title/artist/album metadata,
current position, and duration in seconds. Players may omit metadata or position;
those fields are `null`. Relative `seek` offsets rewind/advance from the current
position; `media seek-to` submits an absolute position between 0 and 86400 seconds.
Both report that a request was submitted, not that playback changed. `playerctl`
names that would match multiple player instances are rejected; use an exact
listed instance name. Player-specific volume is not exposed; use the PipeWire
device-volume controls below.

#### PipeWire devices and per-app routing

Audio operations are **live-desktop only** because the managed private KWin
sessions do not have isolated PipeWire servers. Every command must explicitly
select `--session user-desktop`. The backend requires `wpctl` and `pactl`, checks
that the PulseAudio compatibility server is PipeWire, and verifies changes by
reading state back. `doctor --json` reports whether both executables are
installed; `audio list` checks whether the live server is usable:

```bash
# Inspect defaults, outputs (sinks), inputs (sources), and current output streams.
kde-desktop --session user-desktop audio list --json

# Use exact device names from audio list. sink means output; source means input.
kde-desktop --session user-desktop audio set-default sink 'alsa_output.headphones' --json
kde-desktop --session user-desktop audio set-volume source 'alsa_input.usb_mic' 50 --json
kde-desktop --session user-desktop audio set-mute source 'alsa_input.usb_mic' mute --json

# Move just one listed output stream; the ID and application name are both checked.
kde-desktop --session user-desktop audio route-stream 42 'alsa_output.headphones' \
  --application Firefox --process-id 1234 --json
```

Changing the default affects new auto-connected streams; existing streams may
stay on their current output. Use `route-stream` for one existing stream. If a
browser has several streams, list them and choose the specific stream ID; the
operation never moves all streams based only on a broad app-name match. Audio
listing can reveal active application names, and mute/volume/default changes
affect the user's real devices.

### MCP server

The stdio MCP server exposes the same window, input, display, MPRIS media,
PipeWire audio, screenshot, and capability operations, plus private-session
lifecycle tools.
Install the project with `uv sync`, then configure your MCP client to launch
`kde-desktop-mcp` from this checkout:

```json
{
  "mcpServers": {
    "kde-desktop": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/kde-desktop-automation",
        "kde-desktop-mcp"
      ]
    }
  }
}
```

Every MCP desktop tool requires a `session` argument in its input schema. Pass a
managed session ID returned by `kde_session_create`, or pass the exact string
`user-desktop` when deliberately selecting the physical desktop. CLI defaults
and `KDE_DESKTOP_SESSION` do not select MCP targets; `kde_session_use` changes a
persisted CLI default only. This explicit per-call selector prevents accidental
cross-session use but is not an authorization boundary. Existing MCP callers
that omitted `session` must be updated; CLI default-session behavior is unchanged.

`kde_screenshot` returns target/session metadata and PNG image content. Session
creation/launch/stop tools operate only on managed private sessions; launch
takes an argv array and never evaluates a shell command. MCP clients sharing
the server still share the Unix user's authority to list, launch, and stop
sessions, so a coordinator should own lifecycle calls and distribute explicit
IDs. Input remains best-effort and must be independently verified.

### Deliberate safety limits

- Shared operations use the configured private default when `--session` is
  omitted. A live desktop target must be selected explicitly with
  `--session user-desktop` on each invocation; an unset/stopped private default
  never redirects to the user desktop.
- `dotool` is a system-wide uinput device and is **not** used in the private
  session. Private input uses KWin's session-bound fake-input Wayland protocol;
  KWin can ignore its requests, so a successful submission is not proof of an
  application effect.
- Private window management uses KWin's privileged Plasma Wayland protocol,
  separately scoped to each session socket; it is enabled only when that
  compositor advertises protocol v13 or newer. It does not query the live
  desktop's KWin D-Bus service.
- Live `user-desktop` operations currently require KDE on Wayland, `kdotool`, a
  reachable KWin D-Bus service, and (for input) a compatible KWin fake-input
  protocol. `capabilities` probes window discovery/D-Bus, while display and
  screenshot availability are based on executable presence. X11 and persistent
  attach/detach are not implemented.
- Window-level input still uses coordinates and current focus; there is no
  accessibility element tree or app-level “click Play All” operation. Recheck
  window state and independently verify meaningful effects.
- Private session lifecycle requires Linux `pidfd` process-control support.
  If unavailable, session creation fails closed rather than falling back to
  race-prone PID signalling.
- `session stop` removes the private runtime only when it can verify it owns all
  entries and no mountpoints are present. If a portal leaves mounted or busy
  state behind, the CLI warns and preserves it for manual cleanup.

The private session has been smoke-tested on Plasma 6.8 with `kwin_wayland
--virtual`: `wlpdisplays` sees the virtual output, Spectacle captures it, and
Kate accepts KWin fake-input keyboard and pointer events; a file was typed and
saved, then independently read back. A separate, explicitly scoped manual test
also saved a dedicated Kate test file through the live KWin socket on this
machine. Both private KWin and live-desktop window discovery have opt-in
integration tests; the private test also activates a disposable Kate window,
sends targeted input, and reads back a scratch file. Those successful tests are
not authorization or delivery guarantees for other desktops. Exact capabilities
vary with the installed Plasma build and utilities. See
[`interface.md`](interface.md) for the broader target model, operation contracts,
and known gaps.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Optional backend smoke tests are deliberately opt-in. The live-desktop test only
lists and inspects windows; it sends no input:

```bash
KDE_DESKTOP_RUN_KWIN_INTEGRATION=1 uv run pytest tests/test_kwin_integration.py -q
KDE_DESKTOP_RUN_USER_DESKTOP_INTEGRATION=1 uv run pytest tests/test_user_desktop_integration.py -q
```

The package uses a `src/` layout. CLI and MCP transports should call the same
typed operations; backend commands are invoked with argument arrays, never by
building shell strings.

## Design proposal

See [`interface.md`](interface.md) for the target model, coordinates, multi-step
automation scenario, backend boundaries, and future MCP mapping.
