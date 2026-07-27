# First Live Day

Guide for transitioning from shadow mode to live production.

## Prerequisites

1. Run `python -m src.live_readiness --skip-network` — must exit 0 or 1
2. Complete all items in the pre-live verification checklist
3. Promotion criteria must all be met
4. Operator must explicitly acknowledge and disable shadow mode

## Step-by-step

### 1. Verify readiness

```bash
python -m src.live_readiness --skip-network
```

### 2. Run canary test

```bash
python -m src.production_canary --no-write --json
```

Verify: status=success, no errors, expected sportsbooks present.

### 3. Complete manual checklist

```bash
python -m src.manual_checklist status
# Complete each item:
python -m src.manual_checklist complete delivery-01 --notes "Verified correct"
```

### 4. Check promotion criteria

```bash
python -m src.promotion --json
```

All criteria must show PASS.

### 5. Enable live delivery

```bash
python -m src.delivery_gate enable --confirm "ENABLE LIVE DELIVERY"
```

This:
- Sets `SHADOW_MODE=false`
- Sets `LIVE_DELIVERY_ACKNOWLEDGED=true`
- Persists gate state to `data/.delivery_gate.json`

### 6. Monitor shadow dashboard

```bash
python -m src.shadow_dashboard
```

Watch for critical data-quality findings in the first 24 hours.

### 7. Disable if issues arise

```bash
python -m src.delivery_gate disable
```

This immediately re-enables shadow mode.

## Rollback

At any point, run:

```bash
python -m src.delivery_gate disable
```

Shadow mode is re-enabled and all delivery is blocked.
