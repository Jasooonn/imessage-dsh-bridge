#!/bin/zsh
set -euo pipefail

readonly LABEL="com.local.imessage-dsh-bridge"
readonly PLIST_TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

/bin/launchctl bootout "gui/$(/usr/bin/id -u)/$LABEL" 2>/dev/null || true
if [[ -f "$PLIST_TARGET" ]]; then
  readonly TRASH_TARGET="$HOME/.Trash/$LABEL-$(/bin/date +%Y%m%d-%H%M%S).plist"
  /bin/mv "$PLIST_TARGET" "$TRASH_TARGET"
  print "Moved the LaunchAgent plist to Trash: $TRASH_TARGET"
fi
print "Stopped $LABEL; task history, attachments, and source files were kept."
