#!/bin/sh
# Open today's brief, refreshing it first if it is out of date.
# Mirrors what the Windows .vbs launcher does: show something immediately,
# regenerate in the background, let the page reload itself when it lands.
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HTML="$DIR/index.html"
PY=$(command -v python3 || command -v python || true)

[ -n "$PY" ] || { echo "python3 not found" >&2; exit 1; }

open_url() {
    if command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 &
    elif command -v open >/dev/null 2>&1; then open "$1"
    elif command -v sensible-browser >/dev/null 2>&1; then sensible-browser "$1" >/dev/null 2>&1 &
    else echo "open $1" >&2
    fi
}

if [ ! -f "$HTML" ]; then
    # nothing to show yet: build it, then open
    "$PY" "$DIR/digest.py" --quiet
    open_url "$HTML"
    exit 0
fi

# show what we have right away, then refresh in the background
open_url "$HTML"
"$PY" "$DIR/digest.py" --quiet --if-stale &
