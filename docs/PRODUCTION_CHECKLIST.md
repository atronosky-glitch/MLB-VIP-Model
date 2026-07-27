# Production Checklist

Pre-live verification items required before enabling delivery.

## Categories

### Delivery Safety
- [ ] delivery-01: Review Discord webhook URLs
- [ ] delivery-02: Verify message formatting
- [ ] delivery-03: Confirm delivery rate limiting
- [ ] delivery-04: Validate confidence filter thresholds

### Data Quality
- [ ] data-01: Inspect raw API response schemas
- [ ] data-02: Validate sportsbook mappings
- [ ] data-03: Validate market mappings
- [ ] data-04: Review data-quality findings

### YN Markets
- [ ] yn-01: Manual YN odds spot-check
- [ ] yn-02: YN line validation
- [ ] yn-03: YN recommendation review

### System
- [ ] sys-01: Verify backup and restore
- [ ] sys-02: Verify scheduler configuration
- [ ] sys-03: Verify health check accuracy
- [ ] sys-04: Verify logging output

### Monitoring
- [ ] mon-01: Review shadow dashboard
- [ ] mon-02: Review promotion criteria

## CLI Management

```bash
# View status
python -m src.manual_checklist status

# Mark item complete
python -m src.manual_checklist complete delivery-01 --notes "Verified"

# Mark item incomplete
python -m src.manual_checklist uncomplete delivery-01

# JSON output
python -m src.manual_checklist status --json
```

All required items (*) must be completed before live delivery.
