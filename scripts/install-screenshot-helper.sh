#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
prefix="${HOME}/.local"
data_home="${XDG_DATA_HOME:-${prefix}/share}"
build_dir="${project_root}/build/screenshot-helper"

if ! command -v cmake >/dev/null 2>&1; then
    printf 'cmake is required to build the Screenshot2 helper.\n' >&2
    exit 1
fi

cmake -S "${project_root}/native/screenshot-helper" -B "${build_dir}" \
    -DCMAKE_INSTALL_PREFIX="${prefix}" \
    -DCMAKE_INSTALL_DATADIR="${data_home}"
cmake --build "${build_dir}" --target kde-desktop-screenshot \
    --parallel "${CMAKE_BUILD_PARALLEL_LEVEL:-2}"
cmake --install "${build_dir}"

if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 --noincremental
else
    printf 'Warning: kbuildsycoca6 was not found; refresh KDE application services before capture.\n' >&2
fi

helper="${prefix}/bin/kde-desktop-screenshot"
desktop_entry="${data_home}/applications/kde-desktop-screenshot.desktop"
if [[ ! -x "${helper}" || ! -f "${desktop_entry}" ]]; then
    printf 'The user-local helper or its KWin authorization entry was not installed.\n' >&2
    exit 1
fi

printf 'Installed helper: %s\n' "${helper}"
printf 'KWin authorization entry: %s\n' "${desktop_entry}"
case ":${PATH}:" in
    *":${prefix}/bin:"*) ;;
    *) printf 'Add %s to PATH or set KDE_DESKTOP_SCREENSHOT_HELPER in the MCP launch environment.\n' "${prefix}/bin" ;;
esac
printf 'Restart existing private KWin sessions to copy the authorization entry into them.\n'
