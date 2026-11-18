#!/bin/bash
# Upgrade OpenAlice (npm global, root-owned) with backup, smoke test and automatic rollback.
# Run by Lorenzo (needs sudo for npm). Never touches positions or orders.
set -u
source "$(dirname "$0")/../.env" 2>/dev/null || true
TOKEN="${TG_BOT_TOKEN:-}"; CHAT="${TG_CHAT_ID:-}"
tg() { curl -s -X POST "https://api.telegram.org/bot$TOKEN/sendMessage" -d "chat_id=$CHAT" -d "parse_mode=HTML" --data-urlencode "text=$1" >/dev/null; }
B=/home/kyubi/Polar/live/bin; S=/home/kyubi/Polar/live/state

PEND=$(python3 -c "import json;print(len(json.load(open('$S/pending-orders.json'))))" 2>/dev/null || echo 0)
[ "$PEND" != "0" ] && { echo "Entry orders pending ($PEND): not now, retry once they fill or expire."; exit 1; }

OLD=$(openalice version | grep -o '[0-9][0-9.]*' | head -1)
LATEST=$(npm view openalice version)
[ "$OLD" = "$LATEST" ] && { echo "Already on $OLD."; exit 0; }
echo "Upgrading $OLD -> $LATEST"

bash /home/kyubi/Polar/github-backup/backup.sh || { echo "backup failed, aborting"; exit 1; }

smoke() {
  python3 $B/portfolio.py --dry >/dev/null || return 1
  python3 $B/regime.py --dry >/dev/null || return 1
  python3 $B/guardian.py >/dev/null || return 1
  python3 $B/scanner.py --ignore-gates >/dev/null || return 1
  python3 $B/audit.py --dry >/dev/null || return 1
}

sudo npm install -g "openalice@$LATEST" || { echo "npm install failed, nothing changed"; exit 1; }
systemctl --user restart openalice.service; sleep 25
if smoke; then
  tg "✅ OpenAlice upgraded $OLD -> $LATEST. Smoke test passed (account, guardian, scanner, portfolio, regime, audit)."
  echo "OK"
else
  sudo npm install -g "openalice@$OLD" && systemctl --user restart openalice.service
  tg "⚠️ OpenAlice $LATEST failed the smoke test, rolled back to $OLD. Polar scripts may need a change for $LATEST."
  echo "ROLLED BACK"; exit 1
fi
