# Shadow Mode

Shadow mode is the default production state. It allows all internal analysis, storage, grading, and reporting while blocking public/VIP Discord delivery.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MLB_SHADOW_MODE` | `true` | Enable shadow mode |
| `MLB_LIVE_DELIVERY_ACKNOWLEDGED` | `false` | Operator acknowledged live delivery |

**Priority:** Environment variables > config file > defaults.

## How it works

- `ShadowConfig.is_delivery_blocked()` returns `True` when shadow mode is ON
- `ShadowConfig.can_enable_delivery()` requires shadow mode OFF AND acknowledgement
- `ShadowConfig.block_reasons()` lists why delivery is blocked

## Shadow config file

Located at `data/shadow_config.json`:

```json
{
  "shadow_mode": true,
  "live_delivery_acknowledged": false
}
```

Use `save_shadow_config()` / `load_shadow_config()` to manage.

## Usage

```python
from src.shadow_mode import load_shadow_config

shadow = load_shadow_config()
if shadow.is_delivery_blocked():
    print("Delivery blocked:", shadow.block_reasons())
```

## CLI

```bash
# View current state
python -c "from src.shadow_mode import load_shadow_config; print(load_shadow_config().__dict__)"
```
