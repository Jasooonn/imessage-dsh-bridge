#!/bin/zsh
set -euo pipefail

readonly PROJECT_DIR="${0:A:h}"
readonly APP_PATH="$HOME/Applications/iMessage Attachment Helper.app"
readonly CONTENTS="$APP_PATH/Contents"

if ! command -v swiftc >/dev/null 2>&1; then
  print -u2 "swiftc is required; install Xcode Command Line Tools first."
  exit 2
fi
if [[ -e "$APP_PATH" ]]; then
  print -u2 "Helper already exists at $APP_PATH; move it aside before rebuilding."
  exit 2
fi

/bin/mkdir -p "$CONTENTS/MacOS"
/usr/bin/install -m 644 "$PROJECT_DIR/AttachmentCopyHelper.Info.plist" "$CONTENTS/Info.plist"
swiftc "$PROJECT_DIR/AttachmentCopyHelper.swift" -o "$CONTENTS/MacOS/attachment-copy-helper"
/usr/bin/codesign --force --sign - "$APP_PATH"
print "Built $APP_PATH"
print "Grant this helper access to Messages attachments in macOS Privacy & Security before testing inbound files."
