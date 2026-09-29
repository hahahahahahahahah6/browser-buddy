#!/usr/bin/env bash
# Install the Browser Buddy native messaging host manifest.
#
# Usage:
#   ./install.sh <extension-id>
# or:
#   BROWSER_BUDDY_EXTENSION_ID=<id> ./install.sh
#
# The extension id is shown on chrome://extensions after loading the
# unpacked extension (enable "Developer mode" first).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_ID="${1:-${BROWSER_BUDDY_EXTENSION_ID:-}}"
if [[ -z "$EXT_ID" ]]; then
  echo "error: extension id required." >&2
  echo "usage: ./install.sh <extension-id>" >&2
  exit 1
fi

HOST_PY="$SCRIPT_DIR/browser_buddy_host.py"
chmod +x "$HOST_PY"

case "$(uname -s)" in
  Darwin)
    DEST_DIR="$HOME/Library/Application Support/Google Chrome/NativeMessagingHosts"
    ;;
  Linux)
    DEST_DIR="$HOME/.config/google-chrome/NativeMessagingHosts"
    ;;
  *)
    echo "error: unsupported OS: $(uname -s)" >&2
    exit 1
    ;;
esac

mkdir -p "$DEST_DIR"
sed -e "s#__EXTENSION_ID__#$EXT_ID#g" \
    -e "s#__HOST_PATH__#$HOST_PY#g" \
    "$SCRIPT_DIR/com.browserbuddy.host.json.template" \
    > "$DEST_DIR/com.browserbuddy.host.json"

echo "installed native host manifest -> $DEST_DIR/com.browserbuddy.host.json"
echo "host: $HOST_PY"
echo "allowed extension: chrome-extension://$EXT_ID/"
echo "Restart Chrome, then check the extension's service worker console for '[browser-buddy] native host connected'."
