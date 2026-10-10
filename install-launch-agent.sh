#!/bin/zsh
set -euo pipefail

readonly PROJECT_DIR="${0:A:h}"
readonly CONFIG="$PROJECT_DIR/config.json"
readonly LABEL="com.local.imessage-dsh-bridge"
readonly LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
readonly PLIST_TARGET="$LAUNCH_AGENT_DIR/$LABEL.plist"
readonly LOG_DIR="$HOME/Library/Logs/iMessageDSHarness"
readonly RUNTIME_DIR="$HOME/Library/Application Support/iMessageDSHarness"

if [[ ! -f "$CONFIG" ]]; then
  print -u2 "Missing config.json; copy and edit config.example.json first."
  exit 2
fi

/usr/bin/python3 "$PROJECT_DIR/bridge.py" --config "$CONFIG" --check
/bin/launchctl bootout "gui/$(/usr/bin/id -u)/$LABEL" 2>/dev/null || true
for _ in {1..20}; do
  if ! /bin/launchctl print "gui/$(/usr/bin/id -u)/$LABEL" >/dev/null 2>&1; then
    break
  fi
  /bin/sleep 0.25
done
if /bin/launchctl print "gui/$(/usr/bin/id -u)/$LABEL" >/dev/null 2>&1; then
  print -u2 "Could not stop the running bridge; installation was cancelled before replacing files."
  exit 1
fi
/bin/mkdir -p "$LOG_DIR" "$RUNTIME_DIR" "$LAUNCH_AGENT_DIR"
/usr/bin/install -m 700 "$PROJECT_DIR/bridge.py" "$RUNTIME_DIR/bridge.py"
/usr/bin/install -m 600 "$CONFIG" "$RUNTIME_DIR/config.json"

/usr/bin/python3 - "$PLIST_TARGET" "$RUNTIME_DIR" "$LOG_DIR" "$LABEL" <<'PY'
import plistlib
import sys

target, runtime, logs, label = sys.argv[1:]
data = {
    "Label": label,
    "ProgramArguments": ["/usr/bin/python3", f"{runtime}/bridge.py", "--config", f"{runtime}/config.json"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "StandardOutPath": f"{logs}/launchd.out.log",
    "StandardErrorPath": f"{logs}/launchd.err.log",
}
with open(target, "wb") as output:
    plistlib.dump(data, output)
PY
/bin/chmod 600 "$PLIST_TARGET"

/bin/launchctl bootstrap "gui/$(/usr/bin/id -u)" "$PLIST_TARGET"
/bin/launchctl enable "gui/$(/usr/bin/id -u)/$LABEL"
print "Installed and started $LABEL"
