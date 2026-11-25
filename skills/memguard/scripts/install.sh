#!/bin/bash
# Install or update memguard as a user LaunchAgent, or remove it. macOS only.
# Usage: install.sh [--uninstall]
# The running copy goes to ~/.local/share/memguard: macOS blocks a python started by launchd from
# reading ~/Documents, where clones of this repo often live.
set -euo pipefail

LABEL=local.memguard
DEST="$HOME/.local/share/memguard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/memguard.log"
DOMAIN="gui/$(id -u)"
PYTHON=/usr/bin/python3

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN/$LABEL"
  for _ in 1 2 3 4 5 6 7 8 9 10; do  # bootstrap fails while the old job is still going away
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
  done
fi

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$PLIST" "$DEST/memguard.py"
  rmdir "$DEST" 2>/dev/null || true
  echo "memguard removed (log kept: $LOG)"
  exit 0
fi

"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
  || { echo "memguard needs $PYTHON 3.9+ (xcode-select --install)" >&2; exit 1; }

mkdir -p "$DEST" "$(dirname "$PLIST")" "$(dirname "$LOG")"
cp "$(dirname "$0")/memguard.py" "$DEST/memguard.py"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$DEST/memguard.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
EOF
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "memguard running ($(launchctl print "$DOMAIN/$LABEL" | awk '$1 == "pid" {print "pid " $3; exit}'))"
echo "log: $LOG"
