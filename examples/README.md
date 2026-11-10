# Composing GUI automation scripts

`kde-ai` is a set of target-explicit CLI building blocks. Bash (or another
script) can combine window discovery, bounded waits, inspection, input, and
screenshots into a task-specific workflow. A fullscreen speedrun timer is one
example; the same pattern can wait for an editor, check a dialog, or verify a
window before sending an action.

Examples below use `jq` to read the versioned JSON output. Set the target on
every command so scripts cannot silently switch sessions:

```bash
set -euo pipefail

# Use a managed session for an isolated desktop. Set this to user-desktop only
# when deliberately observing or operating on the physical desktop.
TARGET='agent-0123456789ab'
APP_ID='org.kde.kate'
```

## Wait for and inspect a window

`window wait` is useful after launching an app or triggering a UI transition. It
waits for one unique match; it does not pick the first of several candidates.
The timeout is bounded, and `--json` makes the result easy to compose:

```bash
# Wait up to 15 seconds for the exact app ID and a title containing "notes".
READY="$(kde-ai --session "$TARGET" window wait \
    --app-id "$APP_ID" --title notes --timeout 15 --json)"

# Keep the stable, session-scoped window UUID for subsequent operations.
WINDOW_ID="$(jq -er '.data.window.window_id' <<<"$READY")"

# Re-inspect immediately before acting; a window may have changed or closed.
CURRENT="$(kde-ai --session "$TARGET" window inspect "$WINDOW_ID" --json)"
jq -e --arg app_id "$APP_ID" \
    '.data.window.app_id == $app_id' <<<"$CURRENT" >/dev/null
```

If the selector is ambiguous, the command returns an error instead of guessing.
Add a distinguishing `--title` substring or inspect the candidates from
`window find`. If a later operation reports `stale_target`, find the window
again rather than reusing an old ID.

## Submit an action and verify separately

After confirming the window and the intended action, explicitly activate it,
submit input, then inspect a fresh screenshot or application state. Input
receipts report submission, not proof that the application processed the event.

```bash
# Activation is explicit; input does not implicitly choose or activate a window.
ACTIVATION="$(kde-ai --session "$TARGET" window activate "$WINDOW_ID" --json)"
jq -e '.data.active_window_verified == true' <<<"$ACTIVATION" >/dev/null

# Example action for a known editor window. Adapt this to the app and task.
kde-ai --session "$TARGET" input key "$WINDOW_ID" CTRL+S --json

# Capture new state after the action; verify the result independently.
AFTER="$(mktemp --suffix=.png)"
kde-ai --session "$TARGET" screenshot --output "$AFTER" --json
printf 'Inspect screenshot: %s\n' "$AFTER"
```

Use screenshots and coordinates only after checking which display/window they
refer to. Do not treat a successful input receipt as an application-level
success signal.

## Start a speedrun timer on fullscreen

The `window wait` command can wait for either KWin's explicit fullscreen state
or geometry that covers a logical display:

```bash
# KWin fullscreen state; unknown state does not count as fullscreen.
kde-ai --session "$TARGET" window wait \
    --app-id org.example.Game --fullscreen --timeout 60 --json

# Alternative for a borderless game whose window covers the display.
kde-ai --session "$TARGET" window wait \
    --app-id org.example.Game --fills-display --timeout 60 --json
```

Both predicates require one unique window match. `--fills-display` compares
logical desktop geometry with a 2-pixel edge tolerance; it does not infer that a
media player is playing. Use an MPRIS/player interface separately if a video
workflow needs playback status or playhead position.

For a one-shot timer start, wait, retain the matched window ID, re-inspect that
same ID, then launch the timer as an argv array:

```bash
GAME_APP_ID='org.example.Game'
READY="$(kde-ai --session "$TARGET" window wait \
    --app-id "$GAME_APP_ID" --fullscreen --timeout 60 --json)"
WINDOW_ID="$(jq -er '.data.window.window_id' <<<"$READY")"
CURRENT="$(kde-ai --session "$TARGET" window inspect "$WINDOW_ID" --json)"

# Fail closed if the window closed, changed identity, or left fullscreen.
jq -e --arg app_id "$GAME_APP_ID" --arg window_id "$WINDOW_ID" \
    '.data.window.app_id == $app_id
     and .data.window.window_id == $window_id
     and .data.window.fullscreen == true' <<<"$CURRENT" >/dev/null

# Arrays preserve spaces and shell metacharacters as literal arguments; never eval.
timer=(speedrun-timer --start --category 'Any% No Major Glitches')
export KDE_AI_TARGET="$TARGET" KDE_AI_WINDOW_ID="$WINDOW_ID"
exec "${timer[@]}"
```

For continuous monitoring, stale-window recovery, repeated bounded waits, and
a recheck of borderless display coverage, see the runnable
[`start-timer-on-fullscreen.sh`](start-timer-on-fullscreen.sh). Pass
`user-desktop` only when intentionally selecting the live desktop; the wrapper
requires the target explicitly and invokes the timer without evaluating its
arguments.

These are composable examples, not app-specific automation APIs: scripts should
select their target, identify one unambiguous window, check preconditions,
perform only the intended action, and verify the outcome.
