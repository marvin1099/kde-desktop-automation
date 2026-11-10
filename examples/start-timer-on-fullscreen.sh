#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage:
  start-timer-on-fullscreen.sh <session-id|user-desktop> <exact-app-id> [--title substring] <fullscreen|fills-display> -- <timer-command> [args...]

Waits for one uniquely matching window to meet the selected presentation
condition, rechecks the same window, then replaces this process with the timer
command. `user-desktop` explicitly opts into observing the live desktop.
Requires kde-ai, jq, and Bash.
EOF
    exit 2
}

(( $# >= 4 )) || usage
target=$1
app_id=$2
shift 2
selector_args=(--app-id "$app_id")
if [[ ${1:-} == --title ]]; then
    (( $# >= 2 )) || usage
    selector_args+=(--title "$2")
    shift 2
fi
(( $# >= 2 )) || usage
presentation=$1
shift
[[ ${1:-} == -- ]] || usage
shift
(( $# > 0 )) || usage

case "$presentation" in
    fullscreen) wait_predicate=--fullscreen ;;
    fills-display) wait_predicate=--fills-display ;;
    *) usage ;;
esac

if [[ "$target" == "user-desktop" ]]; then
    printf 'Watching the explicitly selected live desktop for app %s.\n' "$app_id" >&2
fi

while :; do
    if result=$(kde-ai --session "$target" window wait \
        "${selector_args[@]}" "$wait_predicate" \
        --timeout 60 --json 2>&1); then
        window_id=$(jq -er '.data.window.window_id' <<<"$result")

        if ! inspected=$(kde-ai --session "$target" window inspect "$window_id" --json 2>&1); then
            error_code=$(jq -r '.error.code // empty' <<<"$inspected" 2>/dev/null || true)
            if [[ "$error_code" == "stale_target" ]]; then
                continue
            fi
            printf 'Could not re-inspect detected window:\n%s\n' "$inspected" >&2
            exit 1
        fi

        if ! jq -e --arg app_id "$app_id" --arg presentation "$presentation" \
            '.data.window.app_id == $app_id and ($presentation != "fullscreen" or .data.window.fullscreen == true)' \
            <<<"$inspected" >/dev/null; then
            printf 'Window changed before timer start; waiting again.\n' >&2
            continue
        fi

        if [[ "$presentation" == "fills-display" ]]; then
            title=$(jq -er '.data.window.title' <<<"$inspected")
            if ! rechecked=$(kde-ai --session "$target" window wait \
                --app-id "$app_id" --title "$title" --fills-display \
                --timeout 1 --json 2>&1); then
                error_code=$(jq -r '.error.code // empty' <<<"$rechecked" 2>/dev/null || true)
                if [[ "$error_code" == "window_wait_timeout" ]]; then
                    continue
                fi
                printf 'Could not recheck display coverage:\n%s\n' "$rechecked" >&2
                exit 1
            fi
            if ! jq -e --arg window_id "$window_id" \
                '.data.window.window_id == $window_id and (.data.matched_displays | length > 0)' \
                <<<"$rechecked" >/dev/null; then
                printf 'Window or display coverage changed before timer start; waiting again.\n' >&2
                continue
            fi
        fi

        export KDE_AI_TARGET="$target"
        export KDE_AI_WINDOW_ID="$window_id"
        printf 'Detected %s window %s (%s); starting timer.\n' \
            "$presentation" "$window_id" "$app_id" >&2
        exec "$@"
    else
        error_code=$(jq -r '.error.code // empty' <<<"$result" 2>/dev/null || true)
        if [[ "$error_code" == "window_wait_timeout" ]]; then
            last_title=$(jq -r \
                '.error.details.last_observation.window.title // "no matching window"' \
                <<<"$result" 2>/dev/null || true)
            printf 'Still waiting for fullscreen %s (last match: %s).\n' \
                "$app_id" "$last_title" >&2
            continue
        fi
        printf 'Fullscreen wait failed:\n%s\n' "$result" >&2
        exit 1
    fi
done
