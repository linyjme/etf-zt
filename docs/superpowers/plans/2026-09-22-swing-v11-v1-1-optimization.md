# ETF Swing v1.1 Optimization Implementation Plan

> Execute in phases. Keep `SWING_V1` unchanged and keep `SWING_V11_SHADOW` read-only and non-executable.

## Goal

Align the V11 shadow strategy with ETF trading handbook v1.1: completed-week data, stable indicator contracts, market-index environment, complete ETF metadata and relative strength, then entry rules, position actions, UI evidence and shadow validation.

## Phase P0 — data contracts

1. Add holiday-aware completed-week aggregation. Every non-trailing week is complete; the trailing week is complete only when Friday traded or the calendar confirms no remaining trading day.
2. Normalize `calculate_v11_indicators` output at the evaluator boundary so direct evaluator calls and service calls have identical flat evidence.
3. Add environment context from CSI 300 (`000300`) and CSI 1000 (`000852`), with two-day confirmation and completed-week CSI 300 hard defense override.
4. Require complete metadata categories (`BROAD`, `SECTOR`, `CROSS_BORDER`, `GOLD`), environment index and correlation group for enabled ETFs; fail closed when missing.
5. Calculate 20-day relative strength against the mapped environment index and pass it into `V11Context`.

## Phase P1 — entry and risk alignment

1. Implement handbook T1-T5, A1-A7 and B1-B8 without A/B cross-blocking.
2. Use handbook RSI recovery, MACD trigger, Bollinger, long-upper-shadow, 60-day return and 250-day trend definitions.
3. Use initial stops `max(pullback_low*0.99, entry-2*ATR)` for A and `max(box_high*0.99, entry-2*ATR)` for B; reject category width caps.
4. Enforce target capital 2,000 CNY, hard cap 5,000 CNY, lot size, cash and risk caps; apply half-size exactly once.

## Phase P2 — position state machine

Add position state and deterministic actions for break-even at 1R, tracking at 2R/reduction, S1-S7, C1-C7, E1/E2/E3/T25, top-up, cooldown, T+1 sellable shares, no-switch and action priority `STOP_OR_EXIT > ENVIRONMENT > REDUCE > TOP_UP > ENTRY`.

## Phase P3 — presentation and validation

Require real current-day 14:45 evidence for `QUASI_CLOSE_1445`; expose environment, RS, gates, indicators, stop, R, tracking, cooldown and reasons; never show a candidate when blocked. Extend shadow reports to separate no-signal, data-blocked and rule-blocked outcomes.

## Verification

Add regression tests for each phase, run focused tests after every change, then full discovery and compile checks. Confirm V1 behavior is unchanged, V11 is never executable, and no data-quality failure can produce an action candidate.
