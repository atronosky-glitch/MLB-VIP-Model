# Phase 19 — Adaptive Learning Engine Architecture

**Status:** Architecture only. No runtime implementation, threshold change, delivery change, or betting behavior is included.

## 1. Objectives

Phase 19 turns each recommendation into an auditable research observation that can be followed from creation through settlement. It produces rolling performance and calibration evidence, then creates advisory configuration proposals for human review.

The engine must:

- preserve the recommendation, pricing, model, Pinnacle, closing-line, and settlement evidence used for every analysis;
- calculate rolling metrics by sportsbook, canonical market, league, player-prop type, edge bucket, and confidence bucket;
- distinguish descriptive performance from statistically supported conclusions;
- treat Pinnacle as a sharp reference and predictive benchmark, not as infallible ground truth;
- produce proposals only after sample-size and uncertainty gates pass; and
- never place bets, enable delivery, mutate thresholds, or activate a proposal automatically.

## 2. Existing Foundation

Phase 19 builds on these existing components rather than creating parallel sources of truth:

| Existing component | Phase 19 role |
| --- | --- |
| `historical_recommendations` | Immutable recommendation snapshot and creation-time model evidence |
| `closing_prices` | Closing price, closing line, and CLV evidence |
| `market_settlements` | Outcome and settlement status |
| `bet_units` | Risk, profit, and return units |
| `recommendation_traces` | Lifecycle events and operator/audit history |
| `scan_runs` | Run identity, completion, and data freshness context |
| `src/grading.py` | Deterministic O/U grading, CLV, and settlement calculations |
| `src/adaptive_learning.py` | Advisory analysis, score calibration, safety gates, and champion/challenger concepts |
| `config_versions` / learning tables | Versioned proposals and human approval state |

Known gap: Pinnacle reference and approval fields are computed in the pipeline but are not consistently persisted on `historical_recommendations`. Phase 19 must close that persistence gap in the evidence model before using Pinnacle metrics for learning.

## 3. Lifecycle Model

Every recommendation receives one stable `recommendation_id` and follows this append-only lifecycle:

```text
CREATED
  -> OBSERVED (optional price snapshots)
  -> FROZEN
  -> QUALIFIED / RESEARCH_ONLY
  -> CLOSING_CAPTURED (or CLOSING_UNAVAILABLE)
  -> SETTLED (WIN / LOSS / PUSH / VOID / CANCELLED / UNRESOLVED)
  -> INCLUDED / EXCLUDED from learning analysis
```

State transitions are recorded as trace events. Corrections do not overwrite historical evidence; they create a new correction or manual-override event with actor, reason, timestamp, and source.

### 3.1 Creation evidence

The creation record must include:

- stable event ID, player ID, player name snapshot, league, market type, canonical prop type, period, line, side, and alternate-line flag;
- target sportsbook and sportsbook identifier;
- offered American and decimal odds;
- implied probability;
- market fair probability and fair odds, when available;
- edge metric and edge definition: O/U EV or Y/N price advantage;
- model score and confidence score, with version and component explanation;
- Pinnacle reference presence, exact reference line, reference odds, no-vig fair probability, and reference source version;
- scan run ID, observation time, creation time, data source, freshness status, recommendation tier, and qualification result.

No player or sportsbook may be identified by array order or display text when a stable ID exists.

### 3.2 Observation evidence

Use an append-only `recommendation_observations` concept for prices seen after creation. Each observation is keyed by recommendation ID, observation timestamp, sportsbook, line, side, and source run. It may record:

- offered price and line;
- Pinnacle price and line when present;
- market reference/fair probability;
- availability and validation status;
- source run ID and observation source.

Duplicate observations are idempotent. Observations from different lines, periods, sides, players, or events are never merged.

### 3.3 Closing evidence

Closing data remains linked by recommendation ID and must record:

- closing sportsbook and exact closing line;
- closing American and decimal odds;
- closing implied probability;
- observed-at timestamp and source run ID;
- CLV probability, price, and line-change classification;
- explicit `clv_available` and `closing_available` flags.

Missing closing data is a known outcome, not a fabricated zero. CLV analyses use only records with valid closing evidence.

### 3.4 Settlement evidence

Settlement is separate from recommendation quality and bet qualification. It must record:

- final result: `WIN`, `LOSS`, `PUSH`, `VOID`, `CANCELLED`, or `UNRESOLVED`;
- final stat or event result used to grade;
- settlement source, source timestamp, grader version, and settlement reason;
- risk units, profit units, and return units for settled records;
- manual override history when applicable.

YN markets remain unresolved until a verified result source and explicit settlement rule exist.

## 4. Logical Data Model

The implementation may use migrations that extend existing tables or create normalized tables, but the logical contracts are:

### `recommendation_evidence`

One immutable creation-time row per recommendation. It is the learning fact table’s identity anchor and contains all creation, fair-value, confidence, edge, Pinnacle, and dimension fields.

Required dimensions include `recommendation_id`, `event_id`, `league`, `player_id`, `market_type`, `prop_type`, `sportsbook`, `side`, `period`, `line`, `is_alt_line`, `scan_run_id`, and `created_at`.

Required measures include offered odds, implied probability, fair probability, EV or YN edge, confidence, model score, Pinnacle reference values, and source/version fields.

### `recommendation_observations`

Append-only price and reference observations linked to the recommendation. Unique identity must include recommendation ID, observation time, sportsbook, line, side, and source run.

### `recommendation_outcomes`

A read-optimized or materialized join of recommendation evidence, closing evidence, settlement, and units. It is never the authoritative write target; authoritative writes remain in existing recommendation, closing, settlement, and units tables.

### `learning_metric_runs`

Stores the analysis window, as-of timestamp, query/config version, population filters, sample-gate version, and generated metric summary. A metric run must be reproducible from the underlying facts.

### `learning_segment_metrics`

Stores one row per metric run, dimension, segment value, and window. It includes counts, settled counts, wins/losses/pushes, ROI, units, hit rate, CLV, confidence intervals, calibration errors, and eligibility status.

### `learning_proposals`

Stores advisory proposals only:

- proposal ID and metric run ID;
- category and affected parameter;
- current value and proposed value;
- evidence and comparison population;
- sample size, confidence interval, effect size, and overfitting risk;
- expected volume and risk impact;
- status: `INSUFFICIENT_DATA`, `OBSERVE`, `CANDIDATE`, `VALIDATED`, `REJECTED`, `APPROVED`, or `IMPLEMENTED`;
- reviewer, review timestamp, approval rationale, and resulting config version.

`IMPLEMENTED` means a human-approved deployment was separately applied and recorded. It is never set by the learning engine itself.

## 5. Rolling Metrics

The engine computes each segment over explicitly named windows, at minimum:

- trailing 7 days for operational monitoring;
- trailing 30 days for primary rolling decisions;
- trailing 90 days for stability context;
- all-time history for baseline comparison.

Every metric carries `window_start`, `window_end`, `as_of`, and the exact population definition. A recommendation is counted once per recommendation ID, and only settled outcomes enter win/loss/ROI metrics.

Required dimensions:

- sportsbook;
- canonical market type;
- league;
- player prop type, separated from display market labels;
- edge bucket, with O/U EV and Y/N price advantage buckets kept semantically separate;
- confidence bucket;
- optional combined dimensions only when the sample gate explicitly permits them.

Required base metrics:

- eligible, settled, unresolved, and excluded counts;
- wins, losses, pushes, hit rate, risk units, profit units, ROI, and average return;
- average and median offered odds and implied break-even probability;
- average and median edge, confidence, model score, and CLV;
- closing-line availability and line-change rate;
- confidence intervals and minimum detectable effect where applicable;
- data-quality exclusions and missingness rates.

Small samples are visible descriptively but are not promoted to learning conclusions.

## 6. Statistical Gates

Statistical significance is a safety gate, not a guarantee of future profit. A segment can be labeled `INSUFFICIENT_DATA` even when its observed ROI is positive.

Minimum eligibility requires all of:

- the configured minimum settled count for the dimension and market variance class;
- at least the configured minimum number of betting days;
- sufficient sportsbook and event diversity to prevent one slate or one book dominating;
- complete outcome and odds fields for the metric being evaluated;
- no unresolved data-quality or identity violations;
- a confidence interval or posterior interval reported with the estimate;
- comparison against a defined baseline, not zero alone, when proposing a change.

High-variance markets such as home runs, stolen bases, and other configured outlier markets use stricter gates. The existing `src/adaptive_learning.py` constants are the initial policy source; Phase 19 should version any future gate changes.

Recommended methods:

- Wilson or beta-binomial intervals for hit rates;
- bootstrap or conservative intervals for ROI and CLV;
- Brier score and log loss for probability forecasts;
- calibration slope/intercept and expected calibration error for confidence/probability calibration;
- shrinkage or hierarchical estimates for sportsbook and market segments with uneven volume.

No proposal is generated from a point estimate without uncertainty and sample metadata.

## 7. Learning Analyses

### 7.1 Market and sportsbook performance

Identify consistently profitable markets using rolling ROI, CLV, stability across windows, and diversity gates. Identify underperforming sportsbooks using negative ROI and/or negative CLV only after controlling for market mix, edge bucket, confidence, and sample size. A sportsbook is not penalized solely because it receives more difficult markets.

### 7.2 Confidence calibration

For each confidence bucket, compare predicted confidence or probability with observed settled outcomes. Report calibration error, reliability count, Brier score, hit rate, ROI, and interval. Confidence is not treated as a probability unless the model contract explicitly says so; the metric definition must name the target.

### 7.3 EV calibration

For each O/U EV bucket, compare predicted EV with realized ROI, profit units, CLV, and closing availability. Report selection bias, average odds, sample size, and whether realized performance is statistically distinguishable from the comparison baseline. Y/N edge buckets use price-advantage metrics and are never relabeled as EV.

### 7.4 Pinnacle prediction accuracy

Pinnacle analysis has two separate targets:

1. **Price agreement:** compare model fair probability and offered market prices with Pinnacle no-vig probability on the exact same event, player, market, line, side, and period.
2. **Outcome accuracy:** when a verified settlement exists, compare Pinnacle implied probability and model probability with outcomes using Brier score, log loss, calibration, and accuracy.

Pinnacle is a reference signal, not a guaranteed truth source. Line mismatch, one-sided Pinnacle data, missing Pinnacle data, and Pinnacle itself being the target sportsbook are excluded or separately reported.

## 8. Proposal and Approval Workflow

```text
metric run
  -> sample and data-quality gates
  -> advisory proposal
  -> human review
  -> approved config version
  -> optional shadow/canary validation
  -> manually deployed config
  -> post-deployment evaluation
```

Rules:

- The engine may write metrics and proposals, never production threshold values.
- `prop_config.py` remains unchanged until a human applies an approved proposal through an explicit deployment action.
- Every proposal includes a before/after diff, expected volume effect, risk, rollback value, evidence window, and model/config version.
- Approval requires an actor, timestamp, reason, and review status.
- Champion/challenger evaluation remains holdout-based and gate-only; challenger results cannot become champion automatically.
- Shadow mode and delivery gates remain independent safety controls.

## 9. Failure and Data-Quality Handling

- Missing closing lines produce `CLOSING_UNAVAILABLE`, not zero CLV.
- Missing Pinnacle data produces `PINNACLE_UNAVAILABLE`, not a market-median value mislabeled as Pinnacle.
- Unsettled recommendations remain visible but are excluded from outcome metrics.
- Duplicate or corrected source records are idempotent and traceable.
- Invalid mappings, stale rows, impossible prices, and non-approved statuses are excluded from learning populations and counted in data-quality metrics.
- A metric run that cannot reproduce its population is invalid and cannot produce a proposal.

## 10. Implementation Sequence

1. Freeze the evidence contract and backfill/persist Pinnacle and model fields at recommendation creation.
2. Add observation and outcome joins with idempotent lifecycle events.
3. Add rolling metric runs and segment metrics for one dimension at a time.
4. Add confidence and EV calibration reports.
5. Add Pinnacle price-agreement and outcome-accuracy reports.
6. Add statistically gated learning proposals and human approval records.
7. Add dashboard read-only views and exportable audit reports.
8. Run shadow validation before any manually approved parameter deployment.

## 11. Explicit Non-Goals

- No automatic bets or order placement.
- No automatic threshold, weight, market, sportsbook, or delivery changes.
- No use of player names or array position as identity keys.
- No cross-line or cross-period aggregation.
- No treating positive observed ROI from a small sample as a validated edge.
- No production migration that drops or rewrites historical evidence.

## 12. Acceptance Criteria

Phase 19 architecture is ready for implementation when:

- the evidence, observation, closing, settlement, metric, and proposal contracts are approved;
- every metric has a reproducible population and explicit window;
- every proposal has sample, uncertainty, baseline, and approval fields;
- the Pinnacle reference semantics and exclusions are documented;
- the no-auto-change invariant is covered by tests before implementation begins; and
- the dashboard design is read-only until a separate human approval action is introduced.
