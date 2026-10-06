# Polar — deployment

## Fresh machine setup

```bash
git clone <repo> ~/Polar
cd ~/Polar
pip install -r requirements.txt

# Copy and fill in your secrets
cp .env.example .env
$EDITOR .env
```

## Systemd services (user scope — no root needed)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now polar-vpa.service polar-weekend-hunter.service
```

## Live trading activation checklist

Both daemons start in **paper mode** by default. Work through this list in
order before setting `POLAR_LIVE_TRADING=true`.

### 1. Smoke-test the Bitget connection

```bash
python3 -m Core.Library.smoke_test_bitget
```

Expects: "smoke test PASSED". This places a ~6.50 USDT test order and
immediately cancels it to verify credentials and contract specs.

### 2. Seed the VPA live baseline

```bash
# Replace 1000 with your actual account balance in USDT
echo '{"initial_equity_usdt": 1000}' > Core/vpa/data/live_baseline.json
```

The drawdown ladder in `Core/Library/risk.py` scales position sizes and halts
new opens at 30% drawdown relative to this number. Keep it up to date after
large deposits or withdrawals.

### 3. Enable live trading

In `.env`:

```
POLAR_LIVE_TRADING=true
```

Per-core overrides (optional):

```
POLAR_LIVE_TRADING_VPA=false       # keep VPA in paper while WH goes live
POLAR_LIVE_TRADING_VPA=true        # arm VPA even if global flag is false
```

### 4. Restart the services

```bash
systemctl --user restart polar-vpa.service polar-weekend-hunter.service
systemctl --user status polar-vpa.service polar-weekend-hunter.service
```

### 5. Confirm first cycle

```bash
journalctl --user -u polar-vpa.service -f
```

Look for `[vpa] LIVE opened …` on the next entry bar after a pending signal.

---

## Kill switches

**Stop new opens immediately (keeps venue-side stops running):**

```bash
touch Core/vpa/data/HALT
touch Core/weekend_hunter/data/HALT
```

**Close all open positions now:**

```bash
python3 -m Core.vpa.execution --flatten
python3 -m Core.weekend_hunter.execution --flatten
```

**Full stop:**

```bash
systemctl --user stop polar-vpa.service polar-weekend-hunter.service
```
