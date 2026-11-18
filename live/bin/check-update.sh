#!/bin/bash
# Weekly: ask OpenAlice if a newer stable release exists. Changes nothing; tells Lorenzo on Telegram once per version.
set -u
source "$(dirname "$0")/../.env" 2>/dev/null || true
TOKEN="${TG_BOT_TOKEN:-}"; CHAT="${TG_CHAT_ID:-}"
SEEN=/home/kyubi/Polar/live/state/update-notified.txt
J=$(openalice update --check --json --channel stable 2>/dev/null) || exit 1
read -r STATUS CUR LATEST < <(python3 -c 'import json,sys;d=json.loads(sys.argv[1]);print(d["status"],d["currentVersion"],d["latestVersion"])' "$J")
echo "$(date -u +%FT%TZ) status=$STATUS current=$CUR latest=$LATEST"
[ "$STATUS" = "current" ] && exit 0
[ "$(cat $SEEN 2>/dev/null)" = "$LATEST" ] && exit 0
MSG="⬆️ OpenAlice $LATEST is available (you run $CUR, stable channel).%0AUpgrade when no entry is pending:%0A<code>bash ~/polar/bin/upgrade-openalice.sh</code>%0AIt backs up, upgrades, restarts, smoke-tests the Polar scripts and rolls back by itself if anything fails.%0ANotes: https://github.com/TraderAlice/OpenAlice/releases/tag/v$LATEST"
curl -s -X POST "https://api.telegram.org/bot$TOKEN/sendMessage" -d "chat_id=$CHAT" -d "parse_mode=HTML" -d "text=$(printf '%b' "${MSG//%0A/\\n}")" >/dev/null && echo "$LATEST" > "$SEEN"
