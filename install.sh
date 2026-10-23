#!/bin/sh
# Install Daily Brief on macOS or Linux.
#
#   ./install.sh            desktop launcher + daily 08:00 job
#   ./install.sh --no-job   desktop launcher only
#
# Safe to re-run; it overwrites what it owns and leaves the rest alone.
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LABEL="com.dailybrief.refresh"
WITH_JOB=1
[ "${1:-}" = "--no-job" ] && WITH_JOB=0

PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "error: python3 not found on PATH" >&2; exit 1; }
echo "python : $PY ($("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])'))"

chmod +x "$DIR/launch.sh" "$DIR/install.sh" 2>/dev/null || true

# ---------------------------------------------------------------- desktop entry
if [ "$(uname -s)" = "Darwin" ]; then
    # a .command file is the macOS equivalent of a desktop shortcut: double-click runs it
    TARGET="$HOME/Desktop/Daily Brief.command"
    printf '#!/bin/sh\nexec "%s" "%s"\n' "$DIR/launch.sh" "" > /dev/null 2>&1 || true
    cat > "$TARGET" <<EOF
#!/bin/sh
exec "$DIR/launch.sh"
EOF
    chmod +x "$TARGET"
    echo "desktop: $TARGET"
else
    DESKTOP_DIR="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
    [ -d "$DESKTOP_DIR" ] || DESKTOP_DIR="$HOME"
    TARGET="$DESKTOP_DIR/daily-brief.desktop"
    cat > "$TARGET" <<EOF
[Desktop Entry]
Type=Application
Name=Daily Brief
Comment=Today's AI and business brief
Exec=$DIR/launch.sh
Icon=$DIR/brief.ico
Terminal=false
Categories=News;
EOF
    chmod +x "$TARGET"
    # GNOME requires desktop files to be marked trusted before they launch
    command -v gio >/dev/null 2>&1 && gio set "$TARGET" metadata::trusted true 2>/dev/null || true
    echo "desktop: $TARGET"
fi

# ---------------------------------------------------------------- daily 08:00
if [ "$WITH_JOB" = "1" ]; then
    if [ "$(uname -s)" = "Darwin" ]; then
        PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
        mkdir -p "$(dirname "$PLIST")"
        cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/digest.py</string>
    <string>--quiet</string>
    <string>--if-stale</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/data/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/launchd.log</string>
</dict>
</plist>
EOF
        mkdir -p "$DIR/data"
        launchctl unload "$PLIST" 2>/dev/null || true
        launchctl load "$PLIST"
        echo "job    : launchd $LABEL (daily 08:00:00, plus at login)"
    else
        # systemd --user if available, else fall back to crontab
        if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
            UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
            mkdir -p "$UNITDIR"
            cat > "$UNITDIR/daily-brief.service" <<EOF
[Unit]
Description=Daily Brief generator

[Service]
Type=oneshot
ExecStart=$PY $DIR/digest.py --quiet --if-stale
EOF
            cat > "$UNITDIR/daily-brief.timer" <<EOF
[Unit]
Description=Rebuild the daily brief at 08:00

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
            systemctl --user daemon-reload
            systemctl --user enable --now daily-brief.timer
            echo "job    : systemd user timer daily-brief.timer (08:00, Persistent=true)"
        else
            LINE="0 8 * * * $PY $DIR/digest.py --quiet --if-stale"
            ( crontab -l 2>/dev/null | grep -vF "$DIR/digest.py"; echo "$LINE" ) | crontab -
            echo "job    : crontab entry added for 08:00"
        fi
    fi
fi

echo
echo "done. double-click the desktop launcher, or run: $DIR/launch.sh"
