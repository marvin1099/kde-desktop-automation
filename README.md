# KDE Interaction

An AI-friendly CLI and Python package for observing and automating KDE Plasma.
The interface is designed to work as a CLI first and to support an MCP
transport later without changing the operation model.

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

`session launch` passes the executable and arguments directly (no shell) and
records the launched process for session cleanup. `key`, `type`, and `click`
always require a managed session ID and connect to that session's Wayland
socket. Key and text go to the session's **current keyboard focus**; there is
not yet a window list or a way to choose a specific window. For a straightforward
single-app workflow, launch the app, inspect a session screenshot, act, then
capture another screenshot to verify the result.

Input receipts report the target session, the submitted event count, and whether
the Wayland server round trip completed. They deliberately set
`effect_verified: false`: KWin's fake-input protocol may ignore requests and does
not acknowledge authorization or delivery to an application. Verify meaningful
effects from a fresh screenshot or other independent state. The `doctor` and
session capability reports expose these limits; `input.effect_ack` is always
false. Layout-aware key chords and Unicode text currently require KWin's v6
fake-input protocol; the session capability report reflects the version that
session actually advertises.

### Deliberate safety limits

- The CLI never sends input to the user's live desktop by default.
- `dotool` is a system-wide uinput device and is **not** used in the private
  session. Private input uses KWin's session-bound fake-input Wayland protocol;
  KWin can ignore its requests, so a successful submission is not proof of an
  application effect.
- The standalone virtual KWin process currently exposes display information
  and screenshots, but not the KWin D-Bus service needed by `kdotool` for
  window inspection/control. The CLI reports these capabilities as unavailable;
  it does not fall back to the user's session.
- Attaching to or sending input to the real user desktop is not implemented in
  the CLI. It must be a separate explicit, permissioned mode, not an automatic
  fallback.
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
machine. That one success is not an authorization or delivery guarantee for
other desktops, and the CLI still has no live-desktop interaction mode. Exact
capabilities vary with the installed Plasma build and utilities. See
[`INTERFACE.md`](INTERFACE.md) for the broader target model and known gaps.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

The package uses a `src/` layout. CLI and MCP transports should call the same
typed operations; backend commands are invoked with argument arrays, never by
building shell strings.

## Design proposal

See [`INTERFACE.md`](INTERFACE.md) for the target model, coordinates, multi-step
automation scenario, backend boundaries, and future MCP mapping.
