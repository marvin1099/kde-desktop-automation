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

## Public concepts

### Targets

Every operation that acts on a desktop object takes a target. Initial target
kinds:

| Kind | Examples | Typical operations |
| --- | --- | --- |
| `session` | current KDE session | inspect, active window, pointer position |
| `window` | active window, a window reference, a selector | inspect, focus, move, resize, minimize, close, input, screenshot |
| `desktop` | current or a virtual desktop | inspect, switch to, list its windows |
| `display` | connector/name or a display reference | inspect, screenshot, configure where supported |
| `region` | a rectangle in an explicit coordinate space | screenshot, input |

An observation returns an opaque `ref` for each object. References are intended
to be used by the next command, not persisted indefinitely. The response may
also include the backend's native ID for debugging. If a reference has gone
stale, return `stale_target` and ask the caller to inspect again; do not silently
retarget it.

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
- `screen-percent`: legacy absolute percentages for backends such as `dotool`.

The caller should not have to convert a window point to a global screen point.
The input adapter uses fresh window/display geometry to do that conversion and
reports the resolved point. If the window moved between observation and action,
re-resolve the target and recompute. If the backend cannot perform this safely,
return an explicit unsupported/error result rather than click an approximate
location.

## CLI shape

Use a single command with noun/verb subcommands, human-readable output by
default, and a versioned JSON mode for agents. The command name below is
illustrative (`kde-ai`; choose a final name separately).

```text
kde-ai observe [--target session|window:REF|desktop:REF|display:REF] [--format json]
kde-ai window list [--desktop current|all] [--format json]
kde-ai window get --target REF [--format json]
kde-ai window focus --target REF
kde-ai window move --target REF --to DISPLAY --at 0.2,0.1
kde-ai window resize --target REF --size 1000x700
kde-ai desktop list [--format json]
kde-ai desktop switch --target REF
kde-ai display list [--format json]
kde-ai display get --target REF [--format json]
kde-ai input click --target window:REF --at 0.5,0.5 --space window
kde-ai input type --target window:REF --text 'search query'
kde-ai input key --target window:REF --keys Ctrl+L
kde-ai input scroll --target window:REF --at 0.5,0.5 --delta 0,-3 --space window
kde-ai screenshot --target window:REF --output /tmp/window.png
kde-ai capabilities [--format json]
```

`--target` is required for actions where the destination could otherwise be
unclear. `active` can be a convenient window alias. Moving the pointer or
clicking by global coordinates requires an explicit display/desktop target and
coordinate space. Destructive actions such as closing a window should be
separate commands, not hidden side effects of input or selection.

An optional `--dry-run` should be supported for actions that can be planned
without changing the session. It returns the resolved target and intended
effect. It must not be presented as a guarantee that the desktop will remain
unchanged between planning and execution.

## Structured data

JSON output should be versioned and have a consistent envelope. For example:

```json
{
  "schema_version": "1",
  "ok": true,
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
    "capabilities": ["focus", "move", "resize", "screenshot", "input"]
  },
  "warnings": []
}
```

Lists should include a `data` array and be deterministic where possible. Action
responses should identify the target, report the requested and resolved action,
and include any resulting state that can be verified. Errors should have a
machine-readable `code`, a human-readable `message`, and optional `details`;
examples include `ambiguous_target`, `stale_target`, `unsupported`,
`permission_denied`, and `backend_unavailable`. Reserve stderr and non-zero exit
codes for failures; JSON mode should still emit the error envelope.

Screenshots should normally be written to a requested path and return that path
plus capture metadata (target, geometry, timestamp, coordinate space). Avoid
embedding large image data in normal JSON responses. A later MCP transport can
return image content using MCP's image content type while preserving the same
metadata.

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
| `dotool` | Pointer and keyboard input; adapt public target-relative points to its absolute percentage coordinates |
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
   structured output.
2. Add focus, desktop switching, window move/resize, and screenshots; verify
   results by re-reading state.
3. Add target-relative click, type, key, and scroll operations with explicit
   coordinate conversion and stale-geometry checks.
4. Add display settings as a separate, higher-impact operation family, with
   validation and a way to restore the prior configuration where possible.
5. Add an accessibility provider and MCP transport without changing the core
   target/result model.

This order delivers useful desktop awareness early, while postponing fragile
coordinate input and monitor reconfiguration until their target and safety
contracts are established.
