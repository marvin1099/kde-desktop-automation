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

### KWin Screenshot2 helper

All screenshots use KWin's `org.kde.KWin.ScreenShot2` D-Bus API through a small
native Qt helper. Spectacle is not used. KWin restricts this API to registered
executables, so the helper's desktop entry declares the restricted interface;
the Python CLI/MCP process is deliberately not authorized. Install it in the
user's local prefix (not system-wide):

```bash
# Debian/Ubuntu package names; use the matching Qt 6 development packages on other systems.
sudo apt install cmake g++ qt6-base-dev
bash scripts/install-screenshot-helper.sh
```

The script builds and installs only under `$HOME/.local` (or the configured
`XDG_DATA_HOME` for its desktop entry), then refreshes KDE's service cache. No
system-wide helper installation is performed. If `$HOME/.local/bin` is not in
the MCP process's `PATH`, set `KDE_DESKTOP_SCREENSHOT_HELPER` to the installed
binary in the MCP launch environment. Private sessions copy only the matching
helper desktop entry into their isolated application home; they do not authorize
Python or inherit the user's full application-data directory. `doctor` and
capability reports verify the helper and matching authorization declaration;
KWin's actual authorization is still verified by a capture request. Restart
private sessions created before installation so they receive the entry.

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
uv run kde-desktop --session user-desktop window screenshot WINDOW_UUID \
  --output /tmp/window.png --json
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
kde-desktop --session "$TARGET" window set-geometry WINDOW_UUID 640 0 640 800 --json
kde-desktop --session "$TARGET" input key WINDOW_UUID CTRL+END --json
kde-desktop --session "$TARGET" input type WINDOW_UUID $'\nExample note' --json
kde-desktop --session "$TARGET" input click WINDOW_UUID 0.92 0.90 --json
kde-desktop --session "$TARGET" pointer position --json
kde-desktop --session "$TARGET" observe --screenshot-output /tmp/desktop.png --json
kde-desktop --session "$TARGET" display list --json
kde-desktop --session "$TARGET" capabilities --json
```

`display list` is read-only today. Creating an extra virtual output on
`user-desktop` is a proposed feature that would extend the user's visible KWin
desktop; it is distinct from the hidden output created inside a private session.
The proposed operation would accept bounded dimensions, verify the effective
geometry, and manage the output lifecycle only—not Sunshine, streaming, or
connecting a phone/client.

`window find` takes an exact `--app-id` and/or a case-insensitive `--title`
substring. It returns candidates as an error if more than one window matches;
it never chooses silently. Inspection returns title, app ID, fullscreen,
minimized/maximized and active state, geometry, and a UUID scoped to that
session. A UUID is only valid for the selected target session and while that
window exists; never reuse one with another session or after a `stale_target`
error. Before a delayed action, refresh the list/inspection. Window-targeted
actions also accept optional `--expect-app-id` and `--expect-pid` preconditions;
the live window identity is re-inspected before dispatch, and a mismatch fails
with `stale_target`. These checks guard identity, not geometry/state, and cannot
make the check and action atomic. MCP exposes the same fields as
`expected_app_id` and `expected_pid`.
The private KWin protocol
now supports the same window list/find/inspect/activate flow when the session
advertises protocol v13 or newer; capabilities are recorded per session.
`window set-minimized WINDOW_ID minimized|restored` sets and verifies the
desired state on either target. It is reversible; changing state on the live
desktop may change focus.
`window set-fullscreen WINDOW_ID fullscreen|restored` and
`window set-maximized WINDOW_ID maximized|restored` provide the same explicit
desired-state behavior. Each request is checked against a fresh KWin readback;
the readback wait is bounded. Restore a minimized window and clear the other
fullscreen/maximized state before enabling one of these states. On
`user-desktop`, these operations affect the real workspace and may change focus.

`window set-geometry WINDOW_ID X Y WIDTH HEIGHT` sets a KWin **frame** rectangle
in logical desktop pixels, then reads it back and returns both the request and
effective geometry. Restore maximized/fullscreen windows first. The compositor
may enforce size/placement constraints; an unverified request is reported as an
error. This is the precise placement operation and does not simulate a title-bar
drag.

`pointer position` is read-only and reports logical desktop coordinates, KWin's
screen index, the window under the pointer when known, and the matching display
name. `observe` gathers window/display lists, active-window focus context,
pointer context, and a fresh timestamped snapshot ID. Its screenshot is optional
on the CLI (`--screenshot-output PATH`) and included as image content by the
`kde_desktop_observe` MCP tool when the authorized Screenshot2 helper is available. A single-display
capture includes an explicit
screenshot-pixel to logical-desktop mapping; a composite multi-display capture
is reported as unmapped. Neither surface exposes focused child controls or an
accessibility tree.

`input key`, `input type`, and `input click` require a window UUID and require
that window to already be active. Activate it explicitly first. Clicks use
normalized window-relative coordinates from 0 to 1. Private KWin uses content
geometry when available; the live target reports the geometry basis supplied by
KWin D-Bus, so inspect `geometry_basis` before assuming the bounds are identical.
For an inactive live window, explicit activation changes user focus and may
switch virtual desktops. Do not assume window-targeted requests preserve focus.
Both use KWin fake input routed through compositor focus: the window ID is
validated and checked active, but delivery is not atomically bound to that
window and focus can change during submission. KWin can also ignore events; a
round trip is not proof the app received them. Receipts report this routing and
leave `effect_verified: false` until a fresh screenshot or independent app/file
state confirms the result.

For shell-level pointer work, `input click-at X Y` and `input drag X1 Y1 X2 Y2`
use integer logical desktop pixels, including negative origins on multi-monitor
layouts. Unlike window-relative `input click`, these operations are global and
not bound to a window; they move the pointer and may change the active window.
Inspect `display list` first. A live click must lie on a listed display, and each
sampled point along a drag must do so as well; `--steps` is bounded from 1 to 64
(default 8). Drags also accept `--duration-ms` (0–5000, default 750),
`--hold-ms` (0–2000, default 100), and `--end-pause-ms` (0–2000, default 250),
with a combined maximum of 8000 ms. Pacing can make real UI gestures such as
file drag-and-drop more likely to be recognized, but does not add an app-specific
drop operation or guarantee delivery. Global pointer submissions remain
best-effort and should be verified with a fresh screenshot or independent
application state.

```bash
# Explicitly choose the live desktop; coordinates are logical desktop pixels.
kde-desktop --session user-desktop display list --json
kde-desktop --session user-desktop input click-at 420 310 --json
kde-desktop --session user-desktop input click-at --json -- -320 240
kde-desktop --session user-desktop input drag --steps 48 --duration-ms 1500 --hold-ms 250 --end-pause-ms 500 --json -- -320 240 700 480
kde-desktop --session user-desktop window set-minimized WINDOW_ID minimized --json
kde-desktop --session user-desktop window set-minimized WINDOW_ID restored --json
kde-desktop --session user-desktop window set-fullscreen WINDOW_ID fullscreen --json
kde-desktop --session user-desktop window set-fullscreen WINDOW_ID restored --json
kde-desktop --session user-desktop window set-maximized WINDOW_ID maximized --json
kde-desktop --session user-desktop window set-maximized WINDOW_ID restored --json
# Optional identity guards use values returned by window list/find/inspect.
kde-desktop --session user-desktop window set-maximized WINDOW_ID maximized \
  --expect-app-id org.kde.kate --expect-pid 1234 --json
```

The generic drag path was exercised in a hidden session by dragging a test file
from Dolphin into Kate. A paced drag opened the file in Kate; verification used
the resulting Kate title and a fresh screenshot. This is ordinary pointer input,
not a Dolphin/Kate-specific workflow. If a drop does not take, inspect the
source/target surfaces and fresh desktop state before retrying.

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
kde-desktop --session user-desktop media seek-by "$PLAYER" --seconds -15 --json
kde-desktop --session user-desktop media seek-to "$PLAYER" 180 --json
kde-desktop --session user-desktop media wait-for-status "$PLAYER" Playing --timeout 10 --json
```

Inspection returns playback status, available title/artist/album metadata,
current position, and duration in seconds. Players may omit metadata or position;
those fields are `null`. Relative `seek` offsets rewind/advance from the current
position; `media seek-by` exposes the same operation as a dedicated route.
`media seek-to` submits an absolute position between 0 and 86400 seconds. Seek
commands report that a request was submitted, not that playback changed.
`media wait-for-status` waits at most 60 seconds for `Playing`, `Paused`, or
`Stopped` and includes the last status observation on timeout. `playerctl` names
that would match multiple player instances are rejected; use an exact listed
instance name. Player-specific volume is not exposed; use the PipeWire
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
kde-desktop --session user-desktop audio wait-for-mute source 'alsa_input.usb_mic' mute \
  --timeout 10 --json

# Move just one listed output stream; the ID and application name are both checked.
kde-desktop --session user-desktop audio route-stream 42 'Games' \
  --application Firefox --process-id 1234 --json
```

`wait-for-mute` is read-only: it polls one exact listed sink/source for a
requested mute state for 0.1–60 seconds and returns the last observation on
timeout. It does not set or toggle mute. `route-stream` moves one verified app
stream to an exact listed output sink using the PulseAudio compatibility API.
Combined/virtual sinks are valid destinations; monitor endpoints are rejected.
This is not raw PipeWire patchbay control: it does not create or remove
arbitrary port links, so existing virtual-device wiring is left outside the
operation's mutation scope.

Changing the default affects new auto-connected streams; existing streams may
stay on their current output. Use `route-stream` for one existing stream. If a
browser has several streams, list them and choose the specific stream ID; the
operation never moves all streams based only on a broad app-name match. Audio
listing can reveal active application names, and mute/volume/default/routing
changes affect the user's real devices.

#### Temporary power inhibitors

Power inhibitors are **live-desktop only** and do not edit saved KDE power
profiles. Select one typed action; each inhibitor gets its own ID, so listing or
releasing these inhibitors does not affect inhibitors created by KDE or other
applications:

```bash
# Prevent suspend/hibernate for 30 minutes.
kde-desktop --session user-desktop power inhibit sleep --duration 1800 \
  --reason "Long-running render" --json

# Prevent KDE's screen saver/automatic lock while a presentation is running.
kde-desktop --session user-desktop power inhibit screen-lock --duration 3600 \
  --reason "Presentation" --json

# Keep an inhibitor until explicitly released. Save the returned .data.id.
kde-desktop --session user-desktop power inhibit sleep --duration -1 --json
kde-desktop --session user-desktop power list --json
kde-desktop --session user-desktop power release INHIBITOR_ID --json
```

`power inhibit` accepts `screen-lock`, `power-management`, `sleep`, `idle`,
`shutdown`, `handle-power-key`, `handle-suspend-key`, `handle-hibernate-key`,
and `handle-lid-switch`. `screen-lock` uses KDE's
`org.freedesktop.ScreenSaver` interface; `power-management` uses KDE's
`org.freedesktop.PowerManagement.Inhibit` interface. The remaining choices use
`systemd-inhibit`; in particular, `idle` blocks systemd idle actions but is
not a substitute for `screen-lock`.
Finite durations are 1–86400 seconds (default 300); `-1` means indefinite.
Finite inhibitors release themselves when their timer expires. For indefinite
inhibitors, call `power release` with the returned ID when done. `power list`
shows only this application's tracked inhibitors.

### MCP server

The stdio MCP server exposes the same observation, window, pointer/input,
display, MPRIS media, PipeWire audio, screenshot, temporary power-inhibitor,
and capability operations, plus private-session lifecycle tools.
`kde_desktop_observe` returns a structured
snapshot and screenshot image content when the authorized Screenshot2 helper is available;
`kde_pointer_position` reads the pointer;
`kde_window_set_geometry` sets and verifies a KWin frame rectangle.
Install the project with `uv sync`, then configure your MCP client to launch
`kde-desktop-mcp` from this checkout:

```json
{
  "mcpServers": {
    "kde-desktop": {
      "command": "uv",
      "env": {
        "KDE_DESKTOP_SCREENSHOT_HELPER": "/home/your-user/.local/bin/kde-desktop-screenshot"
      },
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

The `kde_power_inhibit`, `kde_power_list_inhibitors`, and
`kde_power_release_inhibitor` tools mirror the CLI power commands. They require
`session="user-desktop"`; `kde_power_inhibit` accepts one of the typed inhibitor
choices, `duration_seconds` (1–86400 or `-1` for indefinite), and an optional
reason. Use the returned ID with the release tool when an indefinite inhibitor
is no longer needed.

`kde_screenshot` captures the full selected desktop. `kde_screenshot_window`
uses KWin Screenshot2 to capture the exact selected UUID (not a crop) without
activating it, including when the target is inactive. The result includes the
selected window and reports observed focus changes. Both tools return
target/session metadata and PNG image content. Session
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
  protocol. `capabilities` probes window discovery/D-Bus, while screenshot
  availability reports the native helper's presence; its desktop-entry
  authorization is checked by KWin only when a capture is requested. X11 and persistent
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
--virtual`: `wlpdisplays` sees the virtual output, KWin Screenshot2 captures it, and
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

The Screenshot2 integration test is separately gated and uses only a private
KWin session. Build the helper and its test window first; the test registers the
helper entry only inside its disposable private home and verifies an inactive
window capture leaves the other active window focused:

```bash
KDE_DESKTOP_RUN_KWIN_SCREENSHOT_INTEGRATION=1 \
KDE_DESKTOP_SCREENSHOT_HELPER="$PWD/build/screenshot-helper/kde-desktop-screenshot" \
KDE_DESKTOP_SCREENSHOT_TEST_WINDOW="$PWD/build/screenshot-helper/kde-desktop-screenshot-test-window" \
uv run pytest tests/test_kwin_integration.py::test_private_kwin_captures_inactive_window_without_changing_focus -q
```

The real Dolphin-to-Kate drop test is additionally gated because it launches
both applications and reads Kate's copied document text from the private
session clipboard. It requires Dolphin, Kate, `kdotool`, and `wl-paste`:

```bash
KDE_DESKTOP_RUN_KWIN_INTEGRATION=1 \
KDE_DESKTOP_RUN_KWIN_DND_INTEGRATION=1 \
uv run pytest tests/test_kwin_integration.py::test_private_dolphin_drag_and_drop_opens_file_in_kate -q
```

The package uses a `src/` layout. CLI and MCP transports should call the same
typed operations; backend commands are invoked with argument arrays, never by
building shell strings.

## Further reading

See [`interface.md`](interface.md) for the operation model and design notes, and
[`automation-roadmap.md`](automation-roadmap.md) for candidate future work.
