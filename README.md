# KDE Desktop Automation

A Python toolkit, CLI, and MCP server for inspecting and automating KDE Plasma.
It defaults to a separate, private KWin session; access to the live desktop must
be selected explicitly.

[Codeberg (primary)](https://codeberg.org/marvin1099/kde-desktop-automation) ·
[GitHub mirror](https://github.com/marvin1099/kde-desktop-automation)

> **Prototype:** support depends on the installed KDE/Wayland components and
> system utilities. Input is best-effort and does not prove an application acted
> on it. A private session isolates its compositor and home directory, but is
> **not a security sandbox** and runs as your Unix user.

## What it can do

- Create private virtual desktops and launch applications inside them.
- Auto-stop private sessions after six hours by default; extend their lease,
  make one indefinite with `-1`, or stop it explicitly.
- Observe windows, displays, pointer/focus context, and capture full-desktop
  screenshots; the CLI and MCP can also capture individual windows directly.
- Find, inspect, activate, minimize/restore, and place windows; send guarded
  keyboard/text input and coordinate clicks/drags.
- Inspect and control MPRIS media players.
- Inspect and change live PipeWire audio settings (live desktop only).
- Temporarily inhibit sleep, KDE screen locking, and other available power
  actions on the live desktop, with timed or explicit release.
- Expose the same target-aware operations through an stdio MCP server.

Capabilities vary by target and installed tools. There is no accessibility tree,
OCR-based control discovery, or app-specific action API yet. See the
[usage guide](docs/usage.md) for exact commands and limitations.

## Requirements

- Linux with KDE Plasma on Wayland; private sessions use KWin's virtual-output
  backend. Exact support depends on the installed Plasma/KWin version.
- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) to install from a
  checkout.
- Optional system utilities provide additional features. Run `kde-desktop
  doctor` to see which are available; per-target `capabilities` reports what can
  be used.
- Screenshots use KWin Screenshot2 through a separately built and authorized
  native helper; Spectacle is not used. See the [usage guide](docs/usage.md#kwin-screenshot2-helper).

## Quick start

```bash
git clone https://codeberg.org/marvin1099/kde-desktop-automation.git
cd kde-desktop-automation
uv sync
uv run kde-desktop doctor
uv run kde-desktop session create --name agent --width 1280 --height 800 --json
```

Copy the `session_id` from the result and use it for commands in that private
desktop:

```bash
SESSION_ID='agent-...'
uv run kde-desktop --session "$SESSION_ID" observe --json
uv run kde-desktop session launch "$SESSION_ID" -- kate --new /tmp/agent-note.txt
uv run kde-desktop --session "$SESSION_ID" screenshot --output /tmp/agent.png --json
uv run kde-desktop session stop "$SESSION_ID"
```

To deliberately target the real desktop, pass `--session user-desktop` on each
command. Private sessions cannot see host windows or media players. Do not run
untrusted applications in them: they are not an OS-level sandbox.

## MCP server

After `uv sync`, configure your MCP client to launch `kde-desktop-mcp` from this
checkout. OpenCode users in this repository are configured by the project-local
`opencode.json`, which starts the stdio server with `uv run kde-desktop-mcp`.
Other MCP clients can use:

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

Every MCP desktop call requires an explicit `session` value: a managed session
ID or `user-desktop` when intentionally using the live desktop.

## Safety essentials

- A missing or stopped private session never falls back to the live desktop.
- Window-targeted input requires that window to already be active; it is guarded
  by focus, not atomically routed to the window.
- Global clicks and drags use logical desktop coordinates, move the pointer, and
  may change focus. A successful input receipt is not proof of an app effect;
  inspect again and verify the result independently.
- Window placement is a KWin frame-geometry request. KWin may constrain it; the
  tool reads back and reports the effective geometry.
- Audio controls are live-desktop-only. Private sessions do not provide an
  isolated audio server.
- Power inhibitors affect the real host; indefinite inhibitors remain active
  until explicitly released with their returned ID.

## Guides

- [CLI and MCP usage](docs/usage.md)
- [Examples](docs/examples.md)
- [Interface and design notes](docs/interface.md)
- [Automation roadmap](docs/automation-roadmap.md)
- [Release process](docs/releasing.md)

## Development

```bash
uv sync --all-groups
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
```

The private-KWin tests are opt-in. The Dolphin-to-Kate drag test additionally
requires Dolphin, Kate, `kdotool`, and `wl-paste`:

```bash
KDE_DESKTOP_RUN_KWIN_INTEGRATION=1 \
KDE_DESKTOP_RUN_KWIN_DND_INTEGRATION=1 \
uv run pytest tests/test_kwin_integration.py::test_private_dolphin_drag_and_drop_opens_file_in_kate -q
```

## License

Licensed under the [GNU Affero General Public License v3.0 only](LICENSE).
