#!/bin/zsh
set -euo pipefail

readonly PROJECT_DIR="${0:A:h}"
readonly CONFIG="$PROJECT_DIR/config.json"
readonly LABEL="com.local.imessage-dsh-bridge"
readonly LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
readonly PLIST_TARGET="$LAUNCH_AGENT_DIR/$LABEL.plist"
readonly LOG_DIR="$HOME/Library/Logs/iMessageDSHarness"
readonly RUNTIME_DIR="$HOME/Library/Application Support/iMessageDSHarness"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

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
/usr/bin/install -m 600 "$PROJECT_DIR/dsh_approval.mjs" "$RUNTIME_DIR/dsh_approval.mjs"
/usr/bin/install -m 600 "$PROJECT_DIR/dsh-approval.patch.yml" "$RUNTIME_DIR/dsh-approval.patch.yml"

/usr/bin/python3 - "$CONFIG" "$RUNTIME_DIR/config.json" <<'PY'
import json
import os
import shutil
import sys

source, target = sys.argv[1:]
with open(source, encoding="utf-8") as stream:
    data = json.load(stream)
if not data.get("imsg"):
    data["imsg"] = shutil.which("imsg")
if not data.get("imsg") or not os.path.isabs(data["imsg"]):
    raise SystemExit("imsg must resolve to an absolute executable path")
with open(target, "w", encoding="utf-8") as stream:
    json.dump(data, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
os.chmod(target, 0o600)
PY

/usr/bin/python3 - "$PLIST_TARGET" "$RUNTIME_DIR" "$LOG_DIR" "$LABEL" <<'PY'
import plistlib
import json
import os
import shutil
import sys

target, runtime, logs, label = sys.argv[1:]
with open(f"{runtime}/config.json", encoding="utf-8") as stream:
    config = json.load(stream)
tool_dirs = [os.path.dirname(config["imsg"])]
zstd = shutil.which("zstd")
if zstd:
    tool_dirs.append(os.path.dirname(zstd))
tool_dirs.extend(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
runtime_path = ":".join(dict.fromkeys(tool_dirs))
data = {
    "Label": label,
    "ProgramArguments": ["/usr/bin/python3", f"{runtime}/bridge.py", "--config", f"{runtime}/config.json"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "EnvironmentVariables": {"PATH": runtime_path},
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
