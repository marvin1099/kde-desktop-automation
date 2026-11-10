# KDE Interaction

An AI-friendly CLI and Python package for observing and automating KDE Plasma.
The CLI and MCP server share one target-aware operation layer. Private hidden
desktops are the default; accessing the live desktop always requires the
explicit `user-desktop` target.

## Current prototype

The first vertical slice creates a **private virtual KWin compositor** using
KWin's virtual framebuffer backend. It gets its own Wayland socket, private
D-Bus session, runtime directory, and application home. The user can keep
working on the physical desktop while this session is inspected or screenshotted.

```bash
uv sync
uv run kde-ai doctor
uv run kde-ai session create --name agent --width 1280 --height 800
uv run kde-ai session list
uv run kde-ai session displays SESSION_ID
uv run kde-ai session screenshot SESSION_ID --output /tmp/agent.png
uv run kde-ai session launch SESSION_ID --json -- kate --new /tmp/agent-note.txt
uv run kde-ai session type SESSION_ID 'Hello from kde-ai'
uv run kde-ai session key SESSION_ID CTRL+S
uv run kde-ai session screenshot SESSION_ID --output /tmp/agent-after.png
uv run kde-ai session stop SESSION_ID
```

`session create` prints the generated ID. The isolated session is not a
security sandbox: it runs as the current Unix user, though with a private home
and session bus. Do not run untrusted applications in it.

`kde-ai doctor` reports host prerequisites without enumerating or injecting into
the live desktop. To probe live window/input capabilities, explicitly run
`kde-ai --session user-desktop capabilities`.

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
uv run kde-ai session create --name agent-a --width 1280 --height 800 --json
uv run kde-ai window list --json

# Create additional independent private desktops; name the target in scripts.
uv run kde-ai session create --name agent-b --json
uv run kde-ai --session agent-0123456789ab window list --json

# Real desktop access is never inferred: select it on each command.
uv run kde-ai --session user-desktop window list --json
uv run kde-ai --session user-desktop window find --title Kate --json
uv run kde-ai --session user-desktop screenshot --output /tmp/desktop.png
```

`session create --default` makes a newly created private session the default;
`session use SESSION_ID` changes the private default. Target selection precedence
for shared commands is: explicit `--session`, then `KDE_AI_SESSION`, then the
configured private default. The environment variable can contain a managed
private session ID only; setting it to `user-desktop` is rejected. Use
`--session user-desktop` on each command to opt into the live desktop.

For a persistent selection in a shell or an agent process environment, set:

```bash
export KDE_AI_SESSION=agent-0123456789ab
kde-ai window list --json
kde-ai input key WINDOW_UUID CTRL+S --json
```

An explicit `--session` overrides the environment. If the selected private
session is missing or stopped, commands fail rather than switching sessions or
falling back to the live desktop. For concurrent agents, give each agent its
own environment value or, especially when sharing an MCP server, pass each
managed session ID explicitly. Session lifecycle commands (`session create`,
`list`, `show`, `use`, `stop`, and `launch`) remain under `session`.

`KDE_AI_SESSION` and `session use` select process-wide/persisted defaults; they
are not per-agent ownership mechanisms. Concurrent session creation now
serializes the first-default claim, but shared MCP clients should still pass
explicit IDs instead of changing or relying on that default.

The common window commands are:

```bash
kde-ai --session "$TARGET" window list --json
kde-ai --session "$TARGET" window find --app-id org.kde.kate --title notes --json
kde-ai --session "$TARGET" window wait --app-id org.kde.kate --title notes --timeout 10 --json
kde-ai --session "$TARGET" window inspect WINDOW_UUID --json
kde-ai --session "$TARGET" window activate WINDOW_UUID --json
kde-ai --session "$TARGET" input key WINDOW_UUID CTRL+END --json
kde-ai --session "$TARGET" input type WINDOW_UUID $'\nTODO' --json
kde-ai --session "$TARGET" input click WINDOW_UUID 0.92 0.90 --json
kde-ai --session "$TARGET" display list --json
kde-ai --session "$TARGET" screenshot --output /tmp/after.png --json
kde-ai --session "$TARGET" capabilities --json
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

Example: use the same script against a hidden player session or the live desktop
by changing only `TARGET`. Coordinates must be calibrated for the player UI:

```bash
TARGET="${1:-user-desktop}"
APP_ID=org.example.Player
WINDOW_JSON="$(kde-ai --session "$TARGET" window find --app-id "$APP_ID" --json)" || exit
WINDOW_ID="$(jq -r '.data.window.window_id' <<<"$WINDOW_JSON")"
if jq -e '.data.window.fullscreen == true' >/dev/null <<<"$WINDOW_JSON"; then
  kde-ai --session "$TARGET" window activate "$WINDOW_ID" --json
  kde-ai --session "$TARGET" input click "$WINDOW_ID" 0.92 0.90 --json
fi
```

This enables reusable AI-authored workflows, but the click is still coordinate
based: there is not yet an accessibility tree that can address a control by its
label (such as “Play All”). To keep this example private by default instead,
pass a managed session ID rather than `user-desktop`.

### MCP server

The stdio MCP server exposes the same window, input, display, screenshot, and
capability operations, plus private-session lifecycle tools. Install the project
with `uv sync`, then configure your MCP client to launch `kde-ai-mcp` from this
checkout:

```json
{
  "mcpServers": {
    "kde-ai": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/kde-interation-ai",
        "kde-ai-mcp"
      ]
    }
  }
}
```

Desktop tools accept an optional `session` argument. Omitting it uses
`KDE_AI_SESSION` from the server process environment, if set to a private ID,
otherwise the configured private default. Pass the managed ID per call for
parallel agents sharing one MCP server. To operate on the real desktop, a tool
call must include `session: "user-desktop"`—the MCP server instructions explicitly
prohibit inferring that target. If no private target is configured or it has
stopped, the tool returns an error instead of falling back. `kde_screenshot`
returns target/session metadata and both structured capture metadata and the
PNG as MCP image content. Session creation/launch/stop tools operate only on
managed private sessions; launch takes an argv array and never evaluates a shell
command. MCP session IDs are selectors, not per-client authorization tokens:
clients sharing the server share the Unix user's authority to list, launch, and
stop managed sessions. `session use` changes the persisted default for all such
clients. For a multi-agent MCP process, configure explicit selection:

```json
{
  "mcpServers": {
    "kde-ai": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/kde-interation-ai", "kde-ai-mcp"],
      "env": {"KDE_AI_MCP_REQUIRE_EXPLICIT_SESSION": "1"}
    }
  }
}
```

In this mode, every desktop tool call must pass `session`, including
`user-desktop` for live access. A coordinator should own session lifecycle calls
and distribute explicit session IDs to the agents it coordinates. Input remains
best-effort and must be independently verified.

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
[`INTERFACE.md`](INTERFACE.md) for the broader target model, operation contracts,
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
KDE_AI_RUN_KWIN_INTEGRATION=1 uv run pytest tests/test_kwin_integration.py -q
KDE_AI_RUN_USER_DESKTOP_INTEGRATION=1 uv run pytest tests/test_user_desktop_integration.py -q
```

The package uses a `src/` layout. CLI and MCP transports should call the same
typed operations; backend commands are invoked with argument arrays, never by
building shell strings.

## Design proposal

See [`INTERFACE.md`](INTERFACE.md) for the target model, coordinates, multi-step
automation scenario, backend boundaries, and future MCP mapping.
