# KDE Desktop Automation

A target-aware CLI, Python toolkit, and MCP server for automating KDE Plasma.
Use one operation model from scripts or AI clients, targeting either a managed
private KWin session or—only when explicitly requested—the live desktop.

> **Prototype:** capabilities depend on the installed KDE/Wayland and PipeWire
> versions. Input submission is best-effort, not proof that an application
> received or acted on an event. A private session is isolated from the user's
> compositor, but it is not a security sandbox.

## What it supports

- **Private sessions:** create, launch apps in, inspect, screenshot, and stop a
  separate virtual KWin session.
- **Windows and input:** list/find/wait/inspect/activate windows; send guarded
  key, text, and normalized window-relative click input; capture screenshots.
- **Media:** list and inspect MPRIS players, then submit typed playback
  controls, including relative seek.
- **Audio:** inspect PipeWire devices and output streams; set defaults,
  volume/mute, or route one verified stream. Audio operations are live-desktop
  only.
- **AI integration:** an stdio MCP server exposes the same target-aware
  operations, plus private-session lifecycle tools.

The current feature boundary and design direction are in
[`docs/interface.md`](docs/interface.md). The review of useful automation
patterns and proposed next steps is in
[`docs/automation-roadmap.md`](docs/automation-roadmap.md).

## Install and try a private session

From a checkout with [uv](https://docs.astral.sh/uv/) installed:

```bash
uv sync
uv run kde-desktop --help
uv run kde-desktop session create --name agent --width 1280 --height 800 --json
```

Use the `session_id` returned by `session create` for subsequent commands:

```bash
SESSION_ID='agent-0123456789ab'
uv run kde-desktop --session "$SESSION_ID" capabilities --json
uv run kde-desktop --session "$SESSION_ID" window list --json
uv run kde-desktop session launch "$SESSION_ID" -- kate --new /tmp/agent-note.txt
uv run kde-desktop --session "$SESSION_ID" screenshot --output /tmp/agent.png --json
uv run kde-desktop session stop "$SESSION_ID"
```

The private session has its own compositor, Wayland socket, D-Bus session,
runtime directory, and application home. It does not see windows or players on
the host desktop.

## Choose a target explicitly

Put the global `--session` option **before** the operation group:

```bash
kde-desktop --session "$SESSION_ID" window list --json
kde-desktop --session user-desktop window list --json
kde-desktop --session user-desktop media list --json
kde-desktop --session user-desktop audio list --json
```

For shared CLI operations, omission selects `KDE_DESKTOP_SESSION`, then the
configured private default. It never falls back to the live desktop. Set
`KDE_DESKTOP_SESSION` only when a persistent private default is useful; pass
`--session` explicitly in multi-agent scripts. MCP calls always require a
`session` argument and do not use CLI defaults.

### Help and scripting

Help is available at each level. Start at the top level, then inspect a group
or a specific operation:

```bash
kde-desktop --help
kde-desktop window --help
kde-desktop window wait --help
kde-desktop audio route-stream --help
```

Use `--json` for versioned output that scripts can parse. A safe discovery flow
is `capabilities` → list/find an exact target → inspect → act → inspect or
screenshot again. Ambiguous window matches fail with candidate details rather
than silently selecting one. See [`docs/usage.md`](docs/usage.md) for command
examples, target semantics, and safety details, and
[`docs/examples.md`](docs/examples.md) for composable scripts.

## MCP server

Install the checkout with `uv sync`, then configure your MCP client to launch
`kde-desktop-mcp`:

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

Each desktop tool call takes an explicit `session`. Use a managed session ID,
or `user-desktop` only when deliberately selecting the live desktop.

## Safety and limits

- Live-desktop access requires an explicit selection: `--session user-desktop`
  for shared commands, the dedicated `user-desktop` CLI namespace, or
  `session="user-desktop"` in MCP. Audio is live-desktop only.
- Private session launch accepts an executable and argument vector, not a shell
  command. The session still runs as the current Unix user.
- Input is focus-guarded and best-effort. Recheck state independently; a
  successful receipt does not verify an application effect.
- There is no accessibility tree or app-specific action API yet. Window
  placement, live display configuration, arbitrary PipeWire graph patching,
  browser-tab discovery, and OCR-driven controls are not implemented.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

The private-KWin integration test starts its own compositor and is opt-in:

```bash
KDE_DESKTOP_RUN_KWIN_INTEGRATION=1 uv run pytest tests/test_kwin_integration.py -q
```

The live-desktop integration test is separate and read-only, but requires
explicit opt-in on a KDE Wayland desktop:

```bash
KDE_DESKTOP_RUN_USER_DESKTOP_INTEGRATION=1 uv run pytest tests/test_user_desktop_integration.py -q
```
