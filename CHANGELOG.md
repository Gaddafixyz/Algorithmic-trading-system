# Changelog

All notable changes to this project are documented in this file.

> **Housekeeping note:** `[1.3.7]` through `[1.3.9]` in this file
> represent three colliding branch-local version sequences that were
> developed independently from the same `[1.3.6]` base and never merged
> until `[1.3.13]`. All entries below `[1.3.13]` and down through
> `[1.3.7]` have been retroactively renumbered into one sequential chain
> so each version label is unique; see `[1.3.13]`'s own entry for the
> full old-label → new-label mapping and merge details. No entry content
> was changed — only header lines (and one internal cross-reference that
> pointed at a renumbered sibling entry).

## [1.3.29] - Full audit for improvements; fix untracked fire-and-forget task in `PaperExchangeClient`

### Context
Broad audit sweep of the current tree: TODO/FIXME/HACK scan, bare-except
scan, mutable-default-arg scan, `eval`/`exec`/`shell=True`/hardcoded-secret
scan, SQL-injection-pattern scan, `datetime.utcnow()` scan, silent
`except: pass` scan, AST-based unused-import scan, dependency-pinning
review. Clean on everything except one functional risk and a pile of
genuinely-unused imports (deferred — see below).

### Fixed — untracked fire-and-forget task (paper trading)
`paper_trading/paper_exchange_client.py::place_order()` spawned its
deferred market-order fill via a bare `asyncio.create_task(...)` with no
stored reference and no exception handling in `_simulate_fill()` itself.
Two real consequences: (1) the task's only reference was the event
loop's internal weak one, so it was a documented candidate for
mid-flight garbage collection; (2) any exception `_simulate_fill()`
raised (bad fill math, a broken `_fill_handler`, etc.) would surface
only via asyncio's default "Task exception was never retrieved" handler
at GC time — invisible to this app's actual logging, meaning a paper
fill could silently fail to ever reach `FillEngine`/`RiskManager` with
no record of why.

Added `_background_tasks: set[asyncio.Task[None]]`, a
`_spawn_fill_simulation()` wrapper that tracks the task and attaches
`_on_background_task_done()` as a done-callback (discards from the set;
logs via `logger.exception` if the task raised anything other than
`CancelledError`). `stop()` now cancels and `asyncio.gather`s any still-
pending tasks before returning — teardown, not a "wait for paper fills
to finish" step, so cancel rather than await-to-completion (consistent
with `shutdown.py`'s per-step try/except wrapping this call — a
lingering fill simulation must never delay or fail the rest of
teardown).

`tests/paper_trading/test_paper_exchange_client.py`: added
`TestBackgroundFillTaskTracking` (5 tests — task tracked while pending,
removed once done, exception logged not swallowed, `stop()` cancels
pending tasks, `stop()` is a no-op with nothing pending). Existing
`TestMarketOrders` tests are unaffected — the deferred-fill mechanism
itself (`create_task` + `asyncio.sleep(0)` x2 to let it run) is
unchanged.

### Deferred — not fixed this pass
- **57 unused imports across 38 files** (AST + whole-file text scan,
  hand-verified a sample against `TYPE_CHECKING`/`__all__` false-
  positive patterns — genuine). Cosmetic, zero functional risk. Left
  for a dedicated cleanup pass rather than folded into this one.
- **Test-coverage gaps**: not reliably assessable in this sandbox
  (`pytest-cov`/`coverage.py` unavailable). A naive filename-matching
  check produced mostly false positives, since the suite groups tests by
  domain (`test_domain.py`, `test_indicators.py`, `test_smart_money.py`)
  rather than 1:1 with source files — real gaps need a local
  `pytest --cov` run.

### Verification
Full-tree `py_compile` clean. Allan: please run
`pytest tests/paper_trading/test_paper_exchange_client.py` locally,
then the full suite.

## [1.3.28] - Wire `RiskLevel`: drawdown-based classification, thresholds approved by Allan

### Wired
- `src/risk/risk_manager.py`: new module-level `classify_risk_level(drawdown_pct, max_drawdown_pct) -> RiskLevel`
  helper, bucketing current drawdown as a ratio of the configured
  `max_drawdown_pct` limit (not fixed percentages, since the limit
  itself is a per-deployment setting):
  - `LOW`: drawdown < 50% of the limit
  - `MEDIUM`: 50%-75% of the limit
  - `HIGH`: 75%-100% of the limit
  - `CRITICAL`: >= 100% of the limit — the same threshold at which
    `RiskManager._check_drawdown_limit` raises `RiskLimitExceededError`
  - `max_drawdown_pct <= 0` (misconfigured/disabled limit) returns
    `CRITICAL` rather than dividing by zero or silently reporting `LOW`.
  `RiskManager.stats`'s dict gained a `"risk_level"` key (`.value` string,
  matching the existing `"circuit_breaker"` key's convention).
- No new plumbing needed beyond that: `stats` already flows into the
  dashboard status payload as-is (`bootstrap/app.py`'s `risk_stats`/
  `"risk": self.risk_manager.stats` sites), so `risk_level` is visible
  there automatically.

### Tests
`tests/risk/test_risk_manager.py`: added `TestClassifyRiskLevel` (8
cases covering every boundary — 50%/75%/100% exactly — plus the
zero/negative-limit guard), and 2 assertions to the existing `stats`
coverage (`risk_level` key present, `LOW` at zero drawdown).

### Verification
Full-tree `py_compile` clean. Allan: please run
`pytest tests/risk/test_risk_manager.py` locally, then the full suite.

## [1.3.27] - Wire the 4 exceptions flagged in `[1.3.26]` as requiring a behavior change: `NoSignalError`, `SignalGenerationError`, `RecordNotFoundError`, `ShutdownError`

### Context
`[1.3.26]` traced these 4 and found no *mechanical* wiring site for any
of them — each would mean converting an intentional return-None or
best-effort/log-and-continue contract into a raise. Explicit decision
this pass: wire them anyway. Documenting the resulting behavior changes
below since, unlike `[1.3.26]`'s `InitializationError` sites, these are
genuine semantic changes, not pure additions.

### Wired

**`NoSignalError` / `SignalGenerationError`** — `strategy/orchestrator.py`:
`_on_candle_closed()`'s per-strategy dispatch now wraps
`strategy.on_candle(...)`: any exception other than `NoSignalError` is
re-raised as `SignalGenerationError` (so the logged/observable failure
type is a domain exception, not whatever internal error the strategy
happened to raise), and a `None` result is converted into a raised
`NoSignalError`, caught immediately in the same dispatch loop as a
control-flow sentinel (`continue`, no error log — this is the expected
common case, not a fault). `strategy/signal_engine.py::analyze()` and
`strategy/base_strategy.py::on_candle()` are **unchanged** — both still
return `Signal | None` exactly as before. The raise/catch happens only
at the orchestrator boundary, not in the hot inner-loop functions
(`analyze()` runs once per closed candle per symbol/timeframe/strategy;
converting *that* layer to raise-based control flow would mean paying
exception overhead on every candle close across every registered
strategy, for what's the overwhelmingly common outcome — most candles
don't produce a signal — with no compensating benefit, since nothing
downstream of `analyze()`/`on_candle()` other than the orchestrator
itself consumes the result). This keeps the exceptions reachable and
observable for anything hooked at the orchestration layer while not
handing the whole engine a hot-path performance regression it doesn't
need. `tests/strategy/test_orchestrator.py`: added
`test_no_signal_raises_and_is_caught_as_control_flow` and
`test_strategy_exception_is_wrapped_as_signal_generation_error`;
existing return-None-focused tests are unaffected since `on_candle()`'s
own contract didn't change.

**`RecordNotFoundError`** — `persistence/repository.py::BaseRepository`
gained `get_by_id()`, additive alongside the existing `find_by_id()`
(which still returns `T | None` — `upsert()`/`delete_by_id()` both
depend on that contract internally and are unchanged).  `get_by_id()`
raises `RecordNotFoundError` when the record doesn't exist; use it at
any call site where a missing record is a fault to surface rather than
a branch to take. No call site converted to use it yet — none of
`OrderRepository`/`SignalRepository`/`TradeRepository`'s methods
currently need "must exist" semantics — so this pass adds the
capability without forcing an adoption site.
`tests/persistence/test_repository.py`: added `TestGetById` (3 tests).

**`ShutdownError`** — `bootstrap/shutdown.py::shutdown()`: every
existing per-step `try/except` is unchanged (still independently
isolated, still never blocks the next step) — added a `_failures: list[str]`
collected across all `logger.error(...)` sites (9 of the 10; the
`SystemShutdownEvent`-publish and listen-key-deletion steps stay
`logger.warning`-only and non-critical, matching their existing
comments). After every step has run, `shutdown()` now raises
`ShutdownError` summarizing whatever failed, if anything did — this
happens strictly after teardown completes, so it changes what the
*caller* observes, not the teardown sequence itself.
`bootstrap/lifecycle.py::LifecycleManager.run()` (the only caller) now
catches `ShutdownError` around its `shutdown()` call and logs it — `run()`
is invoked from a `finally` block, so this must not propagate and mask
whatever the `try` above it was already handling.
`tests/bootstrap/test_shutdown.py`: updated the 5 existing tests whose
injected failure is in a tracked step to `pytest.raises(ShutdownError)`
around the call (assertions on later-step side effects moved after,
unchanged); added `TestShutdownErrorAggregation` (4 tests covering the
happy path, multi-failure message aggregation, that the 2 non-critical
steps never raise, and that a last-step-only failure still raises).
`tests/bootstrap/test_lifecycle.py`: added
`test_shutdown_error_from_shutdown_does_not_propagate`.

### Verification
Full-tree `py_compile` clean. `pydantic`/`prometheus_client`/`pytest`
unavailable in this sandbox — Allan: please run
`pytest tests/strategy/test_orchestrator.py tests/persistence/test_repository.py
tests/bootstrap/test_shutdown.py tests/bootstrap/test_lifecycle.py` locally
first, then the full suite. The `NoSignalError`/`SignalGenerationError`
wiring in particular is worth watching under load — it's now on a much
hotter path than the other three.

## [1.3.26] - Investigated the 6 remaining dead exception subclasses + `RiskLevel`/`TradingMode` enums; wired `InitializationError` (2 sites)

### Context
Continuation of the dead-code disposition list left open after `[1.3.23]`'s
exception-subclass pass and `[1.3.24]`'s enum/function sweep. Traced each
of the 6 remaining exceptions and 2 enums against real call sites before
touching anything, per standing practice.

### Wired
- `src/bootstrap/startup.py`: replaced two generic `RuntimeError` raises
  with `InitializationError(component=..., reason=...)` — the
  paper/testnet safety cross-check and the production zero-equity guard.
  Both are genuine "component failed to initialize, refuse to start"
  cases matching the exception's own docstring, with no return-None-style
  precedent to preserve (unlike the 4 below). `tests/bootstrap/
  test_startup.py`'s 3 corresponding assertions updated to match.

### Investigated, no safe wiring site (flagged for Allan — same category as `[1.3.22]`'s `StaleDataError`)
- `NoSignalError` / `SignalGenerationError`: `strategy/signal_engine.py`
  deliberately returns `None` for "no valid setup found" (explicit
  docstring: "Returns: A Signal if a valid setup is found, else None").
  Real unexpected failures already propagate up through
  `orchestrator.py`'s existing broad `except Exception` at the one call
  site. Wiring either means converting an intentional return-None (or a
  currently-generic-exception) contract into a raise contract — a
  behavior change, not a mechanical wire.
- `RecordNotFoundError`: every lookup in `persistence/repository.py`
  (`find_by_id`, `find_all`) and the concrete repos
  (orders/signals/trades) returns `T | None` / `list[T]`, and
  `find_by_id` itself has zero callers anywhere in `src/`. No call site
  currently treats "not found" as an error condition.
- `ShutdownError`: `bootstrap/shutdown.py` wraps every one of its 9
  teardown steps in its own `try/except Exception: logger.error(...)`,
  by explicit design ("Must never block teardown — this is best-effort
  notice only"). Raising here would abort remaining teardown steps,
  defeating the function's entire purpose.
- `LookAheadBiasError`: no look-ahead-bias detection logic exists
  anywhere in `backtesting/`. Wiring this isn't a connection — it's
  building a new detector from scratch, out of scope for a wiring pass.

### Investigated, no safe wiring site — enums
- `RiskLevel`: zero callers. A drawdown-bucketed classification
  (`risk_manager.stats()`'s `drawdown_pct` vs `max_drawdown_pct`, or the
  dashboard health payload) is a plausible additive site, but the
  LOW/MEDIUM/HIGH/CRITICAL breakpoints are arbitrary and unspecified —
  needs your call on the thresholds, not a default I should invent.
- `TradingMode` (LIVE/PAPER/BACKTEST): zero callers. Overlaps but doesn't
  match `TradingEnvironment` (PAPER/TESTNET/PRODUCTION), which already
  fully drives the paper-vs-live path via `Settings.is_paper` throughout
  `bootstrap/`. Backtesting runs through a separate `BacktestConfig`/
  `engine.py` path that doesn't consult either enum. Wiring `TradingMode`
  in as a unified mode concept across live/paper/backtest is a real
  refactor, not a connection.

### Not dead — corrected from an earlier note
`TimestampedValue` (`core/models.py`) is not dead: `market_data/cache.py`
imports and uses it for `_mark_prices` (replacing a raw `(Decimal,
datetime)` tuple). Superseded item; no action needed.

### Verification
Full-tree `py_compile` clean.

## [1.3.25] - Port two features stranded in abandoned forks: `orderbook_spread_pct` gauge, per-logger yaml overrides

### Context
Reconciled a pile of same-version-numbered zip uploads (duplicates at
`[1.3.16]`, `[1.3.18]`, and `[1.3.21]`–`[1.3.23]`) and found genuine
history collisions, not just re-uploads: at each of those three points,
two different pieces of work were independently committed under the
*same* version label, and only one branch was ever continued forward.
Confirmed which branch is trunk by chain-matching `CHANGELOG.md` entry
titles all the way up to the (unambiguous, singly-uploaded) `[1.3.24]`
head.

Two abandoned-fork features never made it into trunk as a result:

- **`[1.3.18]` fork A** — "Wired `OrderBookUpdatedEvent` to a spread
  Prometheus metric" (19th metric, closing the last publisher-without-
  subscriber gap from the standing event-bus audit). Trunk's actual
  `[1.3.18]` was fork B, "Wire the six orphaned functions flagged in
  `[1.3.17]`" — unrelated work, same version number. `OrderBookUpdatedEvent`
  has had a publisher (`MarketDataFeed`'s book-ticker handler) since
  `[1.3.17]`'s audit, but no subscriber, all the way through `[1.3.24]`.
- **`[1.3.16]` fork** — a `configs/logging.yaml` `loggers:` section for
  per-logger level overrides, implemented via ad-hoc `yaml.safe_load()` +
  `logging.config.dictConfig()` directly inside `telemetry.py`, bypassing
  the `Settings`/pydantic-model pattern every other yaml config in this
  project uses (`RiskConfig`, `MonitoringConfig`, `LoggingConfig` itself,
  etc.). Trunk's `[1.3.16]` (the version that was actually continued)
  correctly used the `Settings`-based pattern but only for rotation
  params, not per-logger levels. Ported the *feature*, not the
  implementation — folded per-logger overrides into the existing
  `LoggingConfig`/`_load_logging_yaml()` machinery instead of introducing
  a second, inconsistent yaml-loading path.

### Wired
- `src/monitoring/metrics.py`: added `orderbook_spread_pct` (`Gauge`,
  labeled by `symbol`) under a new "Market data metrics" section — the
  19th Prometheus metric (18 → 19).
- `src/monitoring/metrics_collector.py`: subscribes to
  `OrderBookUpdatedEvent` in `start()`; new `_on_order_book_updated()`
  computes `(best_ask - best_bid) / best_bid * 100` and sets the gauge —
  percentage rather than the event's raw `spread` field, to match
  `strategy/filters.py::filter_by_spread`'s existing convention and stay
  comparable across symbols at different price scales. Guards
  `best_bid == 0` (shouldn't happen — `feeds.py` skips publishing when
  either book-ticker side is absent — but avoided rather than trusted).
- `src/bootstrap/app.py`: updated the two `MetricsCollector`-related
  comments from 18/15 to 19/16 Prometheus metrics.
- `src/core/config.py`: added `LoggingConfig.logger_levels: dict[str, str]`
  (default: the four previously-hardcoded noisy loggers at `WARNING`).
  `_load_logging_yaml()` now also reads a `loggers:` section and *merges*
  it over that default (not replaces), so specifying one logger doesn't
  un-suppress the other three.
- `src/monitoring/telemetry.py`: `configure_logging()` gained a
  `logger_levels: dict[str, str] | None = None` kwarg; `None` (the
  default) preserves the prior hardcoded four-logger behavior so no
  existing caller changes semantics. `bootstrap/app.py:create_application()`
  now passes `settings.logging.logger_levels` explicitly.
- `configs/logging.yaml`: documented the new `loggers:` section (default
  empty — relies on the hardcoded default merge) with commented examples.

### Tests
- `tests/monitoring/test_metrics_collector.py`: added `TestOrderBookUpdated`
  (gauge set correctly, non-event input ignored, zero-bid guarded,
  latest-value-wins, symbols tracked independently) — ported from the
  abandoned fork, unique per-test symbol labels per the file's existing
  cross-test-contamination convention.
- `tests/core/test_config.py`: added 4 tests to `TestLoggingYamlLoading`
  covering the default `logger_levels`, merge-not-replace semantics,
  adding a new logger, and rejecting an invalid level name.
- `tests/monitoring/test_telemetry.py`: added 3 tests to
  `TestConfigureLogging` covering the `None` fallback, applying explicit
  overrides, and that an explicit (possibly partial) dict is used as-is
  rather than merged — the merge is `Settings`'s job, not
  `configure_logging()`'s.

### Verification
Full-tree `py_compile` clean. `pydantic`/`prometheus_client`/`pytest`
unavailable in this sandbox — Allan: please run
`pytest tests/monitoring/test_metrics_collector.py
tests/monitoring/test_metrics.py tests/core/test_config.py
tests/monitoring/test_telemetry.py tests/bootstrap/test_app.py` locally,
plus the full suite per standing practice.

All 22 event types once again confirmed to have both a publisher and a
subscriber (this had silently regressed for `OrderBookUpdatedEvent`
since `[1.3.19]`, when the fork-A `[1.3.18]` work was superseded without
being merged forward).

## [1.3.24] - Close out the original dead-code sweep: enums, `TimestampedValue`, and the remaining top-level functions

### Context
Final pass on the dead-code list from the audit that started this whole
run: `RiskLevel`/`TradingMode` enums, `TimestampedValue`, and 12
top-level functions flagged as having zero callers. Same discipline
throughout — investigate each against real usage before touching
anything, wire the genuine matches, flag the rest with a specific
reason rather than forcing something that doesn't actually fit.

**Important correction first: 3 of the 12 "dead" functions were a
false positive in the original AST scan.** The scan only checked for
usage *outside* the defining file, so it missed same-file/same-module
callers:
- `rsi_from_candles` — already called by `current_rsi` (its neighbor in
  `indicators/rsi.py`), which is itself used from `smart_money/
  confluence.py`.
- `true_range` — already called in a loop inside `calculate_atr`
  (`indicators/atr.py`), explicitly by design ("delegates to
  true_range() so there is a single implementation of the TR formula").
- `run_migrations_offline`/`do_run_migrations`/`run_migrations_online`
  (`persistence/alembic/env.py`) — standard Alembic template pattern:
  called at module-execution time by Alembic's own CLI loading the file,
  not via a normal Python import, so cross-file grep can never find
  them. Not a gap at all.
- `timestamp_ms` (`exchange/adapters/bybit/auth.py`) — already called
  by `sign_request` in the same module.

None of these four needed any change. Worth noting since the original
audit's "12 dead functions" count included some of these.

### Wired
- **`TimestampedValue`** → `market_data/cache.py`'s `_mark_prices`
  internal dict, replacing a raw `(Decimal, datetime)` tuple — exactly
  the shape the model already described. Internal representation only;
  `put_mark_price`/`get_mark_price`'s public signatures are unchanged.
- **`volume_sma`** → `indicators/volume.py`'s `relative_volume`, which
  had a verbatim inline copy of `volume_sma`'s body
  (`vols = [...]; calculate_sma(vols, period)`) instead of calling it.
  Same computation, now shared instead of duplicated.
- **`validate_non_negative`** → `domain/balance.py`'s `Balance.credit()`,
  replacing a hand-rolled `if amount < 0: raise ValueError(...)`.
  Verified the new message is character-for-character identical to the
  original ("Credit amount must be non-negative, got {amount}"), so the
  one existing `credit()` test (positive-path only) needed no changes.
- **`validate_leverage`** → also finished this pass (started last
  session as part of the interfaces work, landed here): wired into
  `config.py`'s `_load_symbols_yaml`, replacing a hand-rolled leverage-
  ceiling check. Gave it a backward-compatible optional `detail` kwarg
  (zero existing callers, so no compat risk) so the original operator-
  facing message survives verbatim instead of being replaced by the
  function's generic default. `tests/core/test_config.py`'s assertion
  updated from `ValueError` to `ValidationError` (not a subclass
  relationship) — verified the regex match and message directly
  outside pytest.
- **`override_settings`/`reset_settings`** → both are explicitly
  documented "for use in tests only," but had zero test coverage of
  their own — every existing test builds a `Settings()` instance
  directly via the `make_settings` fixture, bypassing the
  `get_settings()` singleton entirely, so the singleton path itself was
  completely unexercised (and `get_settings()` had no test either).
  Added `TestSettingsSingleton` to `tests/core/test_config.py` (4 cases:
  repeated-call identity, reset-forces-reinit, override-replaces,
  override-persists) using both functions exactly as their docstrings
  intend, with a `teardown_method` resetting the singleton so this
  class can't leak state into whichever test runs after it.

### Investigated, deliberately not wired
- **`RiskLevel`, `TradingMode`** (flagged in `[1.3.22]`'s enum
  investigation, revisited here) — `TradingMode` overlaps with the
  already-wired `TradingEnvironment` (a different axis: live/paper/
  testnet vs. live/paper/backtest), and backtesting has no runtime
  "mode" flag at all — it's a fully separate `BacktestEngine` code path.
  `RiskLevel` has no existing numeric-risk-bucketing logic anywhere to
  attach to, and `AlertSeverity` already covers "classify for
  monitoring/alerting" (same stated purpose as `RiskLevel`'s docstring).
  Both read as an earlier design iteration the codebase moved past
  before either was ever wired in.
- **`is_higher_timeframe_bullish`/`is_higher_timeframe_bearish`**
  (`smart_money/market_structure.py`) — superseded by the more general
  `MTFContext.is_htf_aligned()` already wired into `signal_engine.py`.
- **`sma_from_candles`/`current_sma`** — genuinely has no caller (unlike
  the RSI/ATR/timestamp_ms false positives above). Could plausibly join
  `confluence.py`'s existing "computed but never called from the live
  strategy path → ML feature input" pattern (RSI, MACD, stochastic,
  Williams %R, OBV, VWAP, Bollinger, historical volatility all went in
  that way already), but that's a feature-design decision — which SMA-
  derived value, if any (price/SMA ratio? SMA slope?) — not a
  same-effect mechanical wire like the others in that file.
- **`validate_timestamp_not_future`** — no existing "reject a
  timestamp that's implausibly in the future" check anywhere to
  replace; every exchange-parser timestamp is locally generated at
  parse time (`datetime.now(tz=UTC)`), not validated against an
  externally supplied one. Wiring this would mean adding a new guard,
  not replacing an existing one.
- **`validate_percentage`** — the one existing 0–100 range check
  (`core/models.py`'s `Percentage.coerce_decimal`) is a pydantic
  `@field_validator`, which must keep raising `ValueError` for
  pydantic's error collection to work — swapping in our custom
  `ValidationError` there would break that, not just change the type.
- **`validate_risk_reward`** — two separate, already-working
  enforcement paths exist for minimum R:R (`RiskManager`'s reject-and-
  log check, `strategy/filters.py`'s boolean gate); this function is a
  third, raise-based mechanism duplicating both. Picking one to replace
  is a design call, not a mechanical wire.
- **`new_uuid`** — returns a hyphenated UUID string
  (`str(uuid.uuid4())`); every domain model's actual ID generation uses
  `uuid.uuid4().hex` (no hyphens) instead, including one call site
  (`Order.client_order_id`) that explicitly truncates the hex form to
  16 characters — a format the hyphenated version can't substitute for.
  Real, deliberate format mismatch, not a caller gap.

### Verification
Full-tree `py_compile` + `ast.parse` clean (294 files — edits and one
new test class only, no new files). `validate_non_negative`'s message
text and `validate_leverage`'s `detail`-override behavior both directly
exercised outside pytest (still no `pydantic` in-sandbox) and confirmed
to match the exact strings the affected tests check for.
`TestSettingsSingleton`'s logic (global-variable get/set/reset) verified
by reading `get_settings`/`override_settings`/`reset_settings`'s
implementation rather than live execution, since importing
`src.core.config` needs `pydantic`. Allan: please run
`pytest tests/core/test_config.py tests/unit/test_domain.py tests/unit/
test_indicators.py` locally to confirm before merging — the singleton
tests in particular are new ground (nothing in this suite previously
touched `get_settings()`) and are the one part of this pass I'd most
want confirmed against a real interpreter.

This closes out the original dead-code audit's item list. Every
Protocol/ABC, exception subclass, enum, model, and function that sweep
flagged has now been either wired in, corrected as a false positive, or
flagged with a specific documented reason for staying unwired — nothing
left unaccounted for from that list.

## [1.3.23] - Wire 4 of the 11 unused exception subclasses flagged in the original dead-code sweep

### Context
Continuing the exception-hierarchy half of the audit flagged alongside
`core/interfaces.py` (`[1.3.22]`): 11 leaf exception subclasses in
`src/core/exceptions.py` were never raised, caught, or imported
anywhere. Same discipline as the interfaces pass — each was checked
against real call sites for a genuine semantic match before touching
anything, rather than mechanically importing them somewhere to make
grep happy.

**Result: 4 of 11 had a clean, real wiring site this pass; 7 remain
open for a future pass or don't have an obvious safe home yet.**

### Wired
- **`MissingConfigurationError`** → `exchange_factory.py`'s 4
  "credentials not configured" raises (2 for Binance, 2 for Bybit).
  Gave the constructor an optional keyword-only `detail: str | None`
  override so the existing, more actionable messages ("check
  BINANCE_API_KEY / BINANCE_API_SECRET") are preserved exactly rather
  than replaced with the generic default — backward compatible with
  any caller that only ever passes `key`.
- **`InvalidConfigurationError`** → `exchange_factory.py`'s 2 "unknown
  exchange_provider" raises. Reason text keeps the original "Unknown
  exchange_provider '...'" phrasing so it just gets wrapped by the
  class's standard prefix rather than replaced.
- **`InvalidOrderError`** → `src/exchange/binance/orders.py`'s two
  order-parameter-validation raises. Confirmed `BinanceOrderManager`
  has a real caller (`scripts/manual_order.py`, the manual-order CLI —
  see that script's own docstring for the disposition decided when it
  was reinstated) before wiring, since a prior sweep once removed a
  dead `BinanceOrderManager` and it would have been easy to mistake
  this one for the same dead code.
- **`DataQualityError`** → `src/core/validation.py`'s
  `validate_ohlcv_consistency`, replacing a generic `ValidationError`.
  Semantically the better fit — this function is specifically checking
  market-data quality (OHLCV consistency), not general input
  validation — and `DataQualityError` was written for exactly this and
  never used.

All four are subclasses of the exception they replaced only for the
config pair (`ConfigurationError`) — `InvalidOrderError` and
`DataQualityError` are *not* subclasses of the plain `ValueError`/
`ValidationError` they replaced, since neither of those relationships
holds in the hierarchy as designed. Where that changes what a
`pytest.raises(...)` assertion needs to match, the test was updated in
the same commit:
- `tests/exchange/test_binance_orders.py` — both order-param tests now
  assert `InvalidOrderError` instead of `ValueError`.
- `tests/unit/test_core.py` — the OHLCV-inconsistency test now asserts
  `DataQualityError` instead of `ValidationError`.
- `tests/bootstrap/test_exchange_factory.py` — untouched and still
  passes as-is: `MissingConfigurationError`/`InvalidConfigurationError`
  are genuine subclasses of `ConfigurationError`, so the existing
  `pytest.raises(ConfigurationError, match=...)` assertions still hold,
  and every preserved message substring the tests `match=` against
  ("binance", "bybit", "Unknown exchange_provider") was checked
  word-for-word before this landed.

### Investigated, no wiring site found this pass — 7 remaining
- **`StaleDataError`** — `market_data/cache.py`'s staleness checks
  (`get_ticker`/`get_mark_price`/`get_best_bid_ask`) deliberately
  degrade to returning `None` on a stale entry rather than raising;
  callers already branch on `None`. Switching that to an exception
  would be a real behavior change touching every caller, not a
  same-effect type swap — needs a decision, not a mechanical wire.
- **`SignalGenerationError`, `NoSignalError`** — no call site found yet
  where strategy code distinguishes "no signal produced" from "signal
  generation failed" via anything other than returning `None`/an empty
  list. Same shape of problem as `StaleDataError`.
- **`RecordNotFoundError`** — `persistence/repository.py`'s
  `find_by_id` already returns `None` on a miss (by design, checked
  during the `[1.3.22]` `IRepository` investigation); no current
  "must-exist" lookup path that would raise instead.
- **`LookAheadBiasError`** — `backtesting/` doesn't currently have a
  bias-detection check to attach this to; would mean adding new
  detection logic, not wiring existing logic to a new exception type.
- **`ShutdownError`, `InitializationError`** — `bootstrap/app.py`'s
  startup/shutdown sequences currently propagate whatever the
  underlying failure raised rather than wrapping it in a
  system-lifecycle-specific type. Wrapping would be a legitimate
  improvement but changes what every caller of `Application.start()`/
  `.stop()` sees on failure — a design call, not a safe default.

Not wiring `TradingSystemError`, `ExchangeError`, `MarketDataError`,
`RiskError`, `ExecutionError`, `PersistenceError`, `BacktestError`,
`SystemError` — these are the 8 base classes in the hierarchy, meant to
be caught broadly by subclass rather than raised directly; grep finding
"no direct usage" for these is expected, not a gap.

### Verification
Full-tree `py_compile` + `ast.parse` clean (294 files, edits only, no
new files). Directly exercised `MissingConfigurationError`,
`InvalidConfigurationError`, and `DataQualityError`'s construction,
message text, and `isinstance` relationships against their parent
classes outside pytest (still no `pydantic` in-sandbox) to confirm the
`match=` regexes in existing tests keep passing and the subclass
relationships are what this entry claims. `InvalidOrderError`'s
call path goes through `Instrument.validate_order_params` (a pydantic
model method) so couldn't be exercised directly in-sandbox — Allan,
please run `pytest tests/exchange/test_binance_orders.py tests/
bootstrap/test_exchange_factory.py tests/unit/test_core.py` locally to
confirm before merging.

## [1.3.22] - Wire `IRiskManager`/`IOrderManager`/`ILifecycle`; audit the rest of `core/interfaces.py`

### Context
Continuing the standing "built but unwired" sweep, this pass picked up
where the prior dead-code audit left off: `src/core/interfaces.py`'s 9
unused Protocol/ABC interfaces. Each was checked against the real
concrete classes it's supposed to describe, structurally, before
touching anything — the same "investigate before wiring" discipline as
`[1.3.17]`/`[1.3.18]`'s deliberately-unwired items, applied here to
interfaces instead of functions.

**Result: only 3 of the 9 were a clean or fixable match.** The other 6
describe a shape of the system that was never built, or that the system
evolved away from — forcing them on would misrepresent the actual
contract, not document it. Wiring an interface onto a class that
doesn't really satisfy it is worse than leaving it unwired.

### Wired
- **`IRiskManager`** → `ExecutionEngine.__init__`'s `risk_manager` param
  (was the concrete `RiskManager` type). Clean structural match — every
  method `ExecutionEngine` actually calls
  (`validate_signal`/`calculate_position_size`) is on the Protocol;
  `check_portfolio_risk`/`on_position_update` are already exercised
  elsewhere (`bootstrap/startup.py`'s scheduled task; the latter is a
  documented no-op stub — "Handled by event bus" — kept for Protocol
  completeness).
- **`IOrderManager`** → `ExecutionEngine.__init__` and
  `ReconciliationService.__init__`'s `order_manager` params. Needed a
  fix first: the Protocol was missing `get_open_orders`/`persist_order`,
  both of which real callers (`ExecutionEngine`, `ReconciliationService`)
  already depend on — the interface had drifted from actual usage since
  it was written. Added both to `IOrderManager` (matching
  `OrderManager`'s real signatures exactly) before using it as the type
  hint, same "fix the contract, then wire it" order as
  `[1.3.20]`/`[1.3.19]`'s wiring-fixes-a-real-gap pattern.
- **`ILifecycle`** → `BaseStrategy` gains it as a second base
  (`class BaseStrategy(IStrategy, ILifecycle)`). `BaseStrategy` already
  had a concrete `start()`/`stop()`/`is_running` that exactly match
  `ILifecycle`'s abstract surface (name, signature, async-ness) — this
  was structural conformance with no declared contract, now declared.
  No other concrete class in the tree has all three; not force-fit
  elsewhere.

All three are constructor-parameter/base-class type changes only — zero
runtime behavior change (Python doesn't enforce Protocol/ABC contracts
beyond what's already true structurally), same category as the existing
`IExchangeClient` wiring these two mirror.

### Investigated, deliberately not wired — 6 remaining
Each Protocol/ABC below describes a *different* shape than what the
real component actually does. This isn't a missing-caller gap like a
plain orphaned function; it's the interface and the implementation
having diverged, and picking which one is "right" is a design call, not
a mechanical wiring pass:

- **`IMarketDataFeed`** — describes four separate
  `subscribe_candles`/`subscribe_ticks`/`subscribe_order_book`/
  `unsubscribe_all` methods. The real `MarketDataFeed` has one unified
  `subscribe(...)` covering all three plus mark-price/book-ticker
  streams the Protocol doesn't even mention.
- **`ICandleProvider`** — declares `get_candles` as `async`. The real
  `CandleService.get_candles` is synchronous (it reads from an
  in-memory buffer already populated by `subscribe_live`/`initialize`);
  making it match would mean changing every call site, not the
  interface.
- **`INotifier`** — declares `send(message, severity: str) -> bool`.
  The real `Notifier.send` takes `severity: AlertSeverity` (an enum, not
  a string) and returns `None`, and the trade-notification method is
  named `send_trade`, not `send_trade_notification`.
- **`IRepository`** — declares `delete(entity_id: str) -> bool`. The
  real `BaseRepository.delete` takes the *entity itself* and returns
  `None`; the entity-id-keyed, bool-returning method that matches the
  Protocol's shape exists but is named `delete_by_id`. Also `save`
  returns `T` (the persisted entity, useful for reading back a
  generated PK) where the Protocol says `None`.
- **`IHealthCheck`** — describes individual components each exposing
  `component_name`/`health_check()`. The real health-check system
  (wired in a prior session — see `bootstrap/startup.py` step 8c) uses
  a registry (`HealthChecker.register(name, check_fn)` +
  `.run_all()`) with plain name/callable pairs, not `IHealthCheck`-
  shaped objects. Different, already-working architecture for the same
  problem; not a gap.
- **`IIndicator`** — an ABC expecting per-indicator classes with
  `calculate()`/`update()`. The entire `indicators/` package is
  function-based (`macd()`, `volume_sma()`, `bollinger_bands()`, ...),
  not class-based — there are no indicator *classes* to wire this onto
  without inventing a class-wrapper layer the rest of the codebase
  doesn't use.

Flagging all 6 for Allan's call: either retire the interface (it
describes an abandoned design) or treat it as a spec for a refactor
(e.g. give `BaseRepository` a Protocol-matching `delete`, give
`Notifier` a Protocol-matching `send`) — both are real scope decisions,
not safe to make unilaterally.

### Verification
Full-tree `py_compile` + `ast.parse` clean (294 files, no new files —
edits only). Confirmed via grep that `IRiskManager`/`IOrderManager` are
now referenced outside `core/interfaces.py` and their own tests, and
that the 6 flagged interfaces remain referenced only from
`core/interfaces.py` (no accidental partial wiring). Multiple-
inheritance abstract-method resolution for `BaseStrategy(IStrategy,
ILifecycle)` verified by signature/name inspection rather than live
instantiation — `pytest`/class-construction checks can't run in-sandbox
(`pydantic` not installed, same standing constraint). Allan: please run
the existing `tests/execution/test_execution_engine.py`,
`tests/execution/test_reconciliation.py`, and `tests/strategy/
test_base_strategy.py` (or equivalents) locally to confirm the
Protocol/ABC changes don't break construction — no test changes were
needed since this pass changes only type hints and a base-class list,
not behavior, so existing tests should cover it as-is.

## [1.3.21] - Close the test-coverage gap flagged in [1.3.20]: `_load_partial_tp_levels`

### Context
`[1.3.20]` re-flagged a pre-existing gap while adding tests for its
sibling method: `_load_partial_tp_levels` (the direct-yaml-read pattern
`_load_track_funding_pnl` was later modeled on) predates that sibling
and has never had a dedicated test file. No code change — this is a
test-only pass closing that gap, same shape as `test_load_track_funding_
pnl.py`.

### Added: `tests/bootstrap/test_load_partial_tp_levels.py`
`TestLoadPartialTpLevels`, 10 cases — covers every branch in
`_load_partial_tp_levels` (a slightly larger surface than
`_load_track_funding_pnl`'s plain boolean: this one also does
per-level parsing/validation and returns a decimal tuple list, not a
scalar):
- missing file, missing `partial_take_profit` section, section present
  but `enabled` key absent, explicit `enabled: false` — all `None`
- `enabled: true` with valid levels — returns the expected
  `list[tuple[Decimal, Decimal]]`
- `enabled: true` with the `levels` key absent entirely — `None`
- `enabled: true` with an empty `levels` list — `None`
- a malformed level entry mixed with valid ones — malformed one
  skipped, valid ones kept (exercises the per-entry `try/except`)
- every level entry malformed — `None` (exercises the "no valid
  levels survived parsing" branch, distinct from the empty-list case)
- malformed top-level YAML — `None`

### Verification
Full-tree `py_compile` + `ast.parse` clean (294 files across `src/` +
`tests/`, up by one — the new test file only). `grep` confirms
`_load_partial_tp_levels` now has a matching `tests/bootstrap/test_*`
file, same as every other `_load_*` method on `Application`. `pytest`
not run in-sandbox (no network access to install deps, same standing
constraint) — Allan: please run
`pytest tests/bootstrap/test_load_partial_tp_levels.py` locally before
merging.

## [1.3.20] - Wire FundingRateEvent → Position.funding_pnl (session-standard event-bus audit)

### Context
Standing session protocol: unzip → `py_compile`/`ast.parse` sweep →
event-bus producer/subscriber check → Prometheus metrics coverage →
config-yaml reader verification. The event-bus check found one genuine
gap: `FundingRateEvent` has been published by `market_data/feeds.py`
since it was added, but had zero subscribers anywhere. Following that
thread further, `Position.add_funding()`/the `funding_pnl` field
(already wired into `total_pnl` and `Trade.net_pnl`'s formulas) had no
caller anywhere in the codebase — `funding_pnl` has been silently stuck
at `Decimal("0")` for every position since the field was added, which
means every closed `Trade`'s `net_pnl` has been missing a real,
continuous PnL component for perpetual futures. This is the "built but
never wired" pattern from prior sessions, not a new-feature request, so
it was wired unilaterally as a safe/pattern-matching change (it changes
*reported PnL accuracy*, not any live signal-selection, stop-placement,
or position-sizing decision — those three remain the sign-off gate).

(`OrderBookUpdatedEvent` also shows no direct subscriber in the same
check, but that one's not a gap: `MarketDataCache.put_best_bid_ask()`
already feeds `filter_by_spread()` via a cache read rather than a
direct event subscription, so the data does reach its consumer.)

### Added: `execution/position_manager.py::PositionManager.apply_funding`
New method, same pattern as the existing `update_mark_price` (iterate
open positions on a symbol, mutate each). Computes the funding payment
from current notional (`quantity × mark_price` — `mark_price` rather
than a price on the event itself, since `FundingRateEvent` doesn't
carry one and mark_price is already kept live via
`update_mark_price()`/`MarkPriceUpdatedEvent`) using the standard
perpetual-futures convention: a positive rate means longs pay shorts, a
negative rate means shorts pay longs. Feeds the payment into the
existing `Position.add_funding()`.

### Wired: `ExecutionEngine._on_funding_rate`
Subscribes to `FundingRateEvent` and calls `apply_funding()` for every
open position on the event's symbol. Gated by new `track_funding_pnl`
constructor param, **default `False`** per the standing "new behavior
is opt-in" policy — even though this only changes PnL *reporting*
accuracy and touches no trading decision, it changes numbers Allan
might already be reading (dashboards, `total_realized_pnl` in
`PositionManager.stats`), so it stays off until explicitly enabled.
Threaded `configs/strategy.yaml`'s new `execution.track_funding_pnl` key
→ `Application._load_track_funding_pnl()` (same direct-yaml-read pattern
as `_load_partial_tp_levels`, since this section isn't merged into
`Settings`) → `ExecutionEngine(track_funding_pnl=...)`.

### Not touched
No new Prometheus metric added — `position_pnl`/`account_daily_pnl`
already compute against `Trade.net_pnl`/`Position.total_pnl`, both of
which already include `funding_pnl` in their formulas, so once the
field is actually populated the existing metrics become accurate
without any metrics-layer change.

### Tests
- `tests/execution/test_position_manager.py` (new file — this module had
  no dedicated test file before): `TestApplyFunding`, 9 cases covering
  long/short payment direction, both rate signs, mark-price-not-entry-
  price basis, quantity scaling, accumulation across settlements,
  symbol filtering, no-open-position no-op, closed-position skip, and
  multiple positions on the same symbol settled independently.
- `tests/execution/test_execution_engine.py`: new `TestFundingRateUpdates`
  (7 cases): not subscribed by default, subscribed when
  `track_funding_pnl=True`, non-`FundingRateEvent` ignored, no-op when
  not running, no-op when no open position, and the normal-path call
  into `PositionManager.apply_funding`.
- `tests/bootstrap/test_load_track_funding_pnl.py` (new file):
  `TestLoadTrackFundingPnl`, 6 cases (missing file, missing section,
  explicit false, explicit true, non-bool value, malformed yaml) —
  same coverage shape as should exist for `_load_partial_tp_levels`,
  which predates this session and still has none; flagging that gap for
  a future session rather than scope-creeping it into this one.

### Verification
Full-tree `py_compile` + `ast.parse` clean (303 files, up from 301 —
the two new test files). Event-bus producer/subscriber sweep re-run
post-change: `FundingRateEvent` now shows a subscriber; no other event
type regressed. All six `configs/*.yaml` files parse cleanly, including
the new `execution.track_funding_pnl` key. `pytest` not run in-sandbox
(no network access to install deps, same standing constraint) — Allan:
please run
`pytest tests/execution/test_position_manager.py tests/execution/test_execution_engine.py tests/bootstrap/test_load_track_funding_pnl.py`
locally before merging.

## [1.3.19] - Implement the two items [1.3.18] deferred: live Kelly sizing + ML feature schema expansion

### Context
`[1.3.18]` left two things explicitly unfinished pending their own
prerequisites/sign-off: `kelly_position_size` needed a live rolling
win-rate tracker that didn't exist, and adding the new volume/volatility
metadata to the ML feature schema would invalidate any already-trained
model. Allan signed off on building both.

### Added: `risk/trade_tracker.py::RollingTradeTracker`
New module — a fixed-size rolling window of realized net P&L per closed
trade, computing `win_rate`/`avg_win`/`avg_loss` fresh on every read so
older trades age out rather than permanently anchoring the estimate.
This is the live, incrementally-updated equivalent of what
`analytics/expectancy.py`/`performance.py` already compute as a batch
report over a closed `list[Trade]` — RiskManager needed something it
could query synchronously per-signal, which those two don't offer.

### Wired: `risk/position_sizing.py::kelly_position_size`
`RiskManager` now constructs a `RollingTradeTracker` unconditionally
(cheap, in-memory) and feeds it from the same `PositionClosedEvent`
subscription that already drives `record_pnl()` for the daily-loss
breaker. `calculate_position_size()` gains a third
`position_sizing_method` option, `"kelly"`:
- Falls back to `fixed_fractional` (logged warning) until
  `kelly_min_trades` closed trades have accumulated in the current
  process — a fresh process has an empty tracker, so this isn't
  optional.
- Falls back to `fixed_fractional` (logged info, not a warning — this is
  a legitimate "no edge" result from the formula itself, not a data
  problem) whenever the estimated Kelly fraction is non-positive.
- Otherwise sizes via fractional Kelly (`kelly_fraction`, default 0.25 —
  full Kelly is optimal in theory but too volatile in practice), capped
  by `kelly_max_risk_pct` independent of `max_portfolio_risk_pct` (Kelly's
  own formula can imply a much larger fraction than a flat risk_pct
  would ever produce).
- Unlike `fixed_fractional`/`volatility_adjusted`, Kelly's quantity isn't
  derived from this signal's stop distance at all — every downstream cap
  (`max_position_pct_of_equity`, correlation adjustment, liquidation
  check) still applies on top exactly as it does for the other two
  methods.

New `RiskConfig` fields: `kelly_lookback_trades` (default 30),
`kelly_min_trades` (default 20, validated `<= kelly_lookback_trades` at
construction), `kelly_fraction` (default 0.25), `kelly_max_risk_pct`
(default 5.0). `position_sizing_method`'s pattern now allows
`"fixed_fractional" | "volatility_adjusted" | "kelly"`.
`configs/risk.yaml` documents all four new keys.

### Wired: ML feature schema expansion
`machine_learning/features.py::FEATURE_NAMES` gains five entries
(`vwap_distance_pct`, `obv_slope_normalised`, `bb_percent_b`,
`bb_bandwidth`, `historical_volatility`), consuming the metadata
`[1.3.18]` added to `signal.metadata` but deliberately didn't wire into
the feature vector yet. `extract_features()` reads all five with the
same None-degrades-to-neutral convention as every existing metadata-
derived feature. Two of the five needed extra care to be genuinely
dimensionless/bounded rather than raw pass-throughs:
- `obv_slope`: confluence.py's computation itself changed — the raw
  regression slope is now divided by the average bar volume over the
  same window before being stored in metadata, converting an
  asset-scale-dependent number into "OBV momentum in units of average
  volume per bar" (comparable across symbols the same way
  `relative_volume` already is). `extract_features()` additionally
  squashes it through `tanh` since it's still theoretically unbounded.
- `vwap`: turned into `vwap_distance_pct` — `(price - vwap) / vwap * 100`
  — rather than passed through as a raw price level, consistent with
  every other price-scale-dependent feature in this module already being
  expressed as a ratio/distance.

**Consequence, confirmed intentional**: `N_FEATURES` changed
(34 → 39), which means any model previously saved via
`MLTrainer.save()` will now fail to load —
`MLTrainer.load()`'s existing `saved_features != FEATURE_NAMES` guard
catches this loudly with "Retrain the model." rather than silently
using a mismatched vector. Allan: any deployed model needs retraining
before the next restart that would try to load it.

### Tests
- `tests/risk/test_trade_tracker.py` (new file): `TestConstruction`,
  `TestRecordAndStats`, `TestRollingWindow`, `TestMinSample` — 9 cases
  covering the tracker in isolation.
- `tests/risk/test_risk_manager.py`: new `TestRiskManagerKellySizing` (4
  cases: below-min-sample fallback, positive-edge sizing diverges from
  fixed-fractional, non-positive-edge fallback, `kelly_min_trades >
  kelly_lookback_trades` rejected at `RiskConfig` construction).
- `tests/unit/test_ml_features.py`: new
  `TestVolumeVolatilityMetadataDerivedFeatures` (5 cases: absent-metadata
  neutral defaults, VWAP distance math, OBV slope bounded, Bollinger
  pass-through, historical volatility pass-through). Existing
  `test_trainer.py`/`test_signal_filter.py` coverage was already written
  against `N_FEATURES`/`FEATURE_NAMES` dynamically rather than a
  hardcoded count, so it needed no changes to keep covering the
  schema-mismatch-detection behavior this session leans on.

### Verification
Full-tree `py_compile` + `ast.parse` clean (301 files, up from 299 in
`[1.3.18]` — the two new files are `trade_tracker.py` and
`test_trade_tracker.py`). `grep` confirms `kelly_position_size` and
`RollingTradeTracker` are now referenced outside their own module/tests.
`pytest` not run in-sandbox (no network access to install deps, same
standing constraint) — Allan: please run
`pytest tests/risk/test_trade_tracker.py tests/risk/test_risk_manager.py tests/unit/test_ml_features.py`
locally before merging, and retrain/re-save any ML model before it's
next loaded.

## [1.3.18] - Wire the six orphaned functions flagged in [1.3.17]

### Context
Allan signed off on wiring the full flagged list from `[1.3.17]`. Each
was investigated on its own merits rather than force-wired uniformly —
three got wired in as opt-in, off-by-default behavior; three were
investigated and deliberately left unwired, each for a documented
reason consistent with prior sessions' "don't duplicate an existing
live mechanism" and "don't wire something whose supporting
infrastructure doesn't exist yet" precedents.

### Wired

**`smart_money/mitigation.py::extract_mitigation_blocks`** — now runs in
`signal_engine.py`'s `analyze()`, feeding a new `SMCAnalysis.
mitigation_blocks` field. `confluence.py`'s order-block scoring factor
falls back to it (at a flat 60% of what a fresh active OB would score)
when no active OB scores — a mitigated OB's unfilled remainder is a
weaker reference than a fresh one, so it fills a gap rather than
competing with an active OB. Gated by `use_mitigation_blocks` (new,
default `False`), threaded `score_confluence()` → `SMCSignalEngine` →
`SMCStrategy` → `configs/strategy.yaml`'s `smc.use_mitigation_blocks`.

**`risk/stop_loss.py::structure_stop_loss`** — new alternate for the
*no-OB fallback* stop only (the OB-anchored branch is untouched
regardless, per the existing comment in `_calculate_trade_params`
explaining why that branch was left alone in an earlier session).
Anchors just beyond the nearest opposing swing point
(`analysis.structure.last_swing_low`/`last_swing_high`) when one's
available, falling back to the pre-existing `atr_stop_loss()` when
there isn't one or the structure stop would land on the wrong side of
entry. Gated by `stop_loss_method` (new, `"atr"` default / `"structure"`
opt-in), threaded the same path as `use_mitigation_blocks` above via
`configs/strategy.yaml`'s `smc.stop_loss_method`.

**`risk/position_sizing.py::volatility_adjusted_size`** — wired into
`RiskManager.calculate_position_size()` as an alternate base-size model,
scaling the fixed-fractional size down when a signal's stop is wider
than `volatility_target_atr_multiple` × ATR (up, capped at the
fixed-fractional amount, when tighter). Reads ATR from
`signal.metadata["atr"]`; falls back to the fixed-fractional size with a
logged warning if that key is absent. Gated by `position_sizing_method`
(new, `"fixed_fractional"` default / `"volatility_adjusted"` opt-in) on
`RiskConfig`, read from `configs/risk.yaml`.

### Investigated, deliberately not wired

**`risk/position_sizing.py::kelly_position_size`** — needs a live
rolling win-rate/avg-win/avg-loss feed. No such tracker exists in the
live path: `analytics/expectancy.py`/`performance.py` compute those
stats, but only as a batch report over a closed `list[Trade]`, not
something `RiskManager` can query synchronously per-signal. Wiring Kelly
properly means building that tracker first — documented in the new
`position_sizing_method` field's description as a `RiskConfig`
docstring rather than bolted on top of a stat source that isn't there.
Flagging for a future session if Allan wants to prioritize building the
tracker.

**`indicators/volume.py::is_high_volume_bar`** — it's a threshold
wrapper around `relative_volume()`, which already has a live scoring
role (the existing "volume" confluence factor, ~10 lines above where
this would have gone). Wiring it as a second filter/feature would just
double-count that same metric in boolean form — same reasoning that
kept `killzones.py::kill_zone_score` unwired in `[1.3.17]` (it would
have duplicated `sessions.py::session_score`).

**`smart_money/premium_discount.py::is_optimal_entry`** — its exact
logic (discount-for-long / premium-for-short) is already a live,
continuous scoring factor in `confluence.py` (the `premium_discount`
component, ~10pt budget), implemented inline against the same
`PremiumDiscountZones` object rather than by calling this function. That
inline version also handles an equilibrium partial-credit case
`is_optimal_entry` doesn't, so it isn't a strict drop-in replacement
either. Wiring `is_optimal_entry` as a separate opt-in filter would
double-count the same directional bias the scoring factor already
applies continuously.

### Added (metadata, ML-feature-only — see also "not wired" above)

**`indicators/volume.py::obv`, `::vwap`; `indicators/volatility.py::
bollinger_bands`, `::historical_volatility`** — added to
`confluence.py`'s existing informational-metadata block (the same
"computed but never called from the live strategy path until now, no
dedicated filter, ML-feature input only" treatment as the `stochastic_k`/
`williams_r`/`price_zscore`/`trend_slope` block from a prior session).
New `signal.metadata` keys: `vwap`, `obv_slope` (OBV's regression slope,
not the raw running total — an unnormalized cumulative total isn't a
like-for-like feature across bars/symbols the way its slope is),
`bb_percent_b`, `bb_bandwidth`, `historical_volatility`. All `None` on
insufficient candle history, same convention as `rsi_value`/
`macd_histogram`.

Deliberately **not** added to `machine_learning/features.py`'s
`FEATURE_NAMES`/`extract_features()` this session, unlike the earlier
stochastic/Williams %R/z-score/slope block which did get added there.
`trainer.py` has a guard (`saved_features != FEATURE_NAMES`) that
refuses to load a saved model whose feature list doesn't match the
current schema — so expanding `FEATURE_NAMES` now would invalidate any
already-trained model until it's retrained. That's a real, visible
consequence worth flagging and confirming rather than doing silently in
the same pass as everything else above.

### Tests
- `tests/strategy/test_confluence.py`: `make_analysis()` gained
  `mitigation_blocks`/`swing_highs`/`swing_lows` params. New
  `TestMitigationBlockFallback` (5 cases: disabled-default, matching
  fallback score, wrong zone, wrong direction, active-OB priority),
  `TestStopLossMethod` (4 cases: default atr unaffected by available
  swings, structure method anchors to swing, structure falls back to atr
  with no swing, OB-anchored branch identical regardless of method), and
  `TestVolumeVolatilityMetadata` (4 cases: keys present, VWAP sanity
  range, short-history degrades to None not a crash, new metadata
  doesn't move total_score).
- `tests/risk/test_risk_manager.py`: `make_signal()` gained a
  `metadata` param. New `TestRiskManagerVolatilityAdjustedSizing` (3
  cases: default method ignores ATR metadata entirely, wide-stop-vs-ATR
  reduces size vs fixed-fractional, missing ATR falls back to
  fixed-fractional).
- `tests/strategy/test_mitigation.py` (unit tests for
  `extract_mitigation_blocks` itself) was already present and unchanged
  — this session's tests cover the new call site, not the underlying
  function.

### Verification
Full-tree `py_compile` + `ast.parse` clean (299 files — same count as
`[1.3.17]`, no new files added, only edits to existing ones).
Confirmed via grep that all three wired functions are now referenced
outside their own module/tests, and that the three deliberately-unwired
functions remain referenced only from their own module (no accidental
partial wiring). `pytest` not run in-sandbox (no network access to
install deps, same standing constraint) — Allan: please run
`pytest tests/strategy/test_confluence.py tests/risk/test_risk_manager.py`
locally before merging.

## [1.3.17] - Wired `smart_money/killzones.py::should_trade_now` as an opt-in kill-zone signal filter

### Context
Re-ran the standing audits at current HEAD before looking for new work:
event-bus producer/subscriber, Prometheus metrics, and config-yaml
readers all still clean (nothing regressed since `[1.3.16]`). A broader
orphan scan (defs referenced nowhere else in `src/`) turned up several
genuine "built but never wired" cases:
`smart_money/killzones.py::should_trade_now`/`kill_zone_score`,
`smart_money/mitigation.py::extract_mitigation_blocks`/`zone_size`,
`risk/stop_loss.py::structure_stop_loss`,
`risk/position_sizing.py::kelly_position_size`/`volatility_adjusted_size`,
`indicators/volume.py::obv`/`vwap`/`is_high_volume_bar`,
`indicators/volatility.py::bollinger_bands`/`historical_volatility`, and
`smart_money/premium_discount.py::is_optimal_entry`. All are fully
implemented and unit-tested, with zero callers anywhere in the strategy/
risk/execution pipeline.

Of these, only `should_trade_now` fit the established opt-in-gate pattern
(`rsi_exhaustion_filter`/`macd_trend_filter`/`max_spread_pct`: disabled by
default, zero live-behavior change unless explicitly enabled) cleanly
enough to wire without a live-behavior sign-off — it's a pass/reject gate,
not a change to how existing signals are scored, sized, or stopped. The
other six all directly change signal selection, stop placement, or
position sizing if wired into the live path, same category as the
`BinanceOrderManager` decision in `[1.3.14]` — flagged below rather than
acted on.

Note `should_trade_now` doesn't duplicate confluence.py's existing
`"session"` scoring factor: that factor uses
`smart_money/sessions.py::session_score` as a continuous 0-100 weight
nudging the confluence score, whereas this is a hard pass/reject gate
layered on top — different mechanism, not redundant. `kill_zone_score()`
(also in `killzones.py`) *would* duplicate `session_score`'s role if
wired into scoring, so it was left unwired.

### Added
- `src/strategy/filters.py`: `filter_by_kill_zone(require_kill_zone=True)`
  — evaluates `datetime.now(UTC)` against `should_trade_now()`, same
  self-contained shape as the existing `filter_by_weekend()`.
- `src/strategy/orchestrator.py`: `StrategyOrchestrator` gains
  `kill_zone_filter`/`require_kill_zone` constructor params (both default
  to current behavior: filter off, and if later enabled, kill-zone-only
  rather than the relaxed session-score threshold), a
  `_signals_filtered_kill_zone` counter applied in the per-signal filter
  chain (alongside min_atr/rsi/macd/spread), and the counter surfaced via
  `.stats["signals_filtered_kill_zone"]`.
- `configs/strategy.yaml`: `kill_zone_filter: false` and
  `require_kill_zone: true`, documented alongside the other opt-in
  filters.
- `src/main.py`: `_load_orchestrator_filter_config()` reads the two new
  keys (same precedence/fallback pattern as the existing filter keys) and
  passes them through to `StrategyOrchestrator`.

### Flagged for confirmation (not wired this session)
Six more orphaned functions, all behavior-changing if wired into the live
path — need your call on scope/priority before any of these go in:
- `extract_mitigation_blocks`/`zone_size` (`smart_money/mitigation.py`)
- `structure_stop_loss` (`risk/stop_loss.py`) — would change how stops
  are computed
- `kelly_position_size`/`volatility_adjusted_size`
  (`risk/position_sizing.py`) — would change position sizing
- `obv`/`vwap`/`is_high_volume_bar` (`indicators/volume.py`),
  `bollinger_bands`/`historical_volatility` (`indicators/volatility.py`)
  — unclear whether these feed confluence scoring, a new filter, or stay
  as available-but-unused utilities
- `is_optimal_entry` (`smart_money/premium_discount.py`) — would change
  entry-zone qualification, distinct from the existing
  `PremiumDiscountZones`-based scoring already in confluence.py

### Tests
- `tests/strategy/test_filters.py`: new `TestFilterByKillZone` — active
  kill zone, off-hours, and `require_kill_zone=False` session-score mode.
- `tests/strategy/test_orchestrator.py`: new `TestKillZoneFiltering` —
  disabled-by-default no-op, rejection when enabled and outside a kill
  zone, acceptance when enabled and inside one, and the
  `require_kill_zone=False` relaxed-threshold path.
- `tests/strategy/test_killzones.py` (unit tests for `should_trade_now`/
  `kill_zone_score` themselves) was already present and unchanged — this
  session's tests cover the new call site, not the underlying function.

### Verification
Full-tree `py_compile` + `ast.parse` clean (301 files, up from 299 in
`[1.3.16]`). `grep` confirms `filter_by_kill_zone`/`should_trade_now` are
now referenced from `orchestrator.py` in addition to their own
module/tests. `pytest` not run in-sandbox (no network access to install
deps, same standing constraint) — Allan: please run
`pytest tests/strategy/test_filters.py tests/strategy/test_orchestrator.py`
locally before merging.

## [1.3.16] - `configs/logging.yaml`: repurposed as rotation params; wired rotating file handler into structlog

### Context
`configs/logging.yaml` was the last outstanding instance of the
config-file-with-no-reader pattern (`risk.yaml` → `[1.2.x]`,
`monitoring.yaml` → `[1.3.4]`, `strategy.yaml`'s SMC/indicator block →
`[1.3.7]`, `exchanges.yaml` → `[1.3.15]`), but with a twist: it wasn't
just unread, it described a whole `logging.config.dictConfig()` tree
(formatters/handlers/loggers) that duplicated and partially conflicted
with the structlog setup in `monitoring/telemetry.py:configure_logging()`
— the actual live logging path, wired from
`bootstrap/app.py:create_application()`. Running both would have meant
reconciling levels/handlers/noisy-logger suppression across two
independent configuration systems. Decided (with sign-off) to keep
structlog as the single logging path and repurpose the yaml file as
rotation parameters only, rather than resurrecting the dictConfig tree.

Separately, neither path actually wrote logs to disk in production:
structlog used `PrintLoggerFactory(file=sys.stdout)`, stdout only, no
rotation, no durability across process restarts/crashes.

### Changed
- `configs/logging.yaml`: replaced the dictConfig tree with a single
  `rotation:` block (`enabled`, `filename`, `max_bytes`, `backup_count`).
- `src/core/config.py`: added `LoggingConfig` (env-prefix-less, yaml-only
  — same shape as `MonitoringConfig`'s yaml-only fields) and
  `Settings._load_logging_yaml()`, following the established
  env > yaml > default precedence and `model_fields_set` merge pattern.
  Wired into `Settings.load_yaml_overrides()`.
- `src/monitoring/telemetry.py`: `configure_logging()` now builds a
  shared structlog processor chain and renders it through
  `structlog.stdlib.ProcessorFormatter` on stdlib logging handlers
  (`logger_factory` switched from `PrintLoggerFactory` to
  `structlog.stdlib.LoggerFactory()`), so console and file sinks get
  identical record shape (level, logger name, timestamp, correlation
  IDs). New optional kwargs: `file_enabled`, `log_dir`, `file_name`,
  `max_bytes`, `backup_count`. When `file_enabled=True`, attaches a
  `logging.handlers.RotatingFileHandler` to the root logger; the file
  sink always renders JSON regardless of `json_logs`, since rotated logs
  are read by tooling, not a terminal.
- `src/bootstrap/app.py`: `create_application()`'s `configure_logging()`
  call now passes `settings.logging.*` and `settings.paths.log_dir`
  through.
- `tests/monitoring/test_telemetry.py`: rewritten for the new
  ProcessorFormatter-based architecture (old tests asserted the renderer
  was the last entry in `structlog.get_config()["processors"]`, which is
  no longer where rendering happens); added coverage for file-handler
  attachment, JSON-always-on-file, and log-directory auto-creation.
- `tests/core/test_config.py`: added `TestLoggingYamlLoading`, mirroring
  `TestMonitoringYamlLoading`'s precedence/partial-override/missing-file
  cases.

### Verification
Full-tree `py_compile` clean. Could not run `configure_logging()` live or
`pytest` in this sandbox — no network access to install `structlog`.
Allan: please run `pytest tests/monitoring/test_telemetry.py
tests/core/test_config.py tests/bootstrap/test_app.py` locally before
merging; this is a real behavioral change to the logging init path
(logger_factory swap), not just additive config.

## [1.3.15] - Wired `configs/exchanges.yaml` (last config file with no reader)

### Context
Re-ran the standing audits at current HEAD before looking for new work:
event-bus producer/subscriber (22/22 wired, matches `[1.3.11]`'s
re-audit — nothing regressed) and Prometheus metrics (18/18 incremented
outside `metrics.py`, matches `[1.3.4]`/`[1.3.8]` — nothing regressed).
Both clean, so this session's audit turned to the one config-reader
class of bug (`risk.yaml` → `[1.2.x]`, `monitoring.yaml` → `[1.3.4]`,
`strategy.yaml`'s SMC/indicator block → `[1.3.7]`) that hadn't been
checked against `configs/exchanges.yaml` specifically. It hadn't: no
`yaml.safe_load` call, no `_load_exchanges_yaml`-shaped method, nothing
— despite a comment in `exchange/adapters/bybit/rest.py` explicitly
claiming exchange connectivity settings come "in `bootstrap`, from
`configs/exchanges.yaml`."

Two independent hardcoding paths were silently shadowing the file:

- `BinanceConfig.rest_base_url`/`ws_base_url` and
  `BybitConfig.rest_base_url` picked between two hardcoded literals keyed
  only on `self.testnet` — module-level constants
  (`BINANCE_FUTURES_MAINNET_REST`, etc.) for Binance, inline string
  literals for Bybit. `rest_mainnet`/`rest_testnet`/`ws_mainnet`/
  `ws_testnet` weren't fields on either config class, so the yaml keys of
  the same name weren't just unread — there was nowhere for a merge to
  land them.
- `max_orders_per_second`/`max_orders_per_minute` weren't fields either.
  `BinanceFuturesRESTClient.__init__`/`BybitFuturesClient.__init__` each
  constructed their `RateLimiter` with `max_per_second=10,
  max_per_minute=1200` (Binance) / `600` (Bybit) as inline literals.

This is why it went unnoticed for 15 versions: every hardcoded literal
happens to equal the yaml file's shipped default, so a fresh checkout
"works" — the gap only bites the moment someone actually edits
`exchanges.yaml` to point at a different endpoint or rate-limit ceiling,
which nobody had done yet in any prior session.

### Fixed — `src/core/config.py`
- `BinanceConfig`: new fields `rest_mainnet`, `rest_testnet`, `ws_mainnet`,
  `ws_testnet` (defaults = the same four module constants the properties
  used to close over directly — no default-value change), plus
  `max_orders_per_second`/`max_orders_per_minute` (defaults `10`/`1200`,
  matching the old inline `RateLimiter(...)` call). `rest_base_url`/
  `ws_base_url` now read `self.rest_testnet if self.testnet else
  self.rest_mainnet` (and the ws equivalent) instead of the module
  constants.
- `BybitConfig`: same shape — new `rest_mainnet`/`rest_testnet` fields
  (defaults = the same two literals the property used to return
  directly) and `max_orders_per_second`/`max_orders_per_minute`
  (defaults `10`/`600`). `rest_base_url` now reads the two new fields.
  `ws_public_url`/`ws_private_url` are untouched — `exchanges.yaml` has
  no `bybit.ws_*` keys (its own comment notes Bybit's live WS wiring
  isn't reachable from `Application` yet, per `ROADMAP.md`), so there's
  nothing there to wire.
- New `Settings._load_exchanges_yaml()`, registered as a fourth step in
  `load_yaml_overrides()` alongside risk/symbols/monitoring. Identical
  env > yaml > field-default precedence via `model_fields_set`, same
  pattern as `_load_risk_yaml`. No-ops per-exchange if `self.binance`/
  `self.bybit` is `None` (credentials absent, or the other provider is
  active) — mirrors how the rest of `Settings` already treats those two
  fields as optional.

### Fixed — `src/exchange/binance/rest.py`, `src/exchange/adapters/bybit/rest.py`
- `BinanceFuturesRESTClient.__init__`/`BybitFuturesClient.__init__` gain
  `max_orders_per_second`/`max_orders_per_minute` keyword params (same
  defaults as before: `10`/`1200` and `10`/`600`), threaded into the
  `RateLimiter(...)` call in place of the literals. `max_per_10_seconds`
  stays hardcoded at `100` on both — `exchanges.yaml` has no field for a
  10-second-window ceiling, so there's nothing to source it from; adding
  a config field nothing writes to would just be a second unwired knob.
  Every existing call site (`exchange_factory.py`, `scripts/*.py`) uses
  keyword args and omits these two, so all of them keep the exact same
  runtime behavior via the unchanged defaults.

### Fixed — `src/bootstrap/exchange_factory.py`
- `build_rest_client()` passes `max_orders_per_second`/
  `max_orders_per_minute` from `binance_cfg`/`bybit_cfg` through to the
  client constructor — the last hop needed to make an edited
  `exchanges.yaml` value actually reach the running `RateLimiter`.

### Not wired in this pass
`scripts/manual_order.py` still constructs `BinanceFuturesRESTClient`
without passing `max_orders_per_second`/`max_orders_per_minute` from
`settings.binance`, so it runs at the (correct, matching) defaults
rather than a possibly-customized `exchanges.yaml` value. Left alone
deliberately: it's a manual, `--confirm`-gated, single-operator CLI
tool, not the automated path, and the existing `base_url`/`recv_window`
args it already passes from `settings.binance` are the ones that matter
for correctness (hitting the right endpoint); rate-limit ceiling on
that path is a minor operator-experience nicety, not a bug, and adding
it wasn't worth the drive-by scope creep in this session.

A second config-reading gap surfaced during the same audit, in
`configs/logging.yaml`: it's never loaded by any code path at all (no
`yaml.safe_load` call anywhere) — `monitoring/telemetry.py`'s
`configure_logging()` is a fully separate, hand-rolled `structlog`
setup that happens to hardcode the same noisy-logger silence list
(`asyncio`/`websockets`/`aiohttp`/`sqlalchemy.engine`) as the yaml
file's `loggers:` section, but never applies the yaml's `file` handler
— so the configured `RotatingFileHandler` (`logs/trading.log`, 50MB ×
10 backups) never runs and nothing is ever persisted to disk; every log
line only ever reaches stdout. This is a materially different, larger
fix (either wire `logging.config.dictConfig()` to read the yaml file
directly, or extend `configure_logging()` with structlog-native file-
rotation support and decide which one wins if both are present) and
felt like the wrong thing to fold into an already-in-flight change
under the same "config ignored" umbrella without flagging it first.
Flagging for next pass rather than fixing blind.

### Tests
- `tests/core/test_config.py`: new `TestExchangesYamlLoading` class —
  yaml values apply per-provider when no env override exists (including
  asserting `rest_base_url`/`ws_base_url` resolve off the *merged*
  fields, not just that the fields themselves got set), explicit env var
  still wins over yaml, missing file is a silent no-op, and the yaml's
  `bybit:` section merging into `settings.bybit` doesn't raise when
  `settings.bybit is None` (binance-only run).
- `tests/exchange/test_binance_rest.py`,
  `tests/exchange/test_bybit_rest.py`: new `TestRateLimiterConstruction`
  classes on both — custom `max_orders_per_second`/`max_orders_per_minute`
  reach `client._rate_limiter.max_per_second`/`.max_per_minute`, and
  omitting them reproduces the exact old hardcoded defaults.

### Verification
Full `ast.parse` sweep across all 299 `.py` files (`src/` + `tests/` +
`scripts/`), 0 errors. Confirmed via Pydantic's own docs (network access
for `pip install pydantic` isn't available in this sandbox, so no local
`pytest` run — same standing constraint as every prior session) that
`BaseModel.model_dump()` in default (python) mode preserves `SecretStr`
fields as live `SecretStr` objects rather than the masked string, so the
`{**self.binance.model_dump(), **overrides}` → `BinanceConfig(**merged)`
round-trip `_load_exchanges_yaml` uses (identical to `_load_risk_yaml`'s
established pattern) doesn't leak or corrupt `api_key`/`api_secret` —
this is the first of the four `_load_*_yaml` methods to reconstruct a
model that actually holds secrets, so it's the first time this needed
checking rather than being inherited for free from precedent.

## [1.3.14] - Merged two more unmerged archives on top of the [1.3.13] reconciliation

### Context
This session received 20 archives spanning `[1.2.0]` through a labeled
`v1.3.13`. A full manifest diff (every `.py`/`.yaml`/`.md` file across all
20, deduplicated) against the `[1.3.13]` tree found only three files
present anywhere in the set but absent from `[1.3.13]`:
`src/exchange/binance/orders.py`, `scripts/manual_order.py`, and
`tests/exchange/test_binance_orders.py` — all three from a single
archive (locally labeled `v1_3_8-2`). Everything else in `[1.2.0]`
through `[1.3.6]` was already confirmed present in `[1.3.13]`'s working
tree by spot-checking one representative symbol per version (e.g.
`fvg_at_price()`'s caller for `[1.2.1]`, `MTFContext` for `[1.3.2]`) —
consistent with `[1.3.4]`'s "baseline commit" note that prior versions'
code, if not their git history, already carried forward.

Two archives turned out to be genuine unmerged work rather than restating
`[1.3.13]`:

- **`v1_3_10`** — continues branch B's lineage (`[1.3.6]` → ... →
  `[1.3.12]` `BinanceOrderManager` removal) one commit further than what
  existed when the three-way fork was reconciled. Not one of the three
  branches diffed at that time, so it was never pulled in.
- **`v1_3_8-2`** — diverged from the same `[1.3.6]` base as branch B, but
  reached the *opposite* disposition on `BinanceOrderManager`: kept and
  wired it in (manual-CLI only) rather than deleting it, per that
  session's own changelog citing "explicit sign-off." This directly
  conflicts with `[1.3.12]`'s deletion, which `[1.3.13]` already adopted.

### Added — from `v1_3_10` (RollingTimer/TTLCache wiring)
- `src/risk/exposure.py`: `CorrelationTracker.matrix()` now caches its
  result via `TTLCache` (single slot, `default_ttl=5.0` safety net);
  `update()` invalidates the entry the instant new data arrives. Removed
  the `_dirty` flag — it was set on `update()` and cleared at the end of
  `matrix()` but never actually checked, so `matrix()` was silently
  recomputing the full O(n²) pairwise correlation on every call
  regardless of whether anything had changed.
- `src/strategy/orchestrator.py`: per-strategy `RollingTimer` (window=100)
  recorded alongside the existing `candle_processing_latency` Prometheus
  histogram in `_on_candle_closed`, exposed via
  `.stats["candle_processing_latency_ms"]` as `{strategy_id: {avg, p99}}`.
- `src/bootstrap/app.py`: `Application.orchestrator` (optional, set
  post-construction) included in `_dashboard_metrics()["strategy"]` — the
  orchestrator's `.stats` was previously only visible to
  `LifecycleManager`, unreachable from the dashboard `/metrics` endpoint
  or WS snapshot broadcast.
- `src/main.py`: `app.orchestrator` wired post-construction;
  `SMCSignalEngine.analyze()` wrapped in `timer()` (100ms default warn
  threshold) for an immediate SLOW-call log line scoped to just the SMC
  analysis pipeline, independent of Prometheus. Applied as a targeted
  patch rather than a file-level copy — `v1_3_10`'s `main.py` predates
  branch A's SMC/indicator `strategy.yaml` config wiring (only reconciled
  in `[1.3.7]`/`[1.3.13]`), and a raw copy would have silently reverted
  that wiring back out.

### Added — from `v1_3_8-2` (BinanceOrderManager disposition)
- `src/exchange/binance/orders.py`: restored (deleted in `[1.3.12]`),
  plus new `BinanceOrderManager.place_bracket_order()` — composes entry
  (market or limit) + stop-loss + take-profit into one call, the
  capability the module docstring always claimed but no caller had
  assembled. Exit legs use the side opposite entry and are always
  `reduce_only`. Conservative failure handling: entry failure propagates
  with no exit legs attempted; a failed stop or take-profit leg returns
  immediately with whichever subset of `{entry, stop_loss, take_profit}`
  landed, logged at ERROR — no automatic unwind, since flatten vs. retry
  vs. leave-and-protect-manually is a caller decision, not this method's.
- `scripts/manual_order.py`: new operator CLI built on
  `place_bracket_order()`. Deliberately **not** wired into
  `ExecutionEngine`'s automated signal path — that pipeline
  (`ExecutionEngine` → `src/execution/order_manager.py`'s `OrderManager`)
  does several things `BinanceOrderManager` does not: RiskManager
  pre-trade validation, idempotent submission via client-order-id dedup,
  order-lifecycle events to the bus, ambiguous-failure recovery,
  crash-recovery persistence, and Binance/Bybit parity
  (`BinanceOrderManager` is Binance-only). Routing live signals through
  it instead would silently drop all of that. Requires an explicit
  `--confirm` flag and prints the full order plan before sending
  anything.
- `src/execution/order_manager.py` (the real automated-path order
  manager — unrelated class to `BinanceOrderManager` above):
  `RollingTimer` wired into `submit_order()`, tracking exchange
  round-trip latency for order submission. Exposed via
  `.stats["submit_latency_ms"]` (avg/p99), already surfaced on the
  dashboard via `Application._dashboard_status()`. Only successful
  submissions record a sample — a caller retry after ambiguous or
  definite failure means multiple round-trips for one logical
  submission, and averaging those in would misrepresent steady-state
  latency.

### Flagged for confirmation
Restoring `BinanceOrderManager` reverses part of what `[1.3.12]`/`[1.3.13]`
already merged. Both dispositions are individually defensible — branch
B's audit found zero *production* callers and treated that as dead code;
`v_1_3_8-2` kept it for a narrow, explicitly-scoped manual use case behind
a `--confirm`-gated CLI, outside the automated path either way. Restored
here on the strength of the "explicit sign-off" note in that archive's own
changelog, since that reads as direct instruction rather than another
branch's unaware guess — but this is the one judgment call in this merge
that isn't a mechanical reconciliation, and Allan should say if the
deletion was actually the intended final call.

### Tests
- `tests/exchange/test_binance_orders.py` (restored): gains
  `TestPlaceBracketOrder` — market vs. limit entry, exit-side inversion
  for both BUY and SELL, entry-failure propagation, partial-failure
  result sets for both exit legs, client-order-id prefixing per leg.
- `tests/execution/test_order_manager.py`: gains
  `TestSubmitLatencyTracking` — zero-state default, a sample recorded on
  success, no sample recorded on definite failure.
- `tests/unit/test_profiling_utils.py` / `tests/unit/test_cache_utils.py`
  were already present in `[1.3.13]` and unchanged — pre-existing
  coverage for `RollingTimer`/`timer()`/`TTLCache` themselves; this
  entry's tests cover their new call sites, not the utilities.

### Verification
- Full `ast.parse` + `py_compile` sweep across `src/`, `tests/`,
  `scripts/`: 299 files, 0 errors (up from 296 in `[1.3.13]`).
- `grep` confirms no module outside `scripts/manual_order.py` and
  `tests/exchange/test_binance_orders.py` imports from
  `exchange.binance.orders` — restoration is additive with zero
  call-site fallout elsewhere.
- Manifest diff re-run post-merge: the file set now matches the full
  union of all 20 input archives exactly — nothing from any archive
  remains unmerged.
- `pytest` still not run in-sandbox (`pydantic`/`aiohttp` unavailable,
  same standing constraint as every prior session) — needs Allan's local
  environment, particularly the two new test classes above.
- `pyproject.toml` version bumped `1.3.13` → `1.3.14`.

## [1.3.13] - Reconciled three-way fork (branches diverged from [1.3.6])

### Context
Three separate archives were independently developed forward from the
same `[1.3.6]` base, in what turned out to be non-overlapping parallel
sessions, and were never merged into a single tree before now:

- **Branch A** — SMC/indicator `strategy.yaml` config wiring
  (`main.py`, `smart_money/confluence.py`, `smart_money/order_blocks.py`,
  `smart_money/premium_discount.py`, `strategy/signal_engine.py`)
- **Branch B** — Bybit REST error-handling fix, `SignalExpiredEvent`
  wiring, dead `BinanceOrderManager` removal
  (`bootstrap/startup.py`, `exchange/adapters/bybit/rest.py`,
  `exchange/base_client.py`, `monitoring/metrics.py`,
  `monitoring/metrics_collector.py`, removal of `exchange/binance/orders.py`)
- **Branch C** — `advanced_metrics.py`/SQN wiring into performance
  reporting, backtest/live position-sizing formula drift fix
  (`analytics/performance.py`, `backtesting/engine.py`,
  `backtesting/optimization.py`, `backtesting/reports.py`,
  `backtesting/simulator.py`)

Each branch had reused colliding version numbers (`[1.3.7]` was used by
all three branches for a different fix; `[1.3.8]` by two of them) since
each session numbered sequentially from its own `[1.3.6]` starting point
with no visibility into the others. Taking any single one of the three
archives as "latest" would have silently discarded the other two
branches' fixes — this was caught by diffing all three tips against the
common `[1.3.6]` ancestor before any file was treated as canonical.

### How this was merged
The three branches touch **entirely disjoint files** — confirmed by a
full `diff -rq` of each branch's `src/` and `tests/` against the
`[1.3.6]` base before merging. No file was modified by more than one
branch, so this was a mechanical three-way merge (via the repo's
restored git history: one commit per branch off the `[1.3.6]` commit,
then sequential `git merge`) with zero conflicts — nothing needed manual
reconciliation at the code level, only at the changelog/version-number
level below.

### Version renumbering
The colliding entries below have been retroactively renumbered into a
single sequential chain, in the order Branch A → Branch C → Branch B
(this ordering reflects merge order, not verified wall-clock
chronology — the three sessions' relative timing to each other is
unknown). **Only header lines were changed**; body content is otherwise
verbatim from each original archive, same convention as the `[1.1.x]`
renumbering documented in this file's opening note:

| Original label | Branch | New label |
|---|---|---|
| `[1.3.7]` "9 dead SMC/indicator config values" | A | `[1.3.7]` (unchanged) |
| `[1.3.7]` "advanced_metrics.py + SQN" | C | `[1.3.8]` |
| `[1.3.8]` "position-sizing formula drift" | C | `[1.3.9]` |
| `[1.3.7]` "Bybit REST error-swallowing" | B | `[1.3.10]` |
| `[1.3.8]` "SignalExpiredEvent" | B | `[1.3.11]` |
| `[1.3.9]` "BinanceOrderManager removal" | B | `[1.3.12]` |

One in-body cross-reference (Branch B's `BinanceOrderManager` entry
citing its own immediate predecessor) was updated to point at the new
`[1.3.11]` label instead of the stale `[1.3.8]`; no other cross-reference
in any of the six entries pointed at a renumbered sibling.

### Verification
- Full `ast.parse` sweep across merged `src/` and `tests/`: 0 errors.
- Spot-checked all six fixes coexist correctly in the merged tree (SMC
  `premium_zone` kwarg, Bybit non-200 raise path, `BinanceOrderManager`
  absent, `signals_expired` metric, `advanced_metrics` imports in
  `performance.py`, `sizing_fee_rate`/`calculate_position_size` in
  `simulator.py`).
- Runtime `pytest` still not possible in this sandbox
  (`pydantic`/`aiohttp` unavailable) — same standing constraint as every
  prior session; needs Allan's local environment.

## [1.3.12] - Removed dead `BinanceOrderManager`

### Context
Standing dead-code scan (companion to the event-bus audits in
`[1.3.4]`/`[1.3.6]`/`[1.3.11]`) previously flagged five `OrderManager`-adjacent
REST methods as having test-only callers with no confirmed production path.
Traced the actual call graph: `place_market_order`, `place_limit_order`,
`place_stop_market`, `place_take_profit_market`, and
`poll_order_until_terminal` all live on `src/exchange/binance/orders.py::BinanceOrderManager`,
a standalone helper class distinct from the real
`src/execution/order_manager.py::OrderManager`. `BinanceOrderManager` is
never instantiated anywhere in `src/` — production order submission goes
through `IExchangeClient.place_order` directly (per the `IExchangeClient`
widening documented in earlier lineage notes). The class's only callers
were its own unit tests.

### Removed
- `src/exchange/binance/orders.py` (`BinanceOrderManager` and all 5 methods)
- `tests/exchange/test_binance_orders.py` (tests existed solely to cover
  the orphaned class)

No other module imported from `exchange.binance.orders`; removal is a pure
deletion with zero call-site fallout, confirmed by grep across `src/` and
`tests/` post-removal.

## [1.3.11] - Wired SignalExpiredEvent; full event-bus re-audit

### Context
Standing event-bus producer/subscriber audit, same class of check as
`[1.3.4]`/`[1.3.6]`, re-run against the current 22 named event types
(`ast`-based producer scan: instantiation call sites; subscriber scan:
`bus.subscribe(EventType, ...)` call sites, plus checking whether an
apparently-orphaned event is actually consumed by direct method call
instead of the event bus, as `OrderBookUpdatedEvent`/`FundingRateEvent`
are via `MarketDataCache.put_best_bid_ask`).

Of the 22, 21 were already correctly wired — including `OrderSubmittedEvent`,
`OrderStatusChangedEvent`, and `SystemStartedEvent`, which a stale note
from an earlier session's summary had flagged as still-unwired; re-checked
by tracing to `MetricsCollector._on_order_submitted` and
`startup.py`'s `_on_order_status_changed`/`_on_system_started`, both
confirmed live and reachable from `lifecycle.py::startup()`, the real
bootstrap entry point. That note was outdated as of `[1.3.4]`; no action
needed there.

One genuine gap remained: `SignalExpiredEvent`, published from two sites
(`orchestrator.py` on generation-time expiry, `execution_engine.py` on
in-transit expiry — both traced back to `[trading-final-2-1]`'s original
merge, see the `[1.2.x]` reconciliation entry) had zero subscribers
anywhere — not in-process, not via the Redis notifications worker
(`scripts/notifications_worker.py`'s `DEFAULT_EVENT_TYPES` doesn't include
it either). Every signal expiry evaporated on the bus with no visibility
into expiry rate or cause.

### Added — `src/monitoring/metrics.py`
- `signals_expired` Counter (`trading_signals_expired_total`), labeled
  `symbol`/`reason`. `reason` is free text on the event schema (like
  `OrderCancelledEvent.reason`, which `orders_cancelled` already labels
  the same way) but has exactly two fixed producer literals today
  (`expired_at_generation`, `expired_in_transit`), so cardinality is safe
  under the same reasoning already documented for `orders_cancelled`.

### Added — `src/monitoring/metrics_collector.py`
- `MetricsCollector._on_signal_expired`, subscribed in `start()` alongside
  the other 8 event-driven metrics. Same defensive try/except-and-log
  pattern as every other handler — never raises out of the trading path.

### Added — `src/bootstrap/startup.py`
- New step 7: `SignalExpiredEvent` → structured `logger.info` audit-trail
  line (`signal_expired signal_id=... symbol=... reason=...`), subscribed
  in `startup()`. Not routed through `Notifier`/`AlertFiredEvent`: mirrors
  step 5's `OrderStatusChangedEvent` treatment — expiry is a normal (if
  worth watching) occurrence, not an incident on its own. The
  `signals_expired` counter above covers the aggregate-rate/alerting use
  case if that's ever wanted (e.g. an `AlertRule` on
  `rate(trading_signals_expired_total[5m])`); this log line covers the
  per-instance forensic trail.

### Tests
- `tests/monitoring/test_metrics_collector.py`: new `TestSignalExpired`
  class (label increments, distinct-reason label separation), matching
  `TestSignalGenerated`'s structure.
- `tests/bootstrap/test_startup.py`: new `TestSignalExpiredAuditTrail`
  class, matching `TestOrderStatusChangedAuditTrail`'s structure and
  fixtures (`_fake_app`/`_make_settings`/`isolated_event_bus`/`caplog`)
  exactly.

### Verification
Full `py_compile` sweep across `src/` and `tests/` (0 errors, 291 files
— 289 prior + this changelog's 2 new/modified test-touching files don't
add file count since both test files already existed). `ast.parse` on
every touched file individually. Runtime `pytest` execution still isn't
possible in this sandbox — needs Allan's local environment, same
constraint as every prior session; the new tests follow their sibling
classes' exact structure closely enough that a local run is expected to
pass, but that's an expectation, not a verified result.

## [1.3.10] - Fixed silent error-swallowing in Bybit REST client; added rest.py test coverage

### Context
Continuing the standing test-coverage audit: cross-checked every `src/`
module against `tests/` (by direct import, not just filename matching,
to avoid false positives from consolidated test files). Found
`src/exchange/adapters/bybit/rest.py` — the actual Bybit HTTP client,
338 lines, including all request/response and error-mapping logic —
had zero dedicated tests. `auth.py` and `parser.py` were already
covered indirectly via `test_bybit_adapter.py`, but `rest.py` itself
was not. Writing that coverage surfaced two related, previously
unflagged defects in the client's error handling, both fixed here.

### Fixed — `src/exchange/adapters/bybit/rest.py`
- `_handle_response_error`'s guard was
  `if status != 200 or not isinstance(body, dict): return` — the exact
  inverse of the intended check. This returned *without raising* for
  every non-200 HTTP status (401/403/404/etc. — anything below the
  `>=500` threshold already handled upstream in
  `base_client._execute_request`), silently treating gateway-level
  errors as successful responses and handing the raw error body back
  to the caller as if it were valid account/order/market data. Bybit
  V5 normally signals API-level errors as HTTP 200 with a non-zero
  `retCode`, which is presumably how this went unnoticed — the
  200+`retCode`-nonzero path was correctly handled and exercised
  (indirectly) by existing tests; the non-200 path never was.
  Rewrote to raise on non-200 responses too, using `retCode`-based
  classification when a Bybit-shaped error body is present (e.g. a
  proxy passing `retCode` through on a non-200) and falling back to
  HTTP-status-based classification (401/403 → `ExchangeAuthenticationError`,
  else `ExchangeAPIError`) when it isn't. The one pre-existing edge
  case worth flagging: status==200 with a non-dict body still returns
  silently (unchanged from before) — that narrow case is left as-is
  since it isn't the bug this fix targets and changing it wasn't
  verified against a real Bybit response shape.

### Fixed — `src/exchange/base_client.py`
- Same audit surfaced a second, related defect in the shared clock-drift
  resync path: `if response.status != 200 and self._is_clock_drift_error(body): ...`.
  Binance's timestamp error (-1021) does arrive on a non-200 status, so
  this worked for Binance, but Bybit's equivalent (`retCode` 10002)
  arrives on HTTP 200 with the error carried only in the body — meaning
  the resync-and-retry path was permanently unreachable for Bybit.
  Dropped the `status != 200` gate; `_is_clock_drift_error(body)` alone
  is already a sufficiently narrow, adapter-specific check (matches
  Binance's `code == -1021` and Bybit's `retCode == 10002` exactly), so
  removing the status gate fixes Bybit without changing Binance's
  behavior (whose drift error never arrives on 200 in the first place).

### `tests/exchange/test_bybit_rest.py` (new)
- Full `BybitFuturesClient` coverage mirroring `test_binance_rest.py`'s
  conventions (real local `aiohttp.web.TestServer`, not a mocking
  library — see that file's docstring for why): account balance,
  positions, place/cancel/get/list orders (including the `ts_`-prefixed
  client-order-id path and the order-not-found-treated-as-already-cancelled
  case), candles/ticker/order-book/server-time, leverage/margin-type
  configuration, and a dedicated `TestErrorMapping` class covering both
  fixes above — including a regression test for the exact swallowed-401
  scenario the first bug allowed, and a resync-and-retry test for the
  second.

### Housekeeping
- `pyproject.toml`'s `version` field had been left at `1.3.2` since that
  release despite `CHANGELOG.md` moving on through `1.3.6` — bumped to
  match this release. Worth a standing check each session: the two
  drifted silently for four releases with nothing surfacing it.

### Verification
Full `ast.parse`/`py_compile` sweep across `src/` and `tests/` (0
errors). Manually traced both fixed code paths against
`base_client._execute_request`'s call sequence line-by-line (status
dispatch order, what's already handled before `_handle_response_error`
is reached, what the clock-drift branch does with the exception it
raises) to confirm the new logic is reachable and correct, since
runtime pytest execution still isn't possible in this sandbox
(`aiohttp` unavailable) — needs Allan's local environment, same
constraint as every prior session.

## [1.3.9] - Fixed backtest/live position-sizing formula drift

### Context
`BacktestBroker.place_signal()` (`src/backtesting/simulator.py`)
reimplemented fixed-fractional position sizing inline
(`equity * risk_pct/100 / stop_distance`) instead of delegating to
`risk/position_sizing.py::calculate_position_size()` — the exact
function `RiskManager.calculate_position_size()` uses live. The inline
copy silently omitted the fee adjustment the real formula applies
(`effective_risk_per_unit = stop_distance + 2*entry_price*fee_rate`),
so backtests sized positions very slightly larger — and therefore risked
very slightly more per trade — than live trading would for an identical
signal. Same "two implementations quietly drift apart" bug class already
found and fixed once in this codebase, for Sharpe/Sortino's std ddof
convention (`analytics/performance.py`, prior session).

The drift is small in absolute terms (a fraction of a percent of
quantity at typical stop distances — e.g. 0.1 vs. ~0.09615 units for a
$10k/1%/$50k-entry/$1k-stop example) but means every backtest run to
date sized positions off-formula relative to what the live system would
actually do, undermining backtest-to-live parity for exactly the
quantity a strategy is risking.

### `src/backtesting/simulator.py`
- `place_signal()` now calls `calculate_position_size()` directly
  instead of the inline duplicate.
- New `BacktestBroker.__init__` param `sizing_fee_rate: Decimal =
  Decimal("0.0004")` — separate from `commission` (which charges the
  *actual* fee on each fill); this is the fee rate the sizing formula
  itself assumes when shrinking quantity so realized risk still lands
  at `risk_pct` after real fees are deducted. Defaults to the same
  0.0004 both `PercentageCommission` and `calculate_position_size()`
  already default to independently.

### `src/backtesting/engine.py`
- `BacktestConfig` gained `sizing_fee_rate: Decimal = Decimal("0.0004")`,
  threaded to `BacktestBroker`. Every existing `BacktestConfig(...)`
  call site (`scripts/backtest.py`, `scripts/optimize.py`,
  `scripts/train_signal_filter.py`) uses the default, so this reproduces
  the fee-adjusted (correct) sizing everywhere without any script
  changes needed — the *only* behavioural difference from before this
  fix is that quantities are now fee-adjusted like live, which is the
  point of the fix.

### Tests
- `tests/backtesting/test_simulator.py::TestPositionSizingParity` (2
  tests): confirms a placed order's quantity matches
  `calculate_position_size()` called directly with the same inputs, and
  that `sizing_fee_rate=Decimal("0")` recovers the exact pre-fix
  quantity (0.1) — proving the fee term is what changed, not the
  underlying risk-pct/stop-distance math.
- Verified the one pre-existing exact-quantity assertion in this file
  (`TestPartialFillCapping`, `equity=1_000_000, risk_pct=50%`) is
  unaffected: raw sized quantity there is still ~481 units either way,
  far above the 0.1-unit partial-fill cap that test actually checks.
  No other test in the suite asserts an exact sizing quantity.

### Verification
Full `ast.parse` sweep across `src/` and `tests/` (0 errors); `py_compile`
on every touched file. Grepped all `BacktestConfig(...)` and
`BacktestBroker(...)` construction sites in `src/`, `scripts/`, and
`tests/` to confirm none pass a conflicting positional argument the new
field could shift. Runtime pytest execution still isn't possible in this
sandbox — needs Makaveli's local environment.

## [1.3.8] - Wired the entire advanced_metrics.py module + SQN into performance reporting

### Context
Continuing the standing "implemented but never wired" audit (same class
of bug as `fvg_at_price`/`MTFContext` in earlier sessions, and the dead
events fixed in `[1.3.6]`): `src/analytics/advanced_metrics.py` — 8
pure, unit-tested functions (`tests/unit/test_advanced_metrics.py`, 19
tests) — and `expectancy.py::system_quality_number()` had zero callers
anywhere in `src/` or `scripts/`. The module's own docstring says it
exists "to be used identically by the backtester, the paper-trading
report, and live analytics" but never actually was — `PerformanceReport`
only ever computed the basic Sharpe/Sortino/Calmar set.

### `src/analytics/performance.py`
- `PerformanceReport` gained 8 new fields: `probabilistic_sharpe_ratio`,
  `deflated_sharpe_ratio`, `omega_ratio`, `tail_ratio`, `ulcer_index`,
  `historical_var_95`, `historical_cvar_95`, `system_quality_number`.
- `calculate_performance()` now computes all 8 from the same `returns`
  array and `equity_curve` already built for Sharpe/Sortino/drawdown —
  no new data plumbing needed, just wiring. Chose `historical_var`/
  `historical_cvar` over `parametric_var` for the VaR fields, per
  `parametric_var`'s own docstring recommendation ("prefer
  `historical_var`/`historical_cvar`... for fat-tailed crypto returns"
  when a full return series is available, which it always is here).
- New `n_trials: int = 1` parameter, forwarded to
  `deflated_sharpe_ratio()`. Default 1 = "a single pre-registered
  backtest, no parameter search," in which case
  `deflated_sharpe_ratio == probabilistic_sharpe_ratio` exactly.
- `to_dict()`: new keys for all 8 metrics; `omega_ratio`/`tail_ratio`
  fall back to `str()` when non-finite (both can legitimately be `inf`,
  e.g. a trade sample with zero losers) so they don't break JSON
  serialization the way a bare `float('inf')` would.

### `src/backtesting/engine.py`
- `BacktestEngine.run()` gained a keyword-only `n_trials: int = 1`,
  forwarded to `calculate_performance()`. Backward compatible — every
  existing call site (positional/keyword args before this point) is
  unaffected by the default.

### `src/backtesting/optimization.py` / `scripts/optimize.py`
- `optimize()`'s OOS validation run now passes `n_trials=n_trials` (the
  actual Optuna search size) instead of the engine's default of 1 — this
  is the exact scenario `deflated_sharpe_ratio`'s docstring describes
  ("you tried 200 confluence-weight combinations and kept the best
  one"): the OOS Sharpe on `result.best_params` *is* the result of an
  n_trials search and should be judged as one, not treated as if it were
  a single pre-registered backtest. `scripts/optimize.py` now also
  prints the deflated Sharpe alongside the raw one.

### `src/backtesting/reports.py`
- `print_backtest_summary()` displays all 8 new metrics in the rich
  table output (Ulcer Index, Prob./Deflated Sharpe, Omega Ratio, Tail
  Ratio, Historical VaR/CVaR 95%, SQN).

### Tests
- `tests/unit/test_analytics.py::TestAdvancedMetricsWiring` (7 tests):
  confirms all 8 metrics are actually populated (not left at their 0.0
  default) for a varied trade sample; `deflated_sharpe_ratio ==
  probabilistic_sharpe_ratio` at `n_trials=1`; DSR is monotonically
  non-increasing as `n_trials` grows (a real correctness property of the
  underlying math, not just a wiring smoke test); `ulcer_index == 0.0`
  for a monotonically rising equity curve; `system_quality_number`
  stays `0.0` without `Trade.initial_risk`; `to_dict()` serializes
  infinite `omega_ratio` as `"inf"` rather than raising; all 8 keys
  present in `to_dict()`.
- `tests/unit/test_analytics.py::TestEngineNTrialsPassthrough` (2
  tests): `BacktestEngine.run()` calls `calculate_performance()` with
  `n_trials=1` by default and forwards an explicit `n_trials=75`
  correctly (via `unittest.mock.patch(..., wraps=calculate_performance)`
  spying on the real call rather than a bare mock, so the underlying
  computation still runs).

### Verification
Full `ast.parse` sweep across `src/` and `tests/` (0 errors); `py_compile`
on every touched file. Confirmed no code anywhere constructs
`PerformanceReport(...)` positionally (only `PerformanceReport()` +
attribute assignment), so inserting the 8 new dataclass fields mid-class
carries no reordering risk. Runtime pytest execution still isn't
possible in this sandbox — needs Makaveli's local environment.

## [1.3.7] - Wired 9 dead SMC/indicator config values from strategy.yaml

### Context
Continuing the standing "scaffolded ≠ functional" audit (same class of
bug as the event-bus/metrics work in `[1.3.4]`–`[1.3.6]`, applied here to
config instead of events): a full read of every `smc:`/`indicators:` key
in `configs/strategy.yaml` against actual call sites in `src/` found nine
values that were parsed nowhere — each had a hardcoded module-level
constant standing in for it instead, so editing the file silently did
nothing:

- `smc.swing_lookback`, `smc.ob_max_age_bars`, `smc.fvg_min_size_atr_mult`,
  `smc.premium_zone`, `smc.discount_zone`
- `indicators.atr_period`, `indicators.rsi_period`, `indicators.ema_fast`,
  `indicators.ema_slow`

One of these is worse than a dead knob: `premium_zone`/`discount_zone`'s
hardcoded constants (`SMC_PREMIUM_ZONE=0.618`, `SMC_DISCOUNT_ZONE=0.382`)
actively *disagree* with the file's own shipped values (`0.70`/`0.30`).
The file has been telling a different story than the code ran since
before this changelog's `[1.0.0]` entry. After this fix the file's values
take effect, which is a real behavior change to premium/discount zone
classification, not just new coverage — flagged here rather than buried
in the diff.

`swing_lookback` and `atr_period` were a milder case: both were already
real constructor params on `SMCSignalEngine` (not new hardcoded
constants), just never read from the config file — `main.py` relied on
its own literal defaults (`5`, `14`) happening to match the file's
values. Correct today by coincidence, silently wrong the moment someone
edited either key.

### `src/smart_money/premium_discount.py`
- `PremiumDiscountZones` gained `premium_zone`/`discount_zone` fields
  (defaulting to `SMC_PREMIUM_ZONE`/`SMC_DISCOUNT_ZONE`, so an unmodified
  caller sees identical behavior), used by `premium_zone_start`/
  `discount_zone_end` instead of reading the module constants directly.
- `calculate_premium_discount()` and `is_optimal_entry()` both gained
  optional `premium_zone`/`discount_zone` kwargs, `None`-passthrough to
  the constants above.

### `src/smart_money/order_blocks.py`
- `OrderBlock.is_valid()` and `get_active_order_blocks()` both gained an
  optional `max_age_bars` param (`None` → `SMC_OB_MAX_AGE_BARS`, same
  passthrough convention).

### `src/smart_money/confluence.py`
- `score_confluence()` gained `rsi_period`, `ema_fast`, `ema_slow`,
  `ob_max_age_bars` kwargs. The first three feed the RSI/MACD-histogram
  values already computed here for `filter_by_rsi_exhaustion`/
  `filter_by_macd_trend` (metadata only — doesn't touch `total_score`,
  see the existing module comment on why momentum indicators aren't
  folded into the SMC scoring budget). `ob_max_age_bars` now threads into
  both the OB active/valid filter and the freshness-decay curve, which
  previously computed its decay span against `SMC_OB_MAX_AGE_BARS`
  directly regardless of what was passed anywhere else.

### `src/strategy/signal_engine.py`
- `SMCSignalEngine.__init__` gained all seven of the above as optional
  kwargs (`None`-passthrough convention, mirroring the `[1.3.6]`-era
  `confluence_weights` param), stored and forwarded on every `analyze()`
  call: into `get_active_order_blocks()`, `detect_fvgs()`,
  `calculate_premium_discount()`, and `score_confluence()`.

### `src/main.py`
- New `_load_smc_indicator_config()`, mirroring `_load_confluence_weights()`'s
  fail-open pattern: reads `strategy.yaml`'s `smc:`/`indicators:` blocks,
  logs a warning and drops just the one bad key on a malformed value
  (never crashes startup), and additionally guards against
  `premium_zone <= discount_zone` (drops both back to defaults rather
  than producing a zone classification that's premium and discount
  simultaneously, or neither).
- `SMCStrategy.__init__` gained matching kwargs, stored and forwarded to
  every `SMCSignalEngine` built in `initialize()`. `swing_lookback`/
  `atr_period` are handled slightly differently from the other five:
  they're real (non-`Optional`) params on `SMCSignalEngine`, so
  `initialize()` only overrides them when a config value was actually
  supplied, rather than passing `None` through.
- `build_strategy()` calls the new loader and splats its result into the
  `SMCStrategy` constructor alongside the existing `confluence_weights`
  wiring.

### `tests/`
- `tests/strategy/test_confluence.py`: `TestOrderBlockMaxAgeConfigThreading`,
  `TestRsiMacdPeriodConfigThreading` — custom `ob_max_age_bars` changes
  OB freshness-decay scoring and the active/valid gate; custom
  `rsi_period`/`ema_fast`/`ema_slow` reach `result.metadata`; omitted
  kwargs match prior hardcoded-constant behavior exactly.
- `tests/strategy/test_order_blocks.py`: `TestIsValidCustomMaxAge` plus
  new cases on the existing `get_active_order_blocks` test class —
  custom `max_age_bars` both widens and narrows the validity window
  relative to the default constant; mitigated OBs stay invalid
  regardless.
- `tests/unit/test_smart_money.py`: `TestPremiumDiscountConfigurableZones`
  — custom `premium_zone`/`discount_zone` shift the classification
  boundary on both `PremiumDiscountZones` directly and through
  `calculate_premium_discount()`/`is_optimal_entry()`; omitted kwargs
  match the `0.618`/`0.382` defaults exactly.
- `tests/strategy/test_signal_engine.py`: `TestSmcIndicatorConfigPassthrough`
  — monkeypatch-spy pattern (mirroring the existing `confluence_weights`
  passthrough test) confirming all seven kwargs reach their respective
  downstream calls unchanged, plus a value-level check that a custom
  `premium_zone`/`discount_zone` actually appears on
  `engine.last_analysis.premium_discount`.
- `tests/unit/test_main.py`: `TestLoadSmcIndicatorConfig` — full/partial
  block parsing, the actual shipped `strategy.yaml` values round-trip
  exactly, malformed-value fail-open for each of the three parsers,
  the `premium_zone <= discount_zone` guard (including the equal-values
  edge case). `TestSMCStrategySmcIndicatorConfigPassthrough` — defaults
  to `None`/engine's own hardcoded defaults; custom values reach
  `strategy._engines[...]`; the `swing_lookback`/`atr_period`
  conditional-override path specifically (distinct from the plain
  `None`-passthrough the other five kwargs use).

### Verification
Full `ast.parse` sweep across all 288 files in `src/` and `tests/` — 0
errors. Cross-referenced every remaining direct reference to
`SMC_PREMIUM_ZONE`/`SMC_DISCOUNT_ZONE`/`SMC_OB_MAX_AGE_BARS`/
`SMC_FVG_MIN_SIZE_ATR_MULT`/`DEFAULT_RSI_PERIOD`/`DEFAULT_EMA_FAST`/
`DEFAULT_EMA_SLOW` in `src/` to confirm each one left is a legitimate
fallback default, not a bypassed call site. Manually re-derived and
executed the `is_valid`/`get_active_order_blocks` age-window logic, the
OB freshness-decay curve, the `PremiumDiscountZones` boundary math, and
`_load_smc_indicator_config()` itself as standalone scripts against the
actual shipped `configs/strategy.yaml` (stdlib + `PyYAML`, no `pydantic`
needed for any of these) to confirm the numeric behavior before trusting
the test assertions. Runtime pytest execution still isn't possible in
this sandbox (`pydantic`/`aiohttp`/etc. unavailable) — needs your local
environment.

### Remaining scope (unchanged from `[1.3.3]`'s audit)
- All 18 Prometheus metrics — confirmed fixed as of `[1.3.6]`'s wiring;
  no remaining scaffolded-but-unwired metrics found in this session's
  audit.
- No further dead SMC/indicator config values found in `smc:`/
  `indicators:` — every remaining key in those two blocks (e.g.
  `min_atr_pct`, `htf_timeframe`, `ltf_timeframe`, filter toggles) was
  already confirmed read in prior sessions.

### Note on `.git`
This session's own packaging step (not the code changes above) ran a
`&&`/`;`-chained shell command where an earlier step failed
(`rsync` isn't installed in this sandbox); the failure changed which
directory a later `rm -rf .git` executed in, deleting the `.git`
history `[1.3.5]` had restored. All source/test file *contents* were
verified intact (full `ast.parse` sweep + diff of every changed file
against this entry's description, both after the fact) — this was a
git-history loss, not a code loss. `.git` has been reinitialized fresh
in this archive with a single commit for the current tree; the
`[1.0.0]`–`[1.3.6]` commit-by-commit history is gone and would need to
be reconstructed from this file's narrative record if it matters going
forward. Flagging this explicitly rather than silently re-initializing,
consistent with this project's standing "never trust the changelog,
audit the actual state" principle — applied here to its own tooling.

## [1.3.6] - Wired 2 more dead risk events; event-bus audit re-run

### Context
Continuing the standing event-bus producer/subscriber audit (same class
of bug fixed for `ComponentHealthChangedEvent`/`SystemStartedEvent`/
`SystemShutdownEvent`/`OrderStatusChangedEvent` in `[1.3.4]`): a full
constructed-vs-subscribed cross-check across `src/` found
`DailyLossLimitReachedEvent` and `RiskLimitBreachedEvent` still had
producers (both in `risk_manager.py::validate_signal()`, on the
daily-loss and drawdown hard-limit branches) but zero subscribers — the
specific reason a hard limit tripped never reached on-call beyond the
generic `circuit_breaker_open` alert.

Two other candidates were reviewed and intentionally left as-is:
`FundingRateEvent` and `OrderBookUpdatedEvent` (`market_data/feeds.py`)
are both high-frequency market-data events whose real consumer is
`MarketDataCache` via a direct method call (`put_best_bid_ask`), not the
event bus — `filter_by_spread` (the "future execution-quality guard"
`feeds.py`'s docstring flags) is already wired at signal-generation time
in `orchestrator.py`, reading from that cache. Subscribing them to
anything on the bus today would just be unused overhead on a per-tick
event. `SignalExpiredEvent` (two producers: `orchestrator.py`
`"expired_at_generation"`, `execution_engine.py` `"expired_in_transit"`)
is already `logger.warning`'d at both publish sites and has no
corresponding Prometheus metric to feed — lower priority than the two
wired below, left for a future pass if a `signals_expired` metric is
ever added.

### `src/bootstrap/startup.py`
- New step 6 in the alert/notification wiring block: both events route
  through the existing `AlertFiredEvent` → `Notifier.send()` pipeline.
  `DailyLossLimitReachedEvent` always pages `CRITICAL` (breaching this
  limit unconditionally trips the circuit breaker). `RiskLimitBreachedEvent`
  forwards `event.severity` as-is rather than hardcoding it, since the
  event already carries a severity field and today's one producer
  (`max_drawdown`) passes `CRITICAL` but the schema doesn't guarantee
  every future producer will.
  - This is deliberately *not* a duplicate of the `daily_loss_high`/
    `drawdown_warning` `AlertRule`s registered in step 2: those are
    polling-based early warnings below the hard limit (evaluated every
    `ALERT_EVAL_INTERVAL_SECONDS`, cooldown-throttled); these two fire
    once, immediately, at the instant the hard limit itself is breached.

### `tests/bootstrap/test_startup.py`
- `TestHardLimitRiskEventWiring`: daily-loss event pages CRITICAL with
  the PnL/limit figures in the message; drawdown breach forwards the
  event's own CRITICAL severity; a synthetic non-CRITICAL
  `RiskLimitBreachedEvent` confirms the forwarder doesn't hardcode
  severity.

### Verification
Full `ast.parse` sweep across `src/` and `tests/` (0 errors) plus a
regex-based constructed-vs-`subscribe()`-call cross-check for all 23
event classes in `core/events.py`, confirming both events now show
exactly one subscriber. Runtime pytest execution still isn't possible in
this sandbox (`aiohttp`/`pydantic`/etc. unavailable) — needs Makaveli's
local environment.

## [1.3.5] - Restored .git (no code changes)

### Context
This session's audit (full `ast.parse`/`py_compile` sweep across `src/`
and `tests/`, event producer/subscriber cross-check, and a label-schema
consistency check between `metrics.py` and every `[1.3.4]` call site)
found zero defects — `[1.3.4]`'s work checks out. The one confirmed
problem was structural, not code: this archive again arrived with no
`.git` directory, so nothing from `[1.0.0]`–`[1.3.4]` actually persisted
as history — only the working tree and this file's narrative record did.

### Fixed
- Ran `git init`, added a `.gitignore` (caches, venvs, `.env`, local
  DB/model artifacts), and committed the full working tree as a single
  baseline commit. This is a fresh history, not a reconstruction of the
  1.0.0-through-1.3.4 commit sequence — that sequence was never
  recoverable since no `.git` survived any prior session to build on.
- **Still no remote configured**, and this sandbox has no network
  access, so `git remote add origin <url>` and the first `git push`
  have to happen in Makaveli's local environment, not here. Until that
  push happens, re-extracting this archive is the only thing keeping
  the history alive — the same regression will recur next session if
  the zip is the only copy.

> **Housekeeping note:** the 28 `[Unreleased]` headers that had stacked up
> between `[1.1.1]` and `[1.2.0]` (plus one further back, between `[1.1.0]`
> and `[1.0.0]`) have been retroactively version-bumped to `[1.1.2]`
> through `[1.1.29]` and `[1.0.1]` respectively, in the chronological order
> they already appeared in the file. No content was changed — only the
> header line of each entry.

## [1.3.4] - Instrumented all 18 Prometheus metrics; wired 4 dead events

### Context
Continuing from the prior session's audit: all 18 Prometheus metrics
declared in `src/monitoring/metrics.py` were confirmed to still be
completely uninstrumented (never `.inc()`/`.set()`/`.observe()`'d
anywhere in `src/`), and `SystemStartedEvent`, `SystemShutdownEvent`,
`OrderSubmittedEvent`, `OrderStatusChangedEvent` were confirmed to still
have producers but zero subscribers — the same class of bug fixed for
`ComponentHealthChangedEvent` in `[1.1.29]`. No `.git` history was
present in this archive either, so the persistence issue flagged as
possibly-resolved in `[1.1.29]` has regressed; re-flagging per standing
policy.

### `src/core/events.py`, `src/execution/order_manager.py`
- `OrderSubmittedEvent` was missing an `order_type` field even though
  `orders_submitted` (the metric it feeds) is labeled by `order_type` —
  added the field and threaded `order.order_type` through at the one
  publish site.

### `src/monitoring/metrics_collector.py` (new)
- `MetricsCollector`: event-bus subscriber wiring 8 metrics to their
  domain events — `signals_generated`, `orders_submitted`,
  `orders_filled`, `orders_cancelled`, `trades_closed`,
  `open_positions_count`, `position_pnl`, `circuit_breaker_state`.
  Follows the same `__init__` / `async start()` / bus-subscribe shape as
  `ExecutionEngine`/`RiskManager`. Every handler body is wrapped in
  `try/except Exception: logger.exception(...)` — a metrics-recording
  failure must never propagate into the trading path.
- `PositionClosedEvent` also resets that symbol's `position_pnl` gauge to
  `0.0` (the live unrealized-PnL reading is meaningless once the position
  is closed) and classifies `trades_closed`'s `outcome` label as
  win/loss/breakeven from `realized_pnl`'s sign.
- `CircuitBreakerStateChangedEvent`: `circuit_breaker_state` gauge set to
  `1` only for `OPEN`; `HALF_OPEN` maps to `0` since it still permits a
  single probation trade through (see `RiskManager.validate_signal`) —
  materially different from a hard block.
- Known limitation: `orders_cancelled`'s `reason` label is free text on
  the event rather than a fixed enum (mirrors the metric's pre-existing
  label schema in `metrics.py`, not a new design choice here). The one
  producer today (`OrderManager.cancel_order`) passes a fixed literal, so
  cardinality is bounded as of this change — flagging so it's watched if
  more cancellation call sites are added.

### `src/risk/risk_manager.py`
- `risk_validations` — added `.labels(result="failed"/"passed").inc()`
  next to all 9 existing `self._validations_passed/_failed += 1` sites in
  `validate_signal()`, so the Prometheus counter now moves in exact
  lockstep with the pre-existing in-memory stats.

### `src/exchange/base_client.py`
- `api_requests`/`api_latency` — instrumented in
  `BaseExchangeClient._execute_request()` (the method shared by both
  Binance and Bybit REST clients). `endpoint` label uses the un-queried
  URL path rather than the full request URL, to avoid unbounded
  cardinality from per-call query-string content (timestamps,
  signatures, order IDs). `status` label defaults to `"error"` and is
  only overwritten once a response object actually exists — connection-
  level failures (`ServerTimeoutError`, `ClientConnectionError`) never
  reach that point, so they correctly report as `"error"` rather than a
  fabricated HTTP status.

### `src/exchange/binance/websocket.py`, `src/exchange/adapters/bybit/websocket.py`
- `ws_reconnects` — incremented at all 3 existing `self._reconnect_count
  += 1` sites (Binance; Bybit public; Bybit private).
- `ws_messages_received` — incremented in `_dispatch`/`_dispatch_public`/
  `_dispatch_private`, labeled by resolved stream/topic name. Both
  exchanges' user-data (private) streams are labeled `"user_data"` rather
  than the raw listen key / topic, since Binance listen keys rotate over
  the process lifetime and would otherwise create unbounded label
  cardinality; using the same label on both exchanges also means a
  cross-exchange dashboard query gets one comparable series instead of
  exchange-specific names.

### `src/strategy/orchestrator.py`
- `candle_processing_latency` — observed around the
  `strategy.on_candle()` call in `_on_candle_closed()`. Times just the
  strategy call itself, not the candle-fetch (an in-memory cache read
  with no interesting variance) — that's the variable-cost part of
  "processing a candle close through strategy" the metric name describes.

### `src/bootstrap/app.py`
- `account_equity`/`account_daily_pnl`/`account_drawdown_pct` — set in
  `Application.record_metrics_snapshot()`, which already gathers exactly
  this data on its existing schedule (see `METRIC_SNAPSHOT_INTERVAL_SECONDS`
  in `startup.py`). Unlike the other 15 metrics, these have no discrete
  domain event to hang off — equity/PnL/drawdown drift continuously with
  every mark-price tick rather than changing at well-defined moments.
  Moved the computation to run *before* the `db.is_connected` guard that
  gates the DB-persistence half of this method, so Prometheus reporting
  keeps working even when the database is down — the same principle
  OrderManager/PositionManager already follow for their own in-memory
  state.
- `Application.__init__` now constructs `self.metrics_collector =
  MetricsCollector()` alongside the other monitoring components.

### `src/bootstrap/startup.py`
- `system_info` — set once in a new `SystemStartedEvent` subscriber
  (`.info({"version": ..., "trading_env": ...})`). `SystemStartedEvent`
  was already imported in this file for its own step-10 publish call but
  had literally never been subscribed to.
- Wired all 4 previously-dead events:
  - `SystemStartedEvent`/`SystemShutdownEvent` → the existing
    `AlertFiredEvent` → `Notifier.send()` pipeline from `[1.1.29]`
    (`INFO`/`WARNING` respectively) — shutdown.py's step-0 comment
    ("so a subscriber e.g. a notifier can still reach the network...")
    described exactly this wiring but it was never connected.
  - `OrderStatusChangedEvent` → a structured `logger.info(...)` audit
    line, not an alert — this fires on every single order including the
    normal NEW→FILLED path, so routing it through `Notifier` would be
    alert spam. Doesn't duplicate `OrderManager`'s own DB persistence,
    which already runs independently of this event.
- `app.metrics_collector.start()` added to the monitoring startup
  section, unconditional on `enable_prometheus` — the subscription
  itself is cheap, and gating it would mean flipping that flag mid-
  process starts metrics collection from a cold state instead of one
  that was tracking all along.

### Tests
- `tests/monitoring/test_metrics_collector.py` (new): all 8
  `MetricsCollector`-owned metrics, publishing each domain event through
  a real `EventBus` and asserting the exact counter/gauge delta —
  including the `CircuitBreakerState.HALF_OPEN` → `0` (not `1`) case and
  the `strategy_id=None` → `"unknown"` label fallback.
- `tests/risk/test_risk_manager_metrics.py` (new): `risk_validations`
  moves on both a passing and a rejected `validate_signal()` call, plus a
  cross-check that the Prometheus delta matches the pre-existing
  in-memory `stats["validations_passed"]` delta exactly (catches the
  metric call being added to the wrong branch).
- `tests/execution/test_order_manager.py`: added an `order_type`
  assertion to the existing `OrderSubmittedEvent` publish test.
- `tests/bootstrap/test_startup.py`: `TestSystemLifecycleEventWiring`
  (`SystemStartedEvent`/`SystemShutdownEvent` → notifier + `system_info`)
  and `TestOrderStatusChangedAuditTrail` (log line, not a notifier call);
  `_fake_app` fixture updated with `app.metrics_collector.start =
  AsyncMock()` so the existing suite's `startup()` calls don't break.

### Known limitations / explicitly not done
- Runtime verification (pytest, actual `prometheus_client`/`pydantic`/
  `aiohttp` imports) was not possible — sandbox has no network (confirmed
  via a failed `pip install` attempt this session) and those packages
  aren't present locally. All verification is `ast.parse`/`py_compile`
  across every file in `src/` and `tests/` (clean) plus manual line-by-
  line tracing of each call site against the metric's declared label
  schema in `metrics.py`. Needs Makaveli's local environment to actually
  run the new/updated test files.
- `orders_cancelled`'s `reason`-label cardinality risk (see above) is
  noted but not redesigned — changing the metric's label schema wasn't
  in scope for this pass.
- The empirical SMC confluence weights validation run remains the one
  outstanding ROADMAP item, unchanged from prior sessions — still needs
  Makaveli's local environment with network access.
- No `.git` history in this archive — standing reminder: `git push
  origin main` before re-zipping, so this doesn't silently revert again.

## [1.3.3] - LTF tier candle source: the gap `[1.3.2]` flagged and deferred

### Context
`[1.3.2]`'s scope note flagged that `MTFContext` supported a third (LTF)
timeframe tier with no candle source anywhere in the codebase — `main.py`'s
feed-timeframe wiring, `BacktestEngine`, and `configs/strategy.yaml` only
ever carried a single HTF. This closes that gap end-to-end, live and
backtest, mirroring the existing HTF wiring pattern at every layer rather
than inventing a new one.

### `src/strategy/signal_engine.py`
- `SMCSignalEngine` gained `ltf_timeframe` (constructor) and `ltf_candles`
  (`analyze()` argument). When both are present and sufficient
  (`>= swing_lookback * 2 + 2` candles, same threshold as HTF), it feeds
  `MTFContext.update()` for the LTF tier — purely additive; omitting either
  leaves the LTF tier simply unfed, which `alignment_score()` already
  handles correctly (see `[1.3.2]`'s denominator fix).
- `SMCAnalysis` now also carries `htf_timeframe`/`ltf_timeframe` through
  from the engine (see confluence.py below).

### `src/smart_money/confluence.py`
- `SMCAnalysis` gained `htf_timeframe: Timeframe | None` and
  `ltf_timeframe: Timeframe | None` fields.
- The HTF/MTF alignment scoring factor's `mtf_context.alignment_score()`
  call now passes `htf=analysis.htf_timeframe, ltf=analysis.ltf_timeframe`
  explicitly instead of relying on `MTFContext`'s `HTF_MAP`/`LTF_MAP`
  defaults. **This also fixes a currently-active latent bug in the HTF
  side of this exact call**, not just adding LTF: `[1.3.2]` added the
  explicit-override *capability* to `MTFContext.alignment_score()`/
  `is_htf_aligned()` but only wired the explicit `htf` argument through at
  the `is_htf_aligned()` hard-filter call site in `signal_engine.py` — the
  scoring-factor call here still fell through to `HTF_MAP` default lookup.
  With the default config (`timeframe: 1h`, `htf_timeframe: 4h`),
  `HTF_MAP[H1]` is `D1`, not `H4` — so this scoring factor has been
  silently scoring HTF as "unavailable" (falling back to the pre-MTFContext
  three-way check via `analysis.htf_structure`) on every default-config run
  since `[1.3.2]` shipped, even though H4 candles were being fed correctly.
  Now fixed for both tiers by the same explicit-pass-through.

### `src/main.py`
- `SMCStrategy` gained `ltf_timeframe` (constructor + property),
  `set_ltf_candles()` (mirrors `set_htf_candles()`, same
  override-vs-`candle_service` precedence), and LTF candle fetch in
  `on_candle()`.
- `build_strategy()` reads `ltf_timeframe` from `configs/strategy.yaml`
  (default `15m`; unlike `timeframe`/`htf_timeframe`, an absent/invalid
  value *disables* the LTF tier rather than falling back to a guessed
  default — an unwanted extra candle fetch has a real live-system cost
  that shouldn't be paid silently).
- `async_main()`'s market-data subscription now also adds `ltf_timeframe`
  to `feed_timeframes` alongside `htf_timeframe` — **this is the actual
  candle source fix**. Everything upstream of this could be wired
  correctly and LTF candles would still never appear in `CandleService`'s
  cache without this; `on_candle()`'s `candle_service.get_candles()` call
  would just return an empty list forever.

### `src/backtesting/engine.py`
- `run()` gained `ltf_candles`/`ltf_timeframe` parameters, with an
  `ltf_cursor` that mirrors the existing `htf_cursor` no-look-ahead logic
  exactly (O(N) monotonic advance, only LTF candles that have actually
  closed as of the current bar are ever exposed via `set_ltf_candles()`).

### `scripts/backtest.py`, `scripts/train_signal_filter.py`
- Both already fully wired HTF end-to-end (CLI arg, fetch, pass to
  strategy and `engine.run()`); added the identical `--ltf` plumbing to
  each for parity. `scripts/optimize.py` does not wire HTF at all (a
  separate, pre-existing gap outside this change's scope — not a
  regression introduced here), so it was left untouched.

### `configs/strategy.yaml`
- Added `ltf_timeframe: 15m`.

### Tests
- `tests/strategy/test_signal_engine.py`: new
  `test_ltf_candles_wired_into_mtf_context` (confirms the LTF tier
  actually reaches `MTFContext`, not just accepted-and-dropped) and
  `test_ltf_omitted_leaves_ltf_tier_absent_not_broken`.
- `tests/backtesting/test_engine.py`: new `TestLtfNoLookAhead` class,
  verifying the LTF cursor never exposes a still-forming LTF candle and
  that `set_ltf_candles()` is never called when `ltf_timeframe` is
  omitted. (No prior test in this file exercised the HTF cursor branch
  either — `AsyncMock(spec=IStrategy)` can't satisfy the `hasattr()`
  duck-typing check since `set_htf_candles`/`set_ltf_candles` aren't part
  of `IStrategy` — so a plain fake-strategy double was used instead.)

### Verification
`ast.parse` clean across all 294 files in `src/`, `scripts/`, and `tests/`.
The cursor-advance arithmetic was independently verified against a
minimal standalone simulation of the exact loop logic outside the
codebase. `pytest` remains unavailable in this sandbox (no network to
install `pydantic`/`numpy`, same limitation noted in `[1.3.2]`) — run the
two touched test files locally before merge.

## [1.3.2] - `MTFContext` wired into confluence scoring and a new HTF filter

### Context
Follow-up audit surfaced another "built but unwired" gap:
`src/strategy/multi_timeframe.py`'s `MTFContext` (structure tracking across
HTF/execution/LTF timeframes, `alignment_score()`, `is_htf_aligned()`) had
zero callers anywhere in `src/` and no test file. The live HTF-bias check
in `SMCSignalEngine.analyze()` instead did a much simpler inline
`detect_market_structure()` call on the HTF candles alone — a three-way
UNKNOWN/aligned/RANGING check with no execution-TF or LTF awareness.
`is_higher_timeframe_bullish/bearish()` in `market_structure.py` were
unused thin wrappers around the same underlying function.

This affects live signal behavior (scoring and now a new hard filter), so
implementation was held for a decision: replace the scoring factor,
add a hard pre-trade filter, both, or leave it orphaned. Decision: both.

### Bugs fixed in `MTFContext` itself while wiring it in
- `alignment_score()` divided aligned-timeframe count by the *theoretical*
  full timeframe set (`len(tfs_to_check)`) rather than the timeframes it
  actually had data for (`available`, computed but unused). With the LTF
  tier never fed (see Scope note below), this silently capped alignment at
  ~0.67 even with perfect execution-TF/HTF agreement — the classic
  numerical-divergence-in-duplicate-logic pattern, except here the
  divergence was between a variable's stated purpose and its actual use in
  the same function. Fixed: denominator is now the count of timeframes
  with data.
- `alignment_score()`/`is_htf_aligned()` derived the HTF to check from the
  fixed `HTF_MAP` default table, ignoring that `SMCSignalEngine`'s HTF is
  independently configurable via `htf_timeframe`
  (`configs/strategy.yaml`). If configured to something other than
  `HTF_MAP`'s default for a given execution timeframe, these methods would
  look up a timeframe key that was never `update()`d and silently score as
  unavailable. Fixed: both now accept an explicit `htf` override, which
  `SMCSignalEngine` passes.
- `is_htf_aligned()` treated "no HTF data yet" the same as "HTF opposes the
  trade" (both fell through to `False`), which would reject every signal
  in any system where HTF data isn't wired up. Fixed: only rejects when
  HTF data has actually been fed; otherwise allows the trade through
  unchanged (the graded scoring factor still reflects reduced conviction).

### `src/smart_money/confluence.py`
- `SMCAnalysis` gained `mtf_context: Any = None` — typed as `Any` and
  duck-typed rather than importing `MTFContext` by name, since
  `strategy/` is an outer layer than `smart_money/` per
  `ARCHITECTURE.md` and this module must not depend on it.
- The HTF-alignment scoring factor (5pt budget) now uses
  `mtf_context.alignment_score()` when present — a graded 0–1 score across
  whichever of {execution TF, HTF, LTF} have data — instead of the old
  three-way UNKNOWN/aligned/RANGING check. Falls back to the old logic
  when `mtf_context` is `None` (existing test fixtures construct
  `SMCAnalysis` directly without one), so this is additive, not breaking.

### `src/strategy/signal_engine.py`
- `SMCSignalEngine` now owns a persistent `MTFContext`, fed on every
  `analyze()` call for both the execution timeframe and (when configured
  and available) the HTF.
- New hard filter after confluence scoring clears `min_score`: rejects the
  signal if `is_htf_aligned()` is `False` for the signal's direction. This
  is a binary confirmation gate layered on top of the graded scoring
  factor above, not a replacement for it — a signal can clear `min_score`
  on other factors while the HTF factor itself is only partially credited;
  this catches setups that are clearly opposed to HTF trend regardless of
  how the rest of the score landed.

### Scope note — LTF tier not wired
`MTFContext` supports a third (lower) timeframe tier, but nothing in the
codebase fetches LTF candles — `main.py`'s feed-timeframe wiring,
`BacktestEngine`, and `configs/strategy.yaml` only carry a single HTF, not
an LTF. Wiring an LTF candle source is a separate, larger change (new
config field, new feed-timeframe threading through both live and
backtest paths, no-look-ahead handling mirroring the existing HTF cursor
logic in `backtesting/engine.py`) that wasn't part of the "both" decision
this pass addressed. `alignment_score()`/`is_htf_aligned()` both already
degrade correctly when LTF data is absent (see bug fixes above), so this
is purely a "not yet built" gap, not a latent bug — flagged for a future
decision rather than assumed.

### Verification
`py_compile` clean across the full repo. `pytest` is unavailable in this
sandbox (no network to install `pydantic`/`numpy`); the existing
`tests/strategy/test_confluence.py` / `test_signal_engine.py` suites and a
new test class for the two `MTFContext` bug fixes should be run locally
before merge.

## [1.3.1] - Funding-rate accrual: wired the actual data source

### Context
Post-merge audit (`py_compile` sweep across all 293 files clean, zero
TODO/FIXME/XXX, `ROADMAP.md` fully checked off) surfaced a "built but
unwired" gap the checked-off ROADMAP item didn't catch: the near-term item
"Populate funding-rate accrual in the backtest engine from historical
funding-rate data" *is* fully implemented at the simulator level
(`backtesting/simulator.py`'s `FundingRate` dataclass and
`_apply_funding_rates()`, `BacktestConfig.funding_rates`) — but
`rest.get_funding_rate_history()`, the REST method that's supposed to
source that data, had **zero callers anywhere**. All three real call sites
that construct a `BacktestConfig` (`scripts/backtest.py`,
`scripts/optimize.py`, `scripts/train_signal_filter.py`) left
`funding_rates` at its `None` default, so every real backtest, weight-tuning
validation run, and ML-training data generation run has been silently
modeling zero funding cost since the feature shipped.

### `src/exchange/binance/parser.py`
- New `parse_funding_rate_history_record()`: parses a single record from
  the *history* endpoint (`fundingRate`/`fundingTime` keys), distinct from
  the existing `parse_funding_rate()` which parses the *live/mark-price*
  stream shape (`lastFundingRate`/`nextFundingTime`) — the two Binance
  responses use different field names for conceptually similar data.
  `next_funding_time` is derived as settlement + 8h (Binance's fixed
  interval) since the history endpoint doesn't report it.

### `src/exchange/binance/rest.py`
- New `fetch_funding_rate_history_parsed(symbol, start_time, end_time) ->
  list[FundingRate]`: paginates past `get_funding_rate_history()`'s
  1000-record cap (≈333 days per page at Binance's 8h settlement interval)
  and returns records parsed and sorted ascending — the exact shape
  `BacktestSimulator._apply_funding_rates()` expects.

### `scripts/backtest.py`, `scripts/optimize.py`, `scripts/train_signal_filter.py`
- All three now fetch funding-rate history over the same date window as
  their candle fetch and pass it to `BacktestConfig(funding_rates=...)`.
  `backtest.py` and `train_signal_filter.py` gained a `--no-funding` flag
  (default on) to opt out. In `optimize.py`, funding accrual is wired into
  the OOS **global-weights validation pass** only (`_backtest_with_weights`),
  not the per-trial Optuna search loop — threading it through every trial
  would slow tuning for a realism factor that isn't what's being optimized;
  the final honest comparison is where it matters.
- A failed funding-rate fetch logs a warning and falls back to `None`
  (no accrual) rather than aborting the run — consistent with treating
  funding realism as an enhancement to an otherwise-valid backtest, not a
  hard requirement.

### `tests/exchange/test_binance_rest.py`
- `TestFetchFundingRateHistoryParsed`: record parsing + ascending sort,
  pagination past the 1000-record cap (verifies the cursor advances past
  the last record's timestamp rather than re-requesting it), empty-result
  handling.

### Verification
`py_compile` clean across the full repo (293 → 294 files). `pytest` is
unavailable in this sandbox; the new test class and the three edited
scripts should be run locally before merge (see "On the horizon").

## [1.3.0] - `information_ratio` benchmark data source implemented

### Context
Last item on the "on the horizon" list from prior sessions:
`information_ratio()` in `analytics/sharpe.py` was fully implemented and
tested in isolation but had no caller anywhere — it needs a
`benchmark_returns` series the same shape as the strategy's own returns,
and nothing in the codebase produced one. Unlike the empirical
confluence-weight tuning (still blocked — genuinely needs Allan's local
environment with live multi-symbol market data), this gap turned out to
be closeable in-sandbox: `BacktestEngine.run()` already receives the
traded symbol's own `candles: list[OHLCV]` as a parameter, which is
sufficient to build a same-asset buy-and-hold benchmark without any new
external data source.

### `src/analytics/performance.py`
- New `benchmark_returns_for_trades(trades, benchmark_candles) -> np.ndarray`:
  for each trade, looks up the benchmark's close price at-or-before
  `opened_at` and at-or-before `closed_at` (via `bisect` on a
  timestamp-sorted candle list) and returns the pct change between them
  — "what would holding the benchmark instead, over this exact trade's
  holding window, have returned." Returns a series aligned 1:1 and in
  the same order as `calculate_performance()`'s own per-trade strategy
  returns. A trade whose window falls outside the benchmark series
  clamps to the nearest available candle rather than raising — a
  partial benchmark series is still useful for the trades it does
  cover. Raises `ValueError` only if `benchmark_candles` is empty.
- `calculate_performance()` gained an optional `benchmark_candles:
  list[OHLCV] | None = None` parameter. When provided, computes the
  aligned benchmark series and calls `analytics/sharpe.py`'s
  `information_ratio()`, populating the new `PerformanceReport.
  information_ratio` field.
- `PerformanceReport.information_ratio: float | None = None` —
  deliberately `None`, not `0.0`, when not computed. Sharpe/Sortino/
  Calmar are always computable from trades alone so `0.0` is a
  meaningful default for them; information_ratio is only meaningful
  with a benchmark, and conflating "no benchmark supplied" with "ratio
  computed as exactly zero" would be the same silent-failure shape this
  project treats as highest severity elsewhere. `to_dict()` updated to
  match (`None` passes through, not `round(None, 4)`).

### `src/backtesting/engine.py`
- New `BacktestConfig.benchmark_buy_and_hold: bool = False` — opt-in,
  matching this project's established convention for optional
  filters/features (see `configs/strategy.yaml`'s opt-in filters).
  Off by default because "strategy beat buy-and-hold" is one lens on a
  backtest result, not the only one a caller may want applied
  automatically. When `True`, `BacktestEngine.run()` passes its own
  `candles` argument through to `calculate_performance()` as the
  benchmark, giving a same-asset buy-and-hold comparison for free with
  no new data source.
- A benchmark against a *different* asset (e.g. BTC as a market-relative
  benchmark for an altcoin strategy) is still possible by calling
  `calculate_performance(trades, benchmark_candles=other_symbol_candles)`
  directly — `benchmark_returns_for_trades()` doesn't assume same-asset,
  that's just what `BacktestConfig.benchmark_buy_and_hold` wires up by
  default for convenience.

### `src/backtesting/reports.py`
- `print_backtest_summary()`'s metrics table now includes an
  "Information Ratio" row, conditionally (only when `p.information_ratio
  is not None`) — otherwise this newly-computed metric would be silently
  absent from the one place a human actually looks at backtest results,
  even when a benchmark was supplied and the value was populated.

### `tests/unit/test_analytics.py`
- `TestInformationRatioWiring`: empty-benchmark `ValueError`,
  `information_ratio is None` when no benchmark supplied,
  alignment-by-holding-window correctness (hand-verified against
  externally computed expected values), and an end-to-end
  `calculate_performance(benchmark_candles=...)` case where a flat
  benchmark against clearly-profitable trades produces a positive
  `information_ratio`.

### Verification
- `py_compile` sweep: clean.
- `benchmark_returns_for_trades()`'s alignment logic independently
  re-verified via a standalone script (reimplementing the same bisect
  logic against hand-computed expected values across three cases:
  normal alignment, a trade predating the entire benchmark series, and
  an empty benchmark) before writing the test above, per this project's
  standing no-`pytest`/no-`pydantic` sandbox constraint.
- **Run `tests/unit/test_analytics.py` locally before merging**, per
  standing policy.

## [1.2.2] - `validate_candle_limit()` wired into Binance's `get_candles`

### Context
Audit pass following the v1.2.1 changelog/fvg_at_price cleanup: of the 14
functions in `core/validation.py`, 10 had zero production callers despite
full unit-test coverage in isolation — a materially bigger version of the
"validate_risk_reward/validate_percentage redundant with risk_manager.py"
gap flagged in a prior session's notes. Traced each:
- `validate_price`/`validate_quantity`/`validate_risk_reward`/
  `validate_percentage`: genuinely redundant. `domain/order.py`'s pydantic
  field validators and `risk_manager.py`'s inline R:R gating already
  enforce the same invariants at better call sites (construction time /
  the actual gating decision). Confirmed dead, left as-is — no fix
  needed, nothing to wire.
- `validate_candle_limit`: different shape of gap. Not redundant with
  anything — `exchange/binance/rest.py::get_candles` silently clamped
  over-limit requests (`min(limit, 1500)`) instead of raising, and didn't
  check the lower bound at all (`limit=0` would have been sent straight
  to Binance's API as a malformed request instead of failing locally
  with a clear error). A misconfigured `warmup_candles` or
  `resync_candles` value would have silently returned fewer candles than
  requested rather than surfacing the problem — the same "silent
  failure" shape this project treats as highest-severity elsewhere
  (Bybit `execType` bug, missing `SignalGeneratedEvent.metadata`).
  Surfaced as a decision (clamp vs. raise vs. clamp-with-warning) since
  an existing test asserted the old clamp behavior; decision was raise.

### `src/exchange/binance/rest.py`
- `get_candles()` now calls `validate_candle_limit(limit)` before
  building the request, replacing the inline `min(limit, 1500)` clamp.
  Raises `ValidationError` before any network call for `limit < 1` or
  `limit > BINANCE_MAX_CANDLES` (1500), instead of silently truncating.
- Checked every call site (`market_data/candles.py`, `strategy/
  orchestrator.py`, `main.py`, `scripts/backtest.py`, `scripts/
  optimize.py`, `scripts/export_data.py`, `scripts/train_signal_filter.py`,
  paper trading delegate): none pass a literal or config-derived limit
  above 1500 today (`resync_candles` defaults to 20, `warmup_candles` to
  500). The one call site reading a config value
  (`CandleBuffer._resync_after_reconnect`, via `self._resync_candles`)
  already wraps the call in `except Exception`, so a future
  misconfiguration surfaces as a logged, visible error on reconnect
  rather than a crash or a silent short-fetch.

### `tests/exchange/test_binance_rest.py`
- Replaced `test_get_candles_caps_limit_at_1500` (asserted the old clamp
  behavior) with `test_get_candles_raises_when_limit_exceeds_exchange_maximum`
  (asserts `ValidationError`, and that no request was even sent),
  `test_get_candles_raises_for_non_positive_limit`, and
  `test_get_candles_passes_through_a_valid_limit` (boundary case at
  exactly 1500).

### Verification
- `py_compile` sweep: clean.
- Traced every `get_candles()` call site in `src/` and `scripts/` by hand
  to confirm none regress under the new raise-on-violation behavior.
- **Run `tests/exchange/test_binance_rest.py` locally before merging**,
  per standing policy.

## [1.2.1] - `fvg_at_price()` finally has a caller

### Context
`fvg_at_price()` in `smart_money/fair_value_gap.py` survived the v1.2.0
"Wired FVG-aware exit management" pass with its title on the tin but zero
actual callers — that pass wired `analysis.open_fvgs` end-to-end through
`confluence.py` → event metadata → `execution_engine.py`, but both real
call sites turned out to need more than plain containment
(`confluence.py` also needs "near but not yet touched" proximity scoring;
`execution_engine.py`'s exit-management loop only has serialized
`(bottom, top)` Decimal tuples by the time a position is open, not live
`FairValueGap` objects, so `fvg_at_price()`'s `list[FairValueGap]` typing
doesn't fit there without a larger refactor). Re-examined whether either
site could route through it anyway rather than leaving it as
intentionally-unwired utility code.

### `src/smart_money/confluence.py`
- The FVG-scoring loop's containment check (`fvg.contains_price(...)`,
  called once per open FVG per candle) is now computed once up front via
  `contained_fvgs = fvg_at_price(analysis.open_fvgs, analysis.current_price)`,
  and each iteration checks `fvg in contained_fvgs` instead of
  re-deriving containment itself. The "near" (not-yet-touched but close)
  branch is unchanged — that's a proximity query `fvg_at_price()` doesn't
  answer, so it still needs its own per-FVG check.
- Purely a dedup of existing logic onto the tested utility function —
  `analysis.open_fvgs` is already `get_open_fvgs()`-filtered
  (`strategy/signal_engine.py`), so `fvg_at_price()`'s own `is_filled`
  check is a no-op here, and containment membership is provably identical
  to the direct `contains_price()` call it replaces (verified via
  standalone equivalence check against multiple zones/prices, matching
  the new test below).
- New test: `TestFairValueGapScoring::test_containment_check_uses_fvg_at_price_and_picks_correct_zone`
  in `tests/strategy/test_confluence.py` — two open FVGs (one containing
  `current_price`, one merely nearby), asserts the containing zone gets
  full containment credit (`proximity_factor=1.0`) rather than falling
  back to the reduced "near" factor a mis-wired containment check would
  produce.

### `src/strategy/exit.py` / `src/execution/execution_engine.py`
- Not touched. `fvg_target_zones`/`fvg_reversal_zones` there operate on
  the `(bottom, top)` Decimal-tuple snapshot taken at entry (see v1.2.0
  entry below for why), which is a different type than
  `fvg_at_price()` accepts. Forcing a fit would mean either widening
  `fvg_at_price()`'s signature to accept raw tuples (weakens its type
  safety for the one call site that does have live `FairValueGap`
  objects) or keeping live `FairValueGap` references in `position.tags`
  instead of serialized tuples (reintroduces the "no live SMCAnalysis
  reference once a position is open" problem v1.2.0 explicitly designed
  around). Left as-is; `confluence.py` was the only site where routing
  through the shared utility was a strict improvement with no tradeoff.

### Verification
- `py_compile` sweep: all files clean.
- Standalone equivalence check (no `pytest`/`pydantic` in this sandbox,
  per standing constraint): confirmed `fvg_at_price()`-based membership
  matches direct `contains_price()` calls across contained, near-only,
  and no-match price scenarios before writing the regression test above.
- **Run `tests/strategy/test_confluence.py` locally before merging**, per
  standing policy.

## [1.2.0] - Wired FVG-aware exit management (`fvg_at_price`)

### Context
Requested audit ("implement what's missing or remaining") on top of a
codebase already in excellent shape: `ast`/`py_compile` sweep across all
293 files clean, zero TODO/FIXME/XXX/stub markers outside the two
legitimate ABC `NotImplementedError` bodies, `ROADMAP.md` fully checked
off, clean git history. The one carried-forward open item from the prior
session's notes — `fvg_at_price()` in `smart_money/fair_value_gap.py` had
a correct implementation but zero callers anywhere, so open FVGs had no
effect on exit management — required a strategy-behavior decision before
implementing, not just a wiring fix. Asked; decision was **both**: open
FVGs should act as partial-take-profit targets (same-direction zones) and
as reversal-risk warnings that tighten the stop (opposing-direction
zones).

### `src/smart_money/confluence.py`
- `score_confluence()`'s metadata now includes the full open-FVG snapshot
  (`open_fvgs`: list of `{type, top, bottom}`, all serialized as strings)
  from `analysis.open_fvgs`, not just the single best-aligned FVG already
  scored into `has_fvg`. This is the only channel this data has to reach
  exit management later — `ExecutionEngine` holds no live `SMCAnalysis`
  reference once a position is open (same constraint `entry_atr` already
  works around).

### `src/strategy/exit.py`
- `ExitPlan` gained `fvg_target_partial_tps: list[tuple[Decimal, Decimal,
  int]]` (price, fraction, zone_index) and `fvg_reversal_risk: bool`.
- `evaluate_exit_actions()` gained `fvg_target_zones`, `fvg_reversal_zones`
  (bottom, top) `Decimal` tuples, `fvg_target_taken` (indices already
  triggered, mirroring how `partial_tp_levels`/`triggered_partial_tps`
  avoids re-submitting the same ladder level), and
  `fvg_partial_tp_fraction` (default `0.25`). This function has no
  position-direction-aware FVG-type knowledge of its own — callers must
  pre-classify same-direction ("target") vs. opposing ("reversal") zones.
  - Target zones: price entering an untaken zone queues a partial TP at
    the zone's midpoint.
  - Reversal zones: price entering one sets `fvg_reversal_risk = True`
    and computes a candidate stop at the zone's protective edge (the
    boundary further from the trade's favorable direction). This
    candidate only ever *tightens* `new_stop_loss` — it's compared against
    whatever the regular ATR-trailing calculation already produced in the
    same pass and only wins if it's the more protective of the two, so
    this can't loosen a stop that trailing/breakeven logic already
    improved.
- New tests: `TestFvgTargetPartialTp`, `TestFvgReversalRisk` in
  `tests/strategy/test_exit.py`.

### `src/core/events.py`
- **Bug found while wiring the above**: `SignalGeneratedEvent` never
  carried `metadata` at all. `ExecutionEngine._on_signal` reconstructs its
  own `Signal` from the event's fields — without this, `signal.metadata`
  would have been an empty dict on that path regardless of what
  `confluence.py` put in the original `Signal`, so the FVG snapshot above
  would have silently gone nowhere in the live system. This is the same
  failure shape `atr` and `signal_id` were added to this event to fix in
  a prior session. Added `open_fvgs: list[dict[str, str]]` to
  `SignalGeneratedEvent`.

### `src/strategy/orchestrator.py`
- `SignalGeneratedEvent(...)` publish site now forwards
  `open_fvgs=signal.metadata.get("open_fvgs", [])`.
- New regression test: `test_open_fvgs_metadata_is_carried_onto_the_event`
  in `tests/strategy/test_orchestrator.py`, mirroring the existing
  `signal_id` regression test.

### `src/execution/execution_engine.py`
- `_on_signal`'s `Signal` reconstruction now passes
  `metadata={"open_fvgs": event.open_fvgs} if event.open_fvgs else {}`.
- `_on_entry_filled`: classifies the signal's `open_fvgs` by position
  direction (bullish FVG + LONG, or bearish FVG + SHORT, is
  "same-direction" → `fvg_target_zones`; the opposite pairing →
  `fvg_reversal_zones`) and snapshots both into `position.tags` at entry —
  a static snapshot, same tradeoff `entry_atr` already makes, since
  `ExecutionEngine` has no live signal-engine reference to refresh it
  against newly-forming FVGs later in the trade's life.
- `_manage_position_exit`: reads the zone tags back each tick, tracks
  which target-zone indices have already fired via a
  `fvg_target_taken` list in `position.tags` (same pattern as
  `position.partial_tp_taken`), submits partial TPs through the existing
  `_submit_partial_tp` path, and logs an info line when
  `fvg_reversal_risk` fires for operational visibility. The resulting
  `new_stop_loss` flows through the existing "does this improve the
  current stop" gate unchanged — no new code path needed there since
  `evaluate_exit_actions()` already returns the single best candidate.
- New regression test:
  `test_open_fvgs_are_classified_into_target_and_reversal_tags` in
  `tests/execution/test_execution_engine.py` — full `_on_signal` →
  `_on_fill` path, asserting the resulting position's
  `fvg_target_zones`/`fvg_reversal_zones` tags.

### Verification
- `py_compile` sweep: all 293 files (up to whatever this pass added),
  zero syntax errors.
- No `pytest`/`pydantic` in this sandbox (no network access, as in every
  prior session) — all logic traced manually with standalone Python
  reproductions of the zone-classification and target/reversal-trigger
  branches, matching the new test assertions. **Run the real suite
  locally before merging**, per standing policy.

### Known limitations / explicitly not done
- FVG zones are a static entry-time snapshot, not refreshed as new FVGs
  form during the life of a trade — consistent with `entry_atr`'s existing
  tradeoff, but worth knowing if it's ever revisited.
- The empirical SMC confluence weights validation run (flagged in prior
  sessions) is still outstanding and still needs a local environment with
  network access.

## [1.1.29] - Wired `ComponentHealthChangedEvent` to alerting

### Context
Full audit pass (`ast.parse` sweep — 293 files, zero syntax errors; grep for
TODO/FIXME/XXX/stub markers — zero hits outside the two legitimate ABC
`raise NotImplementedError` bodies in `backtesting/slippage.py` and
`commission.py`). `ROADMAP.md` has all 16 tracked items checked off (the
last, SMC confluence weight tuning, closed last session modulo the
empirical run, which still needs Makaveli's local environment with network
access). Every test suite previously flagged as recurrently-disappearing
(`tests/monitoring/*`, `tests/notifications/*`, ML tests) is present and
substantive in this archive — `tests/monitoring/` alone has
`test_alerts.py`, `test_dashboard.py`, `test_health.py`, `test_metrics.py`,
`test_telemetry.py` (848 lines combined), `tests/notifications/` has all
five channel suites, and `tests/unit/test_watchdog.py` /
`test_ml_features.py` / `test_signal_filter.py` / `test_trainer.py` cover
the rest. **This is also the first session where a delivered archive's
`.git` history actually carried forward from the prior session intact**
(one commit, `c634c96`, clean working tree) — the standing persistence
issue may finally be resolved on Makaveli's end; flagging so it can be
confirmed rather than re-asserting the problem by default.

With the roadmap clear, went looking for the class of bug this project has
repeatedly found in past sessions: an event with a real producer but zero
subscribers. Checked total reference counts (definition + producer +
consumer, across `src/`, `scripts/`, `tests/`) for all 22 `BaseEvent`
subclasses in `core/events.py`. Every event cleared this except
`ComponentHealthChangedEvent` (4 total references — the class definition,
one comment, and the two-line publish site in `bootstrap/startup.py`).

### `src/bootstrap/startup.py`
- The health-check scheduled task (section 8c) published
  `ComponentHealthChangedEvent` on every database/exchange health-status
  transition, but nothing in `src/` ever subscribed to it — a DB
  connection drop or exchange-connectivity failure would flip
  `health_checker`'s internal status, publish the event, and the event
  would evaporate on the bus. No alert, no notification, nobody paged —
  silently worse coverage than the trading-logic alert rules
  (`circuit_breaker_open`, `daily_loss_high`, `drawdown_warning`) sitting
  right next to it in the same function.
- Added `_on_component_health_changed`, subscribed to
  `ComponentHealthChangedEvent`, reusing the `AlertFiredEvent` →
  `Notifier.send()` pipeline wired in step 1 of the same section (so this
  is additive to existing wiring, not a parallel notification path).
  Severity mapping: `unhealthy` → `CRITICAL`, `degraded` → `WARNING`,
  `healthy` (i.e. a recovery transition) → `INFO`; anything else defaults
  to `WARNING`. Message format: `"{component}: {previous_status} →
  {new_status}"`, with `" ({detail})"` appended only when `detail` is
  non-empty. No separate cooldown needed — the producer is already
  edge-triggered (only fires on an actual status change), so this can't
  spam on its own.

### Tests
- `tests/bootstrap/test_startup.py::TestComponentHealthEventWiring` (new):
  publishing `ComponentHealthChangedEvent` end-to-end through `startup()`'s
  real wiring and asserting on `app.notifier.send`'s exact call args —
  unhealthy→CRITICAL, degraded→WARNING, a recovery transition→INFO, and
  that a blank `detail` doesn't leave a stray `" ()"` in the message.

### Known limitations / explicitly not done
- The empirical SMC confluence weights validation run (see last session)
  is still outstanding — needs Makaveli's local environment.
- Did not re-litigate git persistence beyond flagging the good news above;
  per standing policy, no test suites were found missing, so there was
  nothing to re-write a third time.

## [1.1.28] - Made SMC confluence scoring weights tunable (ROADMAP.md)

### Context
Full audit pass on request ("implement what's remaining on roadmap").
Codebase is in excellent shape overall: zero syntax errors, no TODOs/
stubs/dead code across `src/` and `scripts/`, and every previously-tracked
missing test suite (`test_watchdog.py`, ML tests, notification tests) turns
out to already exist — they'd just moved to `tests/unit/` rather than the
`tests/monitoring/`/`tests/machine_learning/` paths earlier sessions'
CHANGELOG entries expected. `ROADMAP.md` had exactly one open item:
empirically validating/tuning the SMC confluence scoring weights against
multiple symbols/timeframes.

**That item's own note (added last session) said it was a pure operational
task, not a code gap, since `scripts/optimize.py` was "implemented." That
turned out to be wrong on inspection:** the script only ever tuned
`min_score`. The 8 confluence factor budgets (market structure, order
block, FVG, liquidity sweep, premium/discount, session, volume, HTF
alignment) were hardcoded literals scattered through
`score_confluence()`, with no parameter anywhere to change them — and
`configs/strategy.yaml` had a `scoring_weights` block that *looked* like
it controlled this but was never read by any code path. Editing it did
nothing. This pass closes that actual gap; the live-data empirical run
itself is still operational and still needs Makaveli's environment (see
"Known limitations" below).

Also found and fixed a real latent bug while rewriting `scripts/
optimize.py`: `strategy_factory` hardcoded `symbols=["BTCUSDT"],
timeframes=[Timeframe.H1]` regardless of the `--symbol`/`--timeframe` CLI
args. `SMCStrategy.on_candle()` looks its signal engine up by an exact
`f"{symbol}_{timeframe.value}"` key, so running e.g. `--symbol ETHUSDT`
silently produced zero signals on every single trial — the optimizer was
scoring an empty backtest the whole time for any symbol/timeframe other
than the hardcoded default.

### `src/smart_money/confluence.py`
- New `ConfluenceWeights` frozen dataclass: the 8 factor point budgets,
  defaulting to the original hardcoded values (20/20/15/15/10/10/5/5,
  summing to 100). `.normalized()` rescales any positive combination to
  sum to 100, so `min_score`'s gate and `classify_strength()`'s
  thresholds stay meaningful regardless of what a caller (e.g. an Optuna
  trial) passes in. `.total`, `.as_dict()`, and `__post_init__`
  validation (rejects negative values or an all-zero/non-positive sum).
- `DEFAULT_CONFLUENCE_WEIGHTS` module constant.
- `score_confluence()` gained a `weights: ConfluenceWeights | None = None`
  param. All 8 sections rewritten to read their budget from
  `weights.<field>` instead of a bare literal — purely-multiplicative
  sections (order block, FVG, session) substitute the literal directly;
  tiered/additive sections (market structure, liquidity sweep, premium/
  discount, volume, HTF alignment) use a local `<factor>_scale =
  weights.<field> / <original_budget>` and multiply every literal in that
  section by it. Every substitution is scale=1.0 (or, equivalently,
  `weights.field == original literal`) for `DEFAULT_CONFLUENCE_WEIGHTS`,
  so this is a byte-for-byte-identical no-op unless a caller explicitly
  passes custom weights — confirmed by the existing ~40 tests in
  `test_confluence.py` needing zero changes, plus new regression tests
  (below).

### `src/strategy/signal_engine.py`
- `SMCSignalEngine.__init__` gained `confluence_weights:
  ConfluenceWeights | None = None`, stored and forwarded to
  `score_confluence(..., weights=self.confluence_weights)` on every
  `analyze()` call.

### `src/main.py`
- `SMCStrategy.__init__` gained `confluence_weights`, forwarded to every
  `SMCSignalEngine` it constructs in `initialize()`.
- `build_strategy()` now reads `configs/strategy.yaml`'s `scoring_weights`
  block via new `_load_confluence_weights()` and passes it through. Fails
  open (logs a warning, returns `None` → `score_confluence()`'s own
  default) on invalid config instead of crashing startup, same pattern as
  `_build_signal_filter()`. Config values are read as-is regardless of
  scale (the file's existing 0–1 fractions work fine — `normalized()`
  only cares about relative proportions between the 8 values, not their
  absolute scale) — no unit conversion needed between the YAML and
  `ConfluenceWeights`' point-budget-flavored field defaults.

### `configs/strategy.yaml`
- Updated the stale comment above `scoring_weights` (previously read "that
  budget is fixed and tested" — no longer true) and added one directly
  above the block documenting that it's now live-wired, plus a pointer to
  `scripts/optimize.py --tune-weights` for finding alternatives.

### `scripts/optimize.py` (rewritten)
- `--symbols`/`--timeframes` (comma-separated) for multi-combination
  sweeps, alongside the original singular `--symbol`/`--timeframe` (fully
  backward compatible — omitting the plural flags reproduces the exact
  original single-combo CLI contract).
- `--tune-weights`: adds all 8 `ConfluenceWeights` dimensions to the
  Optuna search space alongside `min_score` (bounds ~0.2x–2x each
  factor's original budget — wide enough to meaningfully shift relative
  emphasis, narrow enough not to waste most trials on implausible
  combinations).
- `make_strategy_factory(symbol, timeframe, tune_weights)`: per-combination
  factory closure (fixes the hardcoded-BTCUSDT/H1 bug described above).
- New `_run_global_weights_validation()`: after all combinations are
  optimized, computes the mean of every combination's individually-tuned
  weights (the "one weight set for everything" candidate) and re-backtests
  each combination's own OOS candles with that shared set instead of its
  individually-tuned one, reporting the gap per combination. This is the
  actual empirical check the ROADMAP item asks for — small consistent
  gaps mean the shared default is fine as shipped; large gaps on specific
  symbols/timeframes mean that combination needs its own
  `scoring_weights` override in a per-deployment config. Written to
  `global_weights_validation.json` alongside the existing per-combination
  result files.
- Explicit closing note in the script's own output: this is candidate
  evidence for a human review before any weights become the new shipped
  default, not an automatic config change, and only as good as whatever
  history `--days` pulls from the account it's run against.

### Tests
- `tests/strategy/test_confluence.py`: new `TestConfluenceWeights` (the
  dataclass in isolation — defaults sum to 100, uniform scaling normalizes
  back to the same defaults, validation, `as_dict()` round-trip) and
  `TestScoreConfluenceWithCustomWeights` (explicit-vs-implicit default
  parity, zeroing a factor removes its contribution, score stays in
  [0, 100] for weights that don't sum to 100, custom weights shift a
  component's reported `weight` fraction).
- `tests/strategy/test_signal_engine.py`: new
  `TestConfluenceWeightsPassthrough` — confirms `SMCSignalEngine` actually
  forwards `confluence_weights` into `score_confluence()` on `analyze()`
  (monkeypatch spy), not just stores it unused.
- `tests/unit/test_main.py` (new file): `_load_confluence_weights()` config
  parsing (empty/`None` → `None`, full override, partial override keeps
  remaining defaults, invalid value fails open, fractional config values
  normalize identically to point-budget equivalents) and
  `SMCStrategy`→`SMCSignalEngine` weights passthrough via `initialize()`.
  Scope note left in the file: `src/main.py` otherwise has no dedicated
  test module — a separate, pre-existing gap this pass doesn't attempt to
  close in full.

### Known limitations / explicitly not done
- **The actual empirical validation run is still outstanding.** Everything
  needed to run it now exists (`scripts/optimize.py --tune-weights
  --symbols ... --timeframes ...`), but this sandbox has no network access
  to pull real market data, so nobody has actually run it yet, and the
  shipped `ConfluenceWeights` defaults are unchanged (still the original
  reasoned-not-backtested 20/20/15/15/10/10/5/5). Run it locally, review
  `global_weights_validation.json`, and decide whether to update
  `configs/strategy.yaml`'s `scoring_weights` — this pass deliberately
  does not change that default itself.
- **Git persistence: still no `.git` directory in this archive.** Per
  standing policy this session did not re-litigate that (no test suites
  were found missing that would trigger the "don't rewrite a third time"
  rule), but it's unresolved. Recommend confirming `git log --all` shows
  the expected history for files that have disappeared before, and that
  `git push origin main` is actually succeeding, before the next session.
- `src/main.py` test coverage remains thin outside of the
  `confluence_weights` wiring added here (see test-file scope note above)
  — flagging as a candidate for a future pass, not fixed now.

## [1.1.27] - Wired `filter_by_spread` into the live signal path

### Context
Completes the third merge pass that was left mid-flight: `MarketDataCache`
had a `best_bid_ask` cache added but no writer, no reader, and
`filters.py::filter_by_spread` (fully implemented and unit-tested since an
earlier session) had zero callers anywhere in the strategy pipeline —
`OrderBookUpdatedEvent` was being published per-tick with nowhere for the
orchestrator to look the quote up synchronously.

### `src/market_data/cache.py`
- `MarketDataCache.put_best_bid_ask()` / `get_best_bid_ask()`: TTL-backed
  top-of-book store, same pattern as the existing ticker/mark-price caches.
  Added to `MarketDataCache.stats`.

### `src/market_data/feeds.py`
- `_make_book_ticker_handler`'s `_on_book_ticker` now writes into the cache
  (`self._cache.put_best_bid_ask(...)`) immediately before publishing
  `OrderBookUpdatedEvent`, so any synchronous consumer (not just event-bus
  subscribers) can read the live spread.

### `src/strategy/orchestrator.py`
- New opt-in constructor params `market_data_cache` and `max_spread_pct`
  (0.0 default — disabled), same pattern as `min_atr_pct`/
  `rsi_exhaustion_filter`/`macd_trend_filter`. When enabled,
  `_on_candle_closed` looks up the symbol's cached quote and calls
  `filter_by_spread` before publishing `SignalGeneratedEvent`; a missing
  cached quote is a no-op (not a rejection), so this stays safe even before
  the book-ticker stream has delivered a first quote for a symbol.
  Added `signals_filtered_spread` to `.stats`.

### `src/main.py` / `configs/strategy.yaml`
- `_load_orchestrator_filter_config()` now also reads `max_spread_pct`
  from `strategy.yaml` (added there, default `0.0`, documented inline).
  `async_main()` passes `market_data_cache=app.cache` and the loaded
  `max_spread_pct` into `StrategyOrchestrator`.

### Tests
- `tests/integration/test_market_data_cache.py`: store/retrieve,
  case-insensitivity, unknown-symbol, staleness, and `.stats` coverage for
  `put_best_bid_ask`/`get_best_bid_ask`.
- `tests/market_data/test_feeds.py`: book-ticker handler now asserted to
  call `cache.put_best_bid_ask()` with the parsed bid/ask, in addition to
  the existing event-publication assertion.
- `tests/strategy/test_orchestrator.py` (`TestSpreadFiltering`): disabled-
  by-default with a wide spread, rejects a signal when spread exceeds
  `max_spread_pct`, allows a signal within tolerance, and no-ops when no
  quote is cached yet for the symbol.

## [1.1.26] - Wired the remaining momentum/statistics indicators

### Context
Follow-up to the previous entry's "Not wired (out of scope for this pass)"
note: `momentum.py`'s stochastic/Williams %R and `statistics.py`'s
rolling z-score/regression-slope were still fully implemented,
unit-tested, and uncalled. Rather than force them into a third or fourth
near-duplicate accept/reject filter alongside RSI-exhaustion and
MACD-trend (which would mostly just double-count the same "is this move
overbought/oversold" signal under different names), traced where the
codebase already has infrastructure built specifically to consume extra
numeric signal context: `machine_learning/features.py::extract_features`.

### `confluence.py` now computes all four
`stochastic()`, `williams_r()`, `zscore()`, `linear_regression_slope()` —
all guarded the same way as the existing RSI/MACD computation (None on
insufficient candle history, no exception). Deliberately did *not* wire
`rate_of_change()`: it's highly collinear with the z-score/slope pair for
the same underlying question ("how extended is price"), and three
correlated momentum features add training-time collinearity without
adding real information.

All four land in `signal.metadata` (`stochastic_k`, `williams_r`,
`price_zscore`, `trend_slope`) alongside the RSI/MACD values already
added there — none feed the confluence score itself, same reasoning as
before.

### `machine_learning/features.py` — the actual consumer
Added 6 new dimensionless features to `FEATURE_NAMES`/`extract_features`:
`rsi_normalised`, `macd_histogram_atr`, `stochastic_k_normalised`,
`williams_r_normalised`, `price_zscore`, `trend_slope_atr`. This also
retroactively wires `rsi`/`macd_histogram`, which the previous pass added
to `signal.metadata` but never actually read anywhere — they were
computed and unused in exactly the pattern this whole audit exists to
catch. `N_FEATURES` goes from 28 to 34.

Safe to do because no trained model ships with the repo (`ml_filter`
defaults to `enabled: false` in `configs/strategy.yaml` for exactly this
reason) — there's no existing artifact whose schema this could silently
break. `SignalFilterTrainer.load()`'s existing
`saved_features != FEATURE_NAMES` check means any future save from before
this change would be rejected at load time with an explicit "retrain the
model" error rather than silently mismatched, which is the guard this
kind of schema change is supposed to rely on.

### Verification
- `python3 -m py_compile` clean across the full tree.
- Confirmed via `grep` that every consumer of `N_FEATURES`/`FEATURE_NAMES`
  in `trainer.py` and all three `tests/unit/test_{ml_features,
  signal_filter,trainer}.py` files references the constants symbolically
  (`X.shape == (n, N_FEATURES)`, `set(d.keys()) == set(FEATURE_NAMES)`,
  `FEATURE_NAMES[-1]`) rather than a hardcoded feature count or index —
  none needed updating, none broken by the count change.
- Hand-traced `extract_features()` for both a fully-populated
  `signal.metadata` and an empty one (the pre-wiring default for any
  signal generated before this change, or from a code path that doesn't
  go through `score_confluence()`): both resolve to finite,
  non-`NaN` values via the `is not None` guards on each of the 6 new
  fields — no division by zero, since `atr` already defaults to `1.0`
  when `analysis` is `None` or `analysis.atr` is falsy, per the existing
  code just above.
- Could not execute this trace at runtime — sandbox has no `pydantic`
  installed (no network access) — so this is a logic trace, not a test
  run. Run `pytest tests/unit/test_ml_features.py
  tests/unit/test_signal_filter.py tests/unit/test_trainer.py` locally
  before merging.

## [1.1.25] - Wired the gaps found in the post-merge audit

### Context
Follow-up to the "Post-merge gap audit" entry below: wired in the two real
gaps found there (LeverageConfig/set_margin_type, and the five unused
indicator modules), rather than just documenting them.

### `set_margin_type` — real bug fix
`bootstrap/app.py::apply_symbol_leverage()` called `set_leverage()` per
symbol but never `set_margin_type()`, even though every `IExchangeClient`
implementation (Binance, Bybit, paper trading) had it fully implemented
and individually unit-tested. `configs/symbols.yaml`'s `margin_type` was
decorative — a symbol configured for ISOLATED could silently run on
whatever margin mode the account already had (or CROSS default),
changing real liquidation math without anything in the system knowing.
Now calls both.

### `LeverageConfig` wired
`src/domain/leverage.py::LeverageConfig` was fully implemented, never
imported anywhere, no tests. Now:
- `SymbolRiskConfig.to_leverage_config()` (new) converts
  `configs/symbols.yaml`'s per-symbol settings into it — the validated,
  typed (uppercased symbol, `MarginType` enum) view both
  `apply_symbol_leverage()` and `RiskManager` now consume.
- `SymbolRiskConfig` gained `max_notional: Decimal | None` (was entirely
  missing — the field existed on `LeverageConfig` with no config-side
  counterpart to populate it from).
- `RiskManager.calculate_position_size()` enforces this as an additional
  hard notional cap alongside the existing `max_position_pct_of_equity`
  cap — useful for a symbol whose order book can't absorb an
  equity-proportional size without excess slippage regardless of account
  growth. Backward compatible: `leverage_configs` is a new optional
  constructor param, defaults to `{}`, existing `symbol_leverage: dict`
  usage is untouched.

### `CorrelationTracker` deduplicated
`risk/exposure.py` now calls `indicators/statistics.py::pearson_correlation`
instead of reimplementing Pearson correlation via `np.corrcoef` — the
audit flagged this as the same class of divergent-implementation risk
that previously caused the Sharpe/Sortino `ddof` mismatch. Existing
zero-variance NaN guard preserved exactly as-is.

### RSI + MACD wired into the strategy pipeline
`smart_money/confluence.py::score_confluence()` now computes RSI
(`indicators/rsi.py`) and the MACD histogram (`indicators/macd.py`, which
uses `ema.py` internally) per signal, guarded against
`InsufficientDataError` and NaN on short candle histories. Both land in
`signal.metadata` (`"rsi"`, `"macd_histogram"`) — **not** in the
confluence score itself; that 100pt budget across the existing SMC
factors is fixed and heavily tested, so folding momentum indicators into
it would have silently reweighted every existing signal score.

Two new opt-in filters in `strategy/filters.py`, wired into
`StrategyOrchestrator` exactly like `min_atr_pct` (disabled by default,
config-driven via `configs/strategy.yaml`, no-op on `None`/missing data):
- `filter_by_rsi_exhaustion` — rejects a LONG entering RSI-overbought or a
  SHORT entering RSI-oversold (`rsi_exhaustion_filter`,
  `rsi_overbought`/`rsi_oversold` in `strategy.yaml`).
- `filter_by_macd_trend` — rejects a signal whose direction disagrees with
  the MACD histogram's sign (`macd_trend_filter` in `strategy.yaml`).

Both default `false`/unset in `configs/strategy.yaml`; existing signal
flow is unchanged unless explicitly enabled.

### Not wired in this pass
`indicators/momentum.py`'s stochastic/Williams %R and
`indicators/statistics.py`'s rolling z-score/regression-slope helpers are
still unused as of this entry. Didn't map cleanly onto the existing
opt-in-filter pattern without inventing new semantics from scratch — left
alone rather than force a design decision that should be made
deliberately, not as a side effect of an audit-cleanup pass. (Wired in
the next entry above, via `machine_learning/features.py` instead of a
new filter — see "Wired the remaining momentum/statistics indicators".)

### Verification
- Full tree re-swept with `python3 -m py_compile` and `ast.parse` after
  all changes — clean, 0 errors.
- Traced every `RiskManager(...)` and `StrategyOrchestrator(...)`
  construction in `tests/` — all use kwargs, so the new optional
  constructor params (`leverage_configs`, `rsi_exhaustion_filter`,
  `rsi_overbought`, `rsi_oversold`, `macd_trend_filter`) are additive and
  non-breaking.
- Hand-traced `tests/strategy/test_confluence.py`'s `make_candles()`
  fixture (30 candles default, one call at 150) through both new
  indicator paths: RSI resolves normally (100.0 on the constant-price
  fixture — deltas are all zero); MACD's signal-line warmup needs 35
  candles so the 30-candle fixture correctly resolves to
  `NaN` → `None` via the guard, no exception. Existing
  `result.metadata["atr"|"has_ob"|"has_fvg"]` assertions check individual
  keys, not full-dict equality, so the two new `rsi`/`macd_histogram` keys
  don't break them.
- No test file constructs `SymbolRiskConfig` or asserts on
  `apply_symbol_leverage()`/`set_margin_type()` directly, so nothing
  existing exercises (or could be broken by) that path — flagging as a
  coverage gap worth closing next, not a risk in this change itself.

## [1.1.24] - Post-merge gap audit (no code change)

### Context
Requested audit of the reconciled `trading-final-4` tree for the project's
recurring "built but unwired" failure mode — components fully implemented
but never instantiated, imported, or called by any live code path. Scanned
every class definition and every non-`__init__.py` module in `src/` for
usages outside its own defining file, then hand-verified each hit/miss
against the actual bootstrap and strategy pipeline rather than trusting the
grep alone.

### Found: `src/domain/leverage.py::LeverageConfig` — dead, superseded code
Fully implemented (per-symbol leverage/margin-type model with an
`effective_margin_rate` property) but never imported anywhere, has no
tests, and isn't mentioned in this file. Live leverage handling is a
separate, fully-wired path that doesn't use it at all:
`core/config.py::SymbolTradingConfig.leverage` → loaded from
`configs/symbols.yaml` → pushed to the exchange by
`bootstrap/app.py::apply_symbol_leverage()` (called from
`bootstrap/startup.py`) → the resulting `symbol_leverage` dict is what
`RiskManager` actually uses for liquidation/margin checks. `LeverageConfig`
appears to predate that path and was never deleted when it was superseded
— the same "old implementation left behind after a rewrite" pattern
previously found with `PaperTradingSimulator`/`PaperBroker`. Left in place
pending a decision on whether to delete it or repurpose it (e.g. as the
validated input shape for `configs/symbols.yaml` itself, which today loads
into `SymbolTradingConfig` with no equivalent validator).

### Found: 5 of 6 indicator modules built, tested, never wired into the strategy
`src/indicators/{macd,ema,momentum,rsi,statistics}.py` are all implemented
with real logic and covered by unit tests (`tests/unit/test_momentum.py`,
`test_statistics.py`, `test_indicators.py`), but none of their functions
are called anywhere in `smart_money/confluence.py` or
`strategy/signal_engine.py` — the only two consumers of the `indicators`
package, and both only import `atr.py` and `volume.py`. `ema.py` is
additionally only referenced by the equally-unused `macd.py`. SMC
confluence scoring today runs on ATR and relative volume alone; RSI,
MACD, momentum/stochastic/Williams %R, and the rolling-stats helpers
(z-score, rolling correlation, regression slope) exist but contribute
nothing to signal generation. Not a bug — nothing calls them incorrectly,
they're simply inert — but worth a decision: wire a subset into confluence
scoring, or delete/relocate them to keep the indicators package's surface
matching what's actually used.

### Minor: duplicated Pearson correlation
`risk/exposure.py::CorrelationTracker` computes its correlation matrix
directly via `np.corrcoef` rather than reusing
`indicators/statistics.py::pearson_correlation`. No behavioral difference
found; flagged only because it's the kind of divergent-implementation-of-
the-same-math that previously caused the Sharpe/Sortino `ddof=0` vs
`ddof=1` bug (`analytics/performance.py` vs `sharpe.py`, see below). Not
urgent since both are simple, correct, and match each other's output.

### Verification (regression re-check on previously-fixed bugs)
Re-traced three previously critical, silently-failing bugs from earlier
sessions to confirm they're still fixed in this merged tree:
- Bybit `execType` → `"x": "TRADE"` fill mapping — intact
  (`exchange/adapters/bybit/websocket.py`).
- Circuit breaker fed by realized P&L — intact
  (`risk_manager.py::record_probation_result`).
- SL/TP fills closing positions — intact (`execution_engine.py`'s
  `_on_position_closed` / `close_position` path).

## [1.1.23] - Reconciled trading-final-merged with trading-final-2-1

### Context
`trading-final-2-1.zip` and `trading-final-merged.zip` were uploaded together.
`trading-final-merged` was itself the output of a prior reconciliation (see
the entry below this one) between `trading-final-2` and `trading-final-7`.
`trading-final-2-1` continued the `trading-final-2` lineage independently
past that point, with its own new session on top — no `.git` history in
either archive to establish which was authoritative, so this pass diffed
all 330 shared files, confirming 31 differed, and hand-merged the 7 that
were genuine two-way conflicts (both sides had touched the same function).
The other 24 were one-directional — either archive's change ported forward
cleanly with no overlap.

- **`trading-final-2-1` had, absent from `trading-final-merged`:** signal-
  to-trade `signal_id` lineage (`SignalGeneratedEvent.signal_id` forwarded
  through to the reconstructed `Signal` in both `orchestrator.py` and
  `execution_engine.py`), `SignalExpiredEvent` actually published for both
  expired-at-generation and expired-in-transit cases, `OrderBookUpdatedEvent`
  and `FundingRateEvent` producers (`BybitWebSocket._normalize_book_ticker`
  + `orderbook.1.*` routing, `MarketDataFeed`'s book-ticker subscription and
  funding-rate change-dedup), `Signal.is_price_in_entry_zone()`, and a
  `Dashboard.stop()` socket-leak fix (`server_close()`).
- **`trading-final-merged` had, absent from `trading-final-2-1`:** the
  entire prior reconciliation's ported work (Alembic migrations, `min_atr_pct`
  filter, confluence/analytics dedup — see below), plus session work unique
  to that upload: `validate_stop_risk()` finally wired into
  `RiskManager.validate_signal` (`max_stop_loss_pct`/`min_stop_loss_pct`
  config), partial take-profit ladders (`Position.partial_tp_plan`,
  `ExecutionEngine._submit_partial_tp`/`_resize_protective_orders`), and
  candle-gap backfill after WebSocket reconnect
  (`IWebSocketStreamClient.set_reconnect_callback`,
  `CandleService.resync_after_reconnect`).

### Hand-merged (both sides' work preserved)
- `src/core/events.py`, `src/domain/signal.py` — `signal_id` field and
  `is_price_in_entry_zone()` restored; no overlap with merged's own
  changes to these files.
- `src/strategy/orchestrator.py` — `SignalExpiredEvent` publish and
  `signal_id` forwarding on `SignalGeneratedEvent` construction merged
  alongside the existing `min_atr_pct` dead-market filter; both touch the
  same signal-generation loop but at different points (expiry check vs.
  ATR check vs. event construction), so both are intact.
- `src/execution/execution_engine.py` — `signal_id`/`generated_at`
  forwarding and `SignalExpiredEvent` publish restored in `_on_signal`,
  preserved alongside the existing partial-take-profit submission/resize
  logic and the `is_entry_valid()` staleness re-check in `_submit_entry`
  — disjoint code paths within the same file.
- `src/exchange/adapters/bybit/websocket.py` — `orderbook.1.*` →
  `_normalize_book_ticker` routing restored in `_dispatch_public` and the
  static method itself restored, alongside the existing
  `set_reconnect_callback`/`_ever_connected_public` reconnect-hook wiring.
- `src/market_data/feeds.py` — `FundingRateEvent` producer (with its
  per-symbol change-dedup state) and the book-ticker subscription/handler
  restored, alongside the existing reconnect-hook registration in
  `subscribe()`.
- `src/monitoring/dashboard.py` — `server_close()` restored in `stop()`.

### Verification
- `python3 -m py_compile` swept clean across all 292 `.py` files in the
  merged tree — zero syntax errors. Also independently re-verified via
  `ast.parse()` on every file.
- Grepped every merged file for duplicate method/class definitions
  post-merge (`_on_signal`, `_dispatch_public`, `_normalize_book_ticker`,
  `subscribe`, `_make_mark_price_handler`, `set_reconnect_callback`) to
  confirm the hand-merge didn't leave two competing definitions of the
  same symbol — each appears exactly once.
- Corresponding test files hand-merged the same way: `test_orchestrator.py`,
  `test_execution_engine.py`, `test_feeds.py`, `test_bybit_websocket.py`
  gained back `trading-final-2-1`'s regression tests for the features
  above, layered onto `trading-final-merged`'s existing test bodies (which
  cover the partial-TP/reconnect/min_atr_pct features `trading-final-2-1`
  didn't have). `test_watchdog.py` and `test_ml_features.py` gained back
  two and six test cases respectively that `trading-final-2-1` had added
  and `trading-final-merged` was missing. `test_dashboard.py` — both
  archives had fully independently rewritten this file; rather than hand-
  diffing two unrelated rewrites of the same suite, kept
  `trading-final-merged`'s version and added the one test case it was
  actually missing (`TestStopReleasesSocket`, covering the `server_close()`
  fix). `test_health.py` needed no changes — `health.py` itself is
  byte-identical between both archives (predates the fork point), so the
  two test files are independently-authored coverage of unchanged code.
- `tests/machine_learning/test_signal_filter.py` and `test_trainer.py`
  (from `trading-final-2-1`) were dropped in favor of
  `trading-final-merged`'s `tests/unit/test_signal_filter.py`/
  `test_trainer.py` — the same duplicate-test-suite situation
  `trading-final-2-1`'s own prior session had already found and resolved
  once for `test_watchdog.py`/`test_ml_features.py` (see that entry
  below), just not yet caught for these two files before this upload.
- `pytest` unavailable in this sandbox (no network access) — run the full
  suite locally before merging to `main`, particularly the four hand-
  merged test files.

## [1.1.22] - Reconciled two diverged archives (trading-final-2 and trading-final-7)

### Context
`trading-final-2-tuned-2.zip` and `trading-final-7.zip` were uploaded together
and shared identical history down through the "EventLoopWatchdog.on_timeout"
entry below, then forked — each side had accumulated real work absent from
the other, with no `.git` history in either archive to establish which was
authoritative:

- **`trading-final-2` had, undocumented nowhere else:** a volatility-regime
  logging fix in `execution_engine.py`, `RedisEventConsumer` wiring via a new
  `scripts/notifications_worker.py`, and four new test files (`test_health.py`,
  `test_dashboard.py`, `test_signal_filter.py`, `test_trainer.py`). All three
  sessions were individually changelogged in that archive.
- **`trading-final-7` had, undocumented in *either* archive's CHANGELOG:**
  Alembic migrations actually wired into `startup.py` (previously silently
  skipped — `create_tables()` was standing in for it and can't apply
  incremental revisions to an existing DB), a `min_atr_pct` dead-market
  filter wired end-to-end (`configs/strategy.yaml` → `main.py` →
  `StrategyOrchestrator`), `confluence.py` deduplicated onto
  `risk/stop_loss.py` / `risk/take_profit.py` plus a new liquidity-target
  take-profit path, and `analytics/performance.py` deduplicated onto
  `analytics/sharpe.py` / `expectancy.py` — the dedup fixed a real bug where
  the inline Sharpe/Sortino calc used `ddof=0` while `sharpe.py` used
  `ddof=1`, so the same trade history could report two different ratios
  depending on which code path computed it.

Resolution: `trading-final-7` kept as base (larger change set, includes an
actual correctness fix). `trading-final-2`'s three sessions ported on top:

### Ported from trading-final-2
- `execution_engine.py::_replace_stop_loss_order` now logs the volatility
  regime (`low`/`normal`/`high`) that drove a trailing-stop distance.
  Hand-merged onto final-7's version of the file, which already differed
  here due to the take-profit dedup above — the regime-logging change and
  the dedup touch disjoint code paths, so both are preserved intact.
  `tests/execution/test_execution_engine.py` — added
  `test_trailing_stop_update_logs_volatility_regime` and
  `test_breakeven_move_does_not_log_a_regime`.
- `scripts/notifications_worker.py` — standalone process subscribing to
  Redis Streams via `RedisEventConsumer`, fanning events out through the
  existing `Notifier` channels. Closes the loop on `EVENT_BUS_BACKEND=redis`
  (publish side was already wired; consume side had no caller anywhere).
  `ROADMAP.md` follow-up note ported alongside it.
- `tests/monitoring/test_health.py`, `tests/monitoring/test_dashboard.py`,
  `tests/unit/test_signal_filter.py`, `tests/unit/test_trainer.py` — none of
  these existed in the final-7 base; final-7's monitoring/ML source files
  (`dashboard.py`, `health.py`, `trainer.py`, `signal_filter.py`) had no
  dedicated test coverage until this merge.

### Verification
- `python3 -m py_compile` swept clean across every `.py` file in the merged
  tree — zero syntax errors.
- `execution_engine.py` and `test_execution_engine.py` merges were done by
  hand-diffing both archives' versions line-by-line rather than a blind
  file copy, specifically to avoid silently reverting final-7's take-profit
  dedup (the exact "fixes disappearing" failure mode this project has hit
  repeatedly on prior uploads).
- `pytest` unavailable in this sandbox (no network access) — run the full
  suite locally before merging to `main`, particularly the four newly
  ported test files and the two new `test_execution_engine.py` cases.

## [1.1.21] - EventLoopWatchdog.on_timeout now actually fires an alert

### Fixed
- **`EventLoopWatchdog.on_timeout` was effectively dead code in production.**
  `app.py` constructed the watchdog but never passed `on_timeout`, so a
  stalled event loop only ever produced a `logger.critical` line — nobody
  was paged. Wired `on_timeout=self._on_watchdog_timeout` in `app.py`,
  which publishes a CRITICAL `AlertFiredEvent` through the existing
  `AlertFiredEvent -> Notifier.send()` path set up in `startup.py`. This
  matters more than it might look: the dashboard's HTTP health endpoint
  that would otherwise surface a stall runs on the *same* event loop, so
  polling-based detection fails at exactly the moment it's needed —
  out-of-band notification (Telegram/Discord/Slack/email) is the only
  path that can actually reach an operator.
- Fixing this exposed a second, subtler bug: `EventLoopWatchdog` calls
  `on_timeout` from its background `threading.Thread`, not the asyncio
  loop. A naive fix (just passing an async callback) would either
  silently no-op (calling a coroutine function without awaiting it just
  constructs an unawaited coroutine object — Python won't even warn
  about that from a non-async caller in this pattern) or, if awaited via
  a throwaway `asyncio.run()` on the watchdog thread, break on any
  loop-bound resource the callback touches (aiohttp `ClientSession`,
  DB connections, etc. all belong to the *main* loop; using them from a
  second loop raises "Future attached to a different loop"). Rewrote
  `EventLoopWatchdog` to capture the running loop in `start()` and bridge
  coroutine callbacks back onto it via `asyncio.run_coroutine_threadsafe`,
  with a bounded `future.result()` wait so a genuinely wedged loop can't
  block the watchdog's own check cycle forever.
- `EventLoopWatchdog` previously re-invoked `on_timeout` on *every*
  `check_interval` for as long as a stall persisted (default: every 5s).
  Combined with the dead-callback bug above this was invisible, but once
  wired to an actual notifier it would have paged on-call every 5 seconds
  for the duration of any real stall. Changed to edge-triggering — fires
  once on the not-stalled -> stalled transition, logs once on recovery,
  and can fire again on a subsequent stall — matching the "publish on
  real transitions only" convention `CircuitBreaker` already uses
  elsewhere in this codebase.
- Added `EventLoopWatchdog.timeout_seconds` as a public property so
  `app.py`'s alert message doesn't need to reach into the private
  `_timeout` attribute.

### Added
- `tests/unit/test_watchdog.py` — regression coverage for the above:
  coroutine callback actually executes (bridged cross-thread), fires
  exactly once per stall rather than every check cycle, re-fires on a
  second stall after recovering from the first, coroutine callbacks are
  dropped-with-a-log (not raised, not silently constructed-and-lost) if
  `start()` was somehow called with no running loop, and the sync-callback
  code path (still supported) both runs and has its exceptions logged
  rather than propagated into the watchdog thread.

### Audit notes (no code change)
- Full `ast.parse` sweep across all 286 `.py` files: zero syntax errors.
- Searched for bare `assert` in `src/`: the only two hits
  (`Instrument.price_precision()` / `qty_precision()`) are guarding a
  `Decimal.as_tuple().exponent` type-narrowing that's already provably
  safe given Pydantic's `Field(gt=0)` validation upstream — left as-is,
  documented inline in the existing code, not a live bug.
- Searched for blocking calls inside `async def` (`requests.*`,
  `time.sleep`) — the only real hit was `EventLoopWatchdog._watch()`,
  which is correct as-is: it's a `threading.Thread` target, not a
  coroutine, so `time.sleep()` there is the right call, not the bug (the
  bug was downstream of it, in how `on_timeout` got invoked — see above).
- Searched for silently-swallowed exceptions (`except: pass` /
  `except Exception: pass`) across `src/`: none found.
- Cross-referenced every `*Event` class in `src/core/events.py` against
  actual `.publish(...)` call sites: `SignalExpiredEvent`,
  `OrderBookUpdatedEvent`, and `FundingRateEvent` remain unpublished —
  consistent with prior sessions' notes that these need components
  (signal-TTL tracking, an order-book cache, a funding-rate poller) that
  don't exist yet. **`PositionUpdatedEvent`, previously in that same
  deferred list, is now wired** (`execution_engine.py` publishes it for
  every affected open position after a fill) — that fix from a prior
  session did persist into this archive.

### Persistence status (tracked across sessions)
- **Improved this round**: all of `tests/notifications/*` (5 files),
  `tests/portfolio/*` (3 files), and `tests/monitoring/{test_alerts,
  test_metrics,test_telemetry}.py` survived from the previous session's
  delivery — this is the first time a full batch of previously-written
  tests has persisted intact. `PositionUpdatedEvent` wiring also
  persisted.
- **Still reverting (3rd occurrence)**: `tests/monitoring/{dashboard,
  health,watchdog}.py` and `tests/machine_learning/{trainer,
  signal_filter}.py` are absent from this archive despite being written
  in at least one prior session. Per the standing principle in this
  project not to rewrite dropped work a third time blind, these were
  **not** rewritten this session (except `test_watchdog.py`, which is
  new test coverage tied to the actual bug fix above, not a rewrite of
  previously-dropped content). If these keep disappearing specifically
  while notifications/portfolio/alerts now persist, that pattern is
  worth checking against `git log --all -- tests/monitoring/test_dashboard.py`
  (etc.) on your end — it'd help confirm whether it's a `git add`
  scoping issue (e.g. a `.gitignore` or pathspec that happens to catch
  those four files) rather than a push/commit issue in general, since
  the plain "nothing gets committed" theory is now hard to square with
  the files that *did* survive.

## [1.1.20] - configs/monitoring.yaml wired in; two documentation/dead-code cleanups

**Gap: `configs/monitoring.yaml` was never read by any code path**

Same class of bug as the earlier risk.yaml/symbols.yaml fixes. Alert
thresholds (`daily_loss_warning_pct`, `drawdown_warning_pct`) and cooldowns
were hardcoded directly in `monitoring/alerts.py`'s `build_standard_alerts()`;
health-check/heartbeat/watchdog intervals were hardcoded constructor
defaults in `bootstrap/app.py`. Editing `configs/monitoring.yaml` had zero
effect on the running system — an operator tuning alert sensitivity or
watchdog timeouts there would see no change.

**Fix**

- **`src/core/config.py`**: added `daily_loss_warning_pct`,
  `drawdown_warning_pct`, `alert_cooldown_minutes`,
  `health_check_interval_seconds`, `heartbeat_interval_seconds`,
  `watchdog_timeout_seconds`, `watchdog_check_interval_seconds` to
  `MonitoringConfig` (not env-backed — yaml-only, defaults match the
  previously-hardcoded values so behavior is unchanged with no yaml file
  present). New `Settings._load_monitoring_yaml()`, called from
  `load_yaml_overrides()` alongside `_load_risk_yaml`/`_load_symbols_yaml`,
  same env > yaml > default precedence. `enable_prometheus`/
  `prometheus_port`/`enable_dashboard`/`dashboard_port`/`enable_dashboard_ws`
  deliberately stay env-only (already reachable via `PROMETHEUS_PORT` etc.)
  to avoid a second, possibly-conflicting source of truth. New
  `Settings._monitoring_yaml_overrides` tracks which fields actually came
  from yaml, since after the merge `MonitoringConfig.model_fields_set` can
  no longer distinguish that (the rebuild passes every field explicitly).
- **`src/monitoring/alerts.py`**: `build_standard_alerts()` now accepts
  `daily_loss_warning_pct`, `drawdown_warning_pct`, `cooldown_minutes`
  params (all optional, defaulting to the prior hardcoded behavior).
  `cooldown_minutes`, when given, applies uniformly to all three rules —
  monitoring.yaml's `alerts.cooldown_minutes` doesn't distinguish per rule.
  When not given, each rule keeps its own tuned cooldown (5/60/120 min)
  since a CRITICAL circuit-breaker page shouldn't share a cooldown with a
  WARNING-severity threshold alert.
- **`src/bootstrap/startup.py`**: passes `settings.monitoring.*` into
  `build_standard_alerts()`; health-check scheduling now uses
  `settings.monitoring.health_check_interval_seconds` instead of piggy-
  backing on `ALERT_EVAL_INTERVAL_SECONDS`.
- **`src/bootstrap/app.py`**: `HeartbeatService`/`EventLoopWatchdog` now
  constructed with `settings.monitoring.heartbeat_interval_seconds` /
  `watchdog_timeout_seconds` / `watchdog_check_interval_seconds`.
- **`configs/monitoring.yaml`**: added a header comment clarifying which
  block is live vs. env-only, to prevent future confusion.

**Also**

- **`src/monitoring/health.py`**: removed a dead `Watchdog` class — a
  synchronous duplicate of `monitoring/watchdog.py`'s `EventLoopWatchdog`
  (the one actually constructed as `Application.watchdog`). Imported in
  `bootstrap/app.py` but never instantiated anywhere in `src/` or `tests/`;
  confirmed via grep before removal.
- **`DESIGN.md`**: corrected a stale "Known Limitations" entry claiming
  Binance listen-key renewal isn't implemented. Traced the full path
  (`create_listen_key` → `set_renewal_callback` → `_renewal_loop` →
  `renew_listen_key`, correctly sequenced before `ws_client.start()`) and
  confirmed it's fully implemented and correctly wired — the doc was just
  wrong, presumably left over from before that work landed.

**Tests**

- New `tests/core/test_config.py::TestMonitoringYamlLoading`: yaml values
  apply with no env override, missing file is a no-op, the
  `monitoring:`/prometheus/dashboard sub-block is correctly ignored.
- New `tests/monitoring/test_alerts.py` (previously 0% covered):
  `AlertRule.is_cooled_down()` timing, `build_standard_alerts()` default
  thresholds/cooldowns match pre-change behavior, custom threshold/cooldown
  params, and that `cooldown_minutes=None` preserves per-rule defaults.

## [1.1.19] - Paper trading now routes through the real execution/risk pipeline

**Gap: `TRADING_ENV=paper` was a label only — startup always connected to
the real exchange and placed real orders**

`Settings.is_paper` existed and was read in a couple of places (config
file selection, the equity-sign guard), but nothing in `bootstrap/app.py`
or `bootstrap/startup.py` ever branched execution on it. In paper mode the
system still built a real, credentialed `BinanceFuturesRESTClient`/
`BybitFuturesClient`, created a real listen key, subscribed the real
user-data stream to `FillEngine`, and called `OrderManager.submit_order`
straight through to `place_order` on the live exchange. "Paper trading"
would have submitted live orders with real capital.

Separately, `src/paper_trading/` (`PaperTradingSimulator`, `PaperBroker`,
`fills.py`, `balance.py`) existed as a fully unwired, parallel execution
path — nothing in `bootstrap/` ever constructed or called it, so it was
dead code. Even if wired up as originally written, it would have bypassed
`RiskManager`'s pre-trade checks and `OrderManager`/`FillEngine`'s
lifecycle handling entirely, meaning paper trading's risk enforcement
could silently diverge from live trading's.

**Fix**

- **`src/paper_trading/paper_exchange_client.py`** (new): `PaperExchangeClient`,
  a full `IExchangeClient` implementation. Market-data reads (candles,
  ticker, order book, exchange info) delegate to the real, credentialed
  client so paper trading still sees genuine live prices. Order actions
  are simulated in-memory: `MARKET` orders fill immediately at the last
  known mark price; `LIMIT`/`STOP_MARKET`/`STOP_LIMIT`/
  `TAKE_PROFIT_MARKET`/`TAKE_PROFIT_LIMIT` orders rest and are evaluated
  against live mark-price updates (subscribed directly via
  `IWebSocketStreamClient.subscribe_mark_price`, the same stream
  `MarketDataFeed` uses). Fills are delivered as the identical
  Binance-shaped `ORDER_TRADE_UPDATE` dict both live adapters normalize
  to, so `FillEngine.process_execution_report` needed zero paper-mode
  branching — paper fills flow through the exact same
  `FillEngine` -> `OrderManager` -> `ExecutionEngine`/`RiskManager` path
  live fills do.
  - Fill delivery from `place_order` is deferred via `asyncio.create_task`
    rather than delivered inline, since `OrderManager.submit_order` only
    registers the `client_order_id` -> `order_id` mapping *after*
    `place_order` returns; an inline fill would race that registration
    and get dropped as an unknown order.
  - Stop and take-profit trigger direction is asymmetric by design (a
    protective stop on a long triggers on a *drop*; a take-profit on the
    same long triggers on a *rise*) — see `_trigger_direction()`'s
    docstring; getting this backwards silently turns every stop-loss into
    a take-profit and vice versa.
  - Maintains its own lightweight average-cost position ledger (for
    `get_positions()`/reconciliation) and a paper `equity` balance that
    moves by realized P&L and simulated taker fees on each fill.
- **`src/bootstrap/app.py`**: added `Application.trading_client` — the
  exchange surface `OrderManager`/`ReconciliationService` actually submit
  orders against. Equal to `self.rest_client` normally; equal to a new
  `Application.paper_exchange_client` (constructed when
  `settings.is_paper`) in paper mode. `self.rest_client` itself is
  unchanged and always remains the real, credentialed client, since it's
  still needed for market data even in paper mode. `apply_symbol_leverage`
  now also goes through `trading_client` so paper mode doesn't push real
  leverage changes to a live account.
- **`sync_equity()`**: paper mode now reads `paper_exchange_client.equity`
  directly, bypassing `BinanceAccountService`/`get_account_balance()`
  entirely — there's no live account to query in the first place.
- **`src/bootstrap/startup.py`**: the listen-key creation and
  `ws_client.subscribe_user_data(fill_engine...)` block is skipped
  entirely when `app.paper_exchange_client` is set. Instead,
  `paper_exchange_client.subscribe_fills(fill_engine.process_execution_report)`
  is bound and `paper_exchange_client.start()` subscribes to live
  mark-price updates for every paper-traded symbol. The real `ws_client`
  still starts either way, since market data (candles, mark price) keeps
  flowing from the live exchange regardless of trading mode.
- **`src/bootstrap/shutdown.py`**: stops `paper_exchange_client` (if
  present) after the WebSocket disconnects, logging final paper equity.
- **`src/execution/order_manager.py`, `src/execution/reconciliation.py`**:
  widened the `exchange` constructor parameter from the concrete
  `BinanceFuturesRESTClient` to the `IExchangeClient` protocol, since both
  now legitimately receive either a real exchange client or
  `PaperExchangeClient`.
- **`src/core/config.py`**: added `Settings.paper_initial_equity`
  (`PAPER_INITIAL_EQUITY`, default `10000`) — starting USDT balance for
  `PaperExchangeClient`.
- Removed the now-superseded `src/paper_trading/broker.py`,
  `simulator.py`, `fills.py`, `balance.py` and their tests
  (`tests/paper_trading/test_broker.py`, `test_simulator.py`,
  `test_fills.py`, `test_balance.py`) — confirmed via grep that nothing
  outside that module's own tests ever imported them.

**Tests**

- New `tests/paper_trading/test_paper_exchange_client.py`: market-order
  fills, stop/take-profit trigger-direction correctness (including an
  explicit regression guard that the two must never trigger in the same
  direction for the same side), limit-order fill pricing, the deferred-fill
  race protection, cancel/get/get_open_orders behavior, and ledger/equity
  updates across open + close fills.
- `tests/bootstrap/test_startup.py`: added `TestPaperExchangeClientWiring`
  (listen key skipped, user-data subscription skipped, fills bound and
  paper client started, market-data WebSocket still starts). Updated
  `_fake_app` to take an explicit `paper_exchange_client` kwarg (`None` by
  default) so the existing listen-key/user-data tests keep exercising the
  live-wiring path independently of the `trading_env` label used for the
  unrelated environment/equity-guard tests in the same file.
- `tests/bootstrap/test_shutdown.py`: added `TestPaperExchangeClientTeardown`
  (stops when present, a failing stop doesn't block the rest of teardown,
  no-op when absent).
- `tests/bootstrap/test_app.py`: added coverage that paper mode wires
  `paper_exchange_client` as both `trading_client` and the `_exchange`
  each of `OrderManager`/`ReconciliationService` holds, and that non-paper
  mode is unaffected. Updated the existing `TestSyncEquity` tests
  (which exercise the live `account_service`/`rest_client.get_account_balance`
  branches) to construct `Settings` with `trading_env=TESTNET` rather than
  the default `PAPER`, since `Settings.trading_env` defaults to `paper` and
  the paper branch now takes priority — added a new `TestSyncEquityPaperMode`
  class specifically for the paper branch.
- Not executed in this environment (no network access to install
  `pytest`/`pytest-asyncio`); verified via `py_compile` across every
  touched/added module and by manually tracing the fill-simulation and
  reduce/flip P&L logic against the existing `Order`/`Position` domain
  models.

**Follow-up (not done this pass)**: `configs/paper.yaml` doesn't currently
set `PAPER_INITIAL_EQUITY` (it's env-only, defaulting to 10000) —
consider adding an explicit `.env.example` line per-environment if
different paper starting balances are wanted per config profile. Also
worth adding an integration-style test that drives a full
`SignalGeneratedEvent` -> `RiskManager` -> `ExecutionEngine` ->
`PaperExchangeClient` -> `FillEngine` -> `PositionManager` round trip,
rather than unit-testing each layer in isolation as done here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [1.1.18] - Regression: Dashboard.start() crashed on first request (root cause: no git history in uploaded archive)

**Root cause identified for the recurring "fixes keep disappearing" issue:
this upload contained no `.git` directory at all.** Every prior session's
work — commits, branches, everything — was absent from the archive. That
means the fixes from earlier sessions that *did* survive (Bybit websocket
parity, `EmailNotifier.send`, `trainer.cross_validate`, event-bus wiring,
the Bybit fill-type bug above) survived because they were baked directly
into the zip's file contents, not because git tracked them. Any fix that
was only committed locally and never re-included in the next zip export
is the one that reappears broken. **Action needed on your end: confirm
`git push origin main` is actually being run after each session before
re-zipping, or export the zip from a fresh `git archive`/`git clone`
rather than the working directory, so local commits can't be silently
left behind.**

**Bug: `Dashboard.start()` re-introduced direct instantiation of a
`BaseHTTPRequestHandler` subclass — `Handler.data_provider = Handler()`**

This is the exact `TypeError` bug fixed in a previous session (documented
in prior CHANGELOG entries and in project memory), now back in
`src/monitoring/dashboard.py`. `BaseHTTPRequestHandler.__init__` requires
`(request, client_address, server)`, which `HTTPServer` supplies per
connection — it is never valid to construct one directly. The line
`Handler.data_provider = Handler()` raised `TypeError` the instant it ran
(at `start()` call time, before any request even arrived), which would
have crashed dashboard startup in production.

**Fix**: reintroduced the `_DataProvider` plain-class pattern — a small
non-handler class exposing `health()`/`status()`/`metrics()`, assigned as
`Handler.data_provider`. `Handler` itself is now instantiated only by
`HTTPServer`, never directly. See inline comment in `dashboard.py` at the
fix site explaining why this must never regress again.

**Test suites not rewritten this pass**: monitoring alerts, dashboard,
health, watchdog, notifications, and machine_learning test suites remain
absent (as flagged in prior sessions) and were **not** rewritten a third
time, per the standing policy of diagnosing the persistence issue first.
Given the `.git`-absence finding above, that policy should now be
considered validated rather than resolved — the tests are safe to
(re)write once you confirm the push/export step is fixed, since otherwise
they will simply disappear again in the next handoff.

## [1.1.17] - Critical fix: Bybit fills were silently dropped

**Bug: `BybitWebSocket._normalize_execution` never set the "x" execution-type
field `FillEngine` gates on — every real Bybit fill was discarded**

`FillEngine.process_execution_report()` only processes a message when
`msg["o"]["x"] == "TRADE"` (this mirrors Binance's ORDER_TRADE_UPDATE,
where the same event type also carries NEW/CANCELED/EXPIRED variants
that must not be double-counted as fills). `BybitWebSocket._normalize_execution`
built the `"o"` block with `c`/`l`/`L`/`n`/`N`/`m` but never included `"x"`,
so `order_data.get("x", "")` always evaluated to `""` and the early-return
guard fired on **every single Bybit execution**, live or not. In practice
this meant: on `EXCHANGE_PROVIDER=bybit`, orders would submit and transition
to OPEN, but `FillEngine` would never register the fill, so
`OrderFilledEvent` never published, `PositionManager` never opened a
position, and protective stop-loss/take-profit orders never got placed —
a silent, capital-at-risk failure with no error or log line pointing at
the cause.

Existing test coverage (`tests/exchange/test_bybit_websocket.py`,
added in the previous pass) only asserted on `_normalize_execution`'s
return shape in isolation and never fed the result through
`FillEngine`, so it passed despite the bug.

**Fix** (`src/exchange/adapters/bybit/websocket.py`)
- `_normalize_execution` now reads Bybit's `execType` field (`"Trade"`
  for a genuine fill; `"Funding"`/`"AdlTrade"`/`"BustTrade"`/`"Delivery"`/
  `"BlockTrade"`/`"MovePosition"` for everything else the `execution`
  topic can carry) and maps it to `o["x"] = "TRADE"` only for real fills,
  passing the raw type through otherwise so non-fill entries correctly
  no-op in `FillEngine` rather than being silently miscounted either way.
  Missing `execType` defaults to `"Trade"`, since the `execution` topic
  only ever carries genuine execution entries.

**Tests** (`tests/exchange/test_bybit_websocket.py`)
- `TestNormalizeExecution`: three new cases — `execType="Trade"` maps to
  `x="TRADE"`, missing `execType` defaults to `x="TRADE"`, non-Trade
  `execType` (e.g. `"Funding"`) is passed through and does not become
  `"TRADE"`.
- New `TestNormalizeExecutionFillEngineIntegration` class: feeds
  `_normalize_execution`'s actual output through a real
  `FillEngine.process_execution_report()` (mocked `OrderManager`) —
  asserts a Trade execution calls `order.add_fill` and increments
  `_fills_processed`, and a Funding entry does neither. This is the
  end-to-end shape of test that would have caught the original bug;
  the prior shape-only assertions would not have.
- Not executed in this environment (no network access to install
  `pytest`/`pytest-asyncio`); verified via `py_compile` and by manually
  re-implementing and running the pure normalization logic inline.

## [1.1.16] - ML signal filter training script + backtest signal_id propagation fix

**Review pass**: re-verified three items ROADMAP.md's "Newer follow-ons"
section still listed as open. Two were already done (documentation had
gone stale, not code) and are now checked off with pointers to where the
wiring lives. The third — the training script — was a genuine gap.

**`scripts/train_signal_filter.py` (new)**
- End-to-end training script for the ML signal filter: runs
  `SMCStrategy` through `BacktestEngine` over historical candles,
  captures the `(Signal, SMCAnalysis)` pair behind every emitted signal
  (via `SMCSignalEngine.last_analysis`, observed — not altered —
  immediately after `on_candle()` returns), joins closed trades back to
  their originating signal by `signal_id`, and labels each example by
  realized outcome (`net_pnl > 0`).
- Train/test split is chronological, never shuffled — a random split
  would leak information from later setups into training on earlier
  ones, which is look-ahead bias for a time series.
- Runs `SignalFilterTrainer.cross_validate()` (time-series CV) on the
  training split, fits the final model, evaluates on the held-out split
  when it has both classes represented, and saves via
  `SignalFilterTrainer.save()` to `models/signal_filter.pkl` by default
  (`--output` to override).
- Not executed in this environment — no network access to fetch
  candles or install `pydantic`/`xgboost`/`scikit-learn`; verified by
  `py_compile`/`ast.parse` and manual tracing against
  `SignalFilterTrainer`'s actual signature. Run locally against real
  history before trusting the saved model.

**Bug fix: `backtesting/simulator.py` dropped `signal_id`/`strategy_id`**
- `BacktestBroker` was constructing `Position` (on entry fill) and
  `Trade` (on close, via `Trade.from_position`) without passing
  `signal_id`/`strategy_id` through from the originating `Signal`, even
  though both domain models support the fields and the *live* path
  (`execution/execution_engine.py`) already threads them correctly. This
  meant no backtested trade could ever be joined back to the signal that
  generated it — the exact join `train_signal_filter.py` needs, and also
  useful for any future per-signal-component win-rate analysis.
- Fixed in both the `Position(...)` construction in
  `process_pending_orders()` and the `Trade.from_position(...)` call in
  `_close_position()`.
- Added `TestSignalIdPropagation` (`tests/backtesting/test_simulator.py`):
  covers both the open-position and closed-trade cases.

**Test coverage: Bybit private WebSocket had none**
- `src/exchange/adapters/bybit/websocket.py`'s private stream (auth
  signing, execution/order-status normalization to the
  `ORDER_TRADE_UPDATE` shape `FillEngine` consumes) had zero test
  coverage despite being on the live fills path. Added
  `tests/exchange/test_bybit_websocket.py`: auth signature correctness
  and short expiry window, depth-level snapping, all four public-message
  normalizers (kline/trade/ticker + the taker-side → maker-flag
  inversion), both private normalizers, and subscription-management
  guards (`subscribe_user_data` registers a handler without adding a
  public topic; no credentials + no handler never schedules a private
  connection). No network — pure-function/state assertions only, same
  scope as `test_binance_websocket.py`.

**ROADMAP.md / CHANGELOG.md housekeeping**
- Checked off the correlation-sizing and Bybit-provider items in
  ROADMAP.md's "Newer follow-ons" section — both were already fully
  wired (confirmed by reading `bootstrap/app.py`, `startup.py`,
  `risk_manager.py`, and `exchange_factory.py` directly) and had simply
  never been marked done.
- Annotated the `[1.1.0]` "Known limitations" note (which listed all
  three as still-open) with a pointer to where each was actually
  resolved, rather than editing it in place — that section should
  reflect what shipped in v1.1.0 at the time, not the current state.

## [1.1.15] - Wire PositionUpdatedEvent

**Event bus — last unwired event schema given a real producer**

`PositionUpdatedEvent` was the one remaining `BaseEvent` subclass with
zero producers after the previous event-bus wiring pass.

- **`ExecutionEngine._on_mark_price()`** now publishes a
  `PositionUpdatedEvent` for every open position on a symbol immediately
  after calling `PositionManager.update_mark_price()`. This gives
  dashboards and any other subscriber live unrealized-PnL visibility
  between `PositionOpenedEvent` and `PositionClosedEvent` without
  polling `PositionManager` state directly. The event carries
  `position_id`, `symbol`, `quantity`, and `unrealized_pnl` — the
  minimum state needed to keep a live blotter current.

  Events are not published when the engine is stopped (`_running=False`)
  or when no open positions exist for the symbol, matching the existing
  guard already in `_on_mark_price`.

**Tests**

- `tests/execution/test_execution_engine.py`: new
  `TestPositionUpdatedEventIsPublished` class with four cases —
  single-position publish, multi-position publish (one event per
  position), no event when no open positions, no event when engine is
  not running.

## [1.1.14] - Event-bus wiring: order/circuit-breaker/risk/system events

**Event bus — previously dead event schemas given real producers**

An audit of `src/core/events.py` found that roughly a dozen `BaseEvent`
subclasses existed as fully-defined, documented schema with **zero
producers anywhere in the codebase** — the same "built but unwired"
pattern found in earlier review passes (notifications, `ExposureTracker`,
`MetricRepository`). This pass wires the highest-value ones to their
natural call sites:

- **`OrderManager`** (`execution/order_manager.py`) now publishes, via the
  event bus's `publish_sync` fire-and-forget path:
  - `OrderSubmittedEvent` when an order is sent to the exchange
  - `OrderStatusChangedEvent` on every local status transition
    (PENDING→SUBMITTED→OPEN, →FAILED, →CANCELLED)
  - `OrderRejectedEvent` on definite submission failure, including the
    "ambiguous failure confirmed not landed" path
  - `OrderCancelledEvent` on successful cancellation
  - Previously, nothing downstream of order state (dashboards, audit
    consumers, notifiers) could observe the order lifecycle except by
    polling `OrderManager.stats` / `get_open_orders()`.

- **`CircuitBreaker`** (`risk/circuit_breaker.py`) now publishes
  `CircuitBreakerStateChangedEvent` from `trip()`, `reset()`, and
  `half_open()` — but only on an actual state transition (calling
  `trip()` while already OPEN, or `half_open()` while CLOSED, publishes
  nothing, matching the existing no-op guards).

- **`RiskManager`** (`risk/risk_manager.py`) now publishes:
  - `DailyLossLimitReachedEvent` when the daily loss limit trips the
    circuit breaker in `validate_signal()`
  - `RiskLimitBreachedEvent` for both the drawdown-limit trip in
    `validate_signal()` and the gross-exposure trip in
    `check_portfolio_risk()`
  - Previously these limits were enforced (exception raised, breaker
    tripped) but nothing on the event bus ever explained *why* — only
    the generic `AlertManager` polling loop (every
    `ALERT_EVAL_INTERVAL_SECONDS`) would eventually notice via
    `circuit_breaker_open`.

- **`HealthChecker`** (`monitoring/health.py`) was instantiated in
  `Application.__init__` but `.register()` was never called and
  `.run_all()` was never scheduled anywhere — completely dead
  infrastructure; the dashboard's health endpoint reads the watchdog
  directly instead. `startup.py` now registers `check_database` and
  `check_exchange`, schedules periodic evaluation alongside the other
  scheduler tasks, and publishes `ComponentHealthChangedEvent` whenever
  a check's status actually changes (not on every poll).

- **`SystemStartedEvent`** is now published at the very end of
  `startup.py`, after every prior step (including the health-check
  registration and alert wiring) has succeeded — so a subscriber that
  reacts to "system is up" only fires once startup is genuinely
  complete.

- **`SystemShutdownEvent`** is now published as the first step of
  `shutdown.py`, before any teardown, so a subscriber (e.g. a notifier)
  still has a working network/DB connection to report the shutdown is
  starting.

**Not wired in this pass** (left as-is; no natural single producer
without a larger feature, out of scope here): `OrderBookUpdatedEvent`,
`FundingRateEvent`, `SignalExpiredEvent`, `PositionUpdatedEvent` — these
require an order-book feed, funding-rate poller, signal-expiry sweep, and
mark-price-driven position update loop respectively, none of which
currently exist as a component to hang the publish call off of.

**Tests**
- `tests/execution/test_order_manager.py`: new `TestOrderLifecycleEventsArePublished`
  class, `isolated_event_bus` fixture added (existing `EventBus`/
  `reset_event_bus`/`set_event_bus` pattern from `test_reconciliation.py`)
- `tests/risk/test_circuit_breaker_guards.py`: new
  `TestCircuitBreakerStateChangedEventIsPublished` class, including the
  no-op-guard-publishes-nothing cases
- `tests/risk/test_risk_manager_events.py` (new file): daily-loss,
  drawdown, and gross-exposure event-publishing coverage

## [1.1.13] - Gross/net exposure limits wired in

**Risk management**
- `RiskManager` now constructs an `ExposureTracker` (`risk/exposure.py`)
  unconditionally in `__init__` — unlike `CorrelationTracker` it needs no
  live candle feed, only the current `Portfolio` snapshot, so there's no
  reason to make it opt-in. `ExposureTracker` was fully implemented and
  already had passing unit tests (`tests/risk/test_exposure.py`) but was
  never instantiated anywhere in the live risk path — another instance of
  the "built but unwired" pattern found in earlier review passes.
- `calculate_position_size()` gained two new pre-trade checks, run after
  the existing `max_correlated_exposure_pct` group cap:
  - **Gross exposure cap** (`RiskConfig.max_gross_exposure_pct`, default
    200%): total notional across all open positions (longs + shorts
    combined), projected to include the position being sized. This is the
    general, symbol-agnostic leverage ceiling that
    `max_correlated_exposure_pct` does not provide — that field only fires
    for the "all majors move together" grouping.
  - **Net exposure cap** (`RiskConfig.max_net_exposure_pct`, default
    150%): net directional notional (longs − shorts), also projected.
    Catches a book that satisfies every per-position, per-symbol, and
    gross-exposure limit individually but still ends up almost entirely
    long or almost entirely short because new positions keep landing on
    the same side.
- `RiskManager.check_portfolio_risk()` was previously a near-stub that
  only checked whether the circuit breaker was open. It now also enforces
  the gross exposure cap via `ExposureTracker.is_within_limits()` and
  trips the circuit breaker on breach. This is the periodic counterpart to
  the pre-trade checks above — a book can drift over the gross ceiling
  purely from price moves on existing positions, with no new signal to
  trigger the pre-trade path.
- `check_portfolio_risk()` is now actually called: `startup.py` schedules
  it every `PORTFOLIO_RISK_CHECK_INTERVAL_SECONDS` (60s, new constant in
  `core/constants.py`) via the existing scheduler, alongside the
  `equity_resync` and `metrics_snapshot` jobs. It was already part of the
  `IRiskManager` interface but had no caller anywhere in the codebase.
- Added `RiskConfig.max_gross_exposure_pct` and `max_net_exposure_pct`
  fields (env-configurable via `RISK_MAX_GROSS_EXPOSURE_PCT` /
  `RISK_MAX_NET_EXPOSURE_PCT`).
- Added `tests/risk/test_risk_manager.py::TestRiskManagerExposureCaps`
  covering: gross cap rejects when correlated/net caps are permissive, net
  cap rejects directional skew on an otherwise-hedged book when
  gross/correlated caps are permissive, a signal within all caps still
  sizes normally, and `check_portfolio_risk()` trips the breaker on an
  already-over-limit book with no new signal involved. As with prior
  sessions, these were hand-traced against the sizing/fee/margin formulas
  rather than executed — no pydantic/pytest in this sandbox.

## [1.1.12] - Correlation-adjusted position sizing wired in

**Risk management**
- `RiskManager` now accepts an optional `correlation_tracker:
  CorrelationTracker | None` constructor parameter (opt-in, following the
  existing `filters.py` convention). `exposure.py`'s `CorrelationTracker`
  was fully built with tests-worthy logic (rolling price-return
  correlation matrix, diversification ratio, highest-correlated-pair) but
  had no live feed and no consumer — another instance of the "built but
  unwired" pattern found in earlier passes.
- `RiskManager.start()` now subscribes to `CandleClosedEvent` when a
  tracker was supplied, feeding it closing prices from
  `market_data/feeds.py` (which already published the event; nothing
  downstream ever subscribed to it for this purpose). No tracker means no
  subscription — avoids dead event handling on every candle close for
  single-symbol or correlation-disabled configurations.
- `calculate_position_size()` now calls `correlation_adjusted_size()`
  (`portfolio_risk.py`, previously dead code with no caller) after the
  equity cap and HALF_OPEN probation multiplier are applied, scaling the
  new position down — never up — based on its worst correlation against
  already-held symbols. This sits alongside, not instead of, the existing
  coarse "all majors are one correlated group" notional cap.
- `Application.__init__` (`bootstrap/app.py`) now builds a
  `CorrelationTracker` from the enabled symbols in `settings.symbols` and
  passes it to `RiskManager`, but only when 2+ symbols are enabled (a
  single-symbol universe has nothing to correlate against, and the
  tracker's own `window >= 2` invariant needs a real pair to be
  meaningful).
- Added `RiskConfig.correlation_window` (default 60, matching
  `CorrelationTracker`'s own default) so the rolling window is
  configurable via `RISK_CORRELATION_WINDOW` like other risk parameters,
  instead of hardcoding it at the call site.
- Added `tests/risk/test_risk_manager.py::TestRiskManagerCorrelationAdjustedSizing`
  covering: no-tracker sizing is unaffected (regression guard), a tracker
  with no open positions doesn't scale anything, a highly correlated pair
  is measurably scaled down vs. an equivalent run without a tracker, the
  `CandleClosedEvent` subscription actually feeds the tracker after
  `start()`, and `start()` does not subscribe to `CandleClosedEvent` at
  all when no tracker was supplied. Not executed in this environment — no
  network access to install `pydantic`/`pytest`; verify locally before
  merging.

## [1.1.11] - Re-applied dropped exchange-provider/portfolio/backtesting fixes + new fixes

This `main` branch was missing several fixes from a previous local
session that were apparently never pushed: Bybit's WebSocket client was
present, but `app.py` still hardcoded Binance construction (ignoring
`exchange_provider`), `src/portfolio/` was back to an empty package
stub, and `SlippageModel`/`CommissionModel` were plain classes again
instead of `ABC`s. All three are re-applied here, plus new items found
in this pass:

**Exchange-provider routing (re-applied + extended)**
- Added `IWebSocketStreamClient` protocol and `get_exchange_info` to
  `IExchangeClient` in `core/interfaces.py`.
- Bybit REST client now implements `get_exchange_info()`, normalizing
  `/v5/market/instruments-info` to the Binance `exchangeInfo` shape
  (`parser.normalize_instruments_info`) so `parse_instruments` needs no
  provider-specific branch.
- `exchange_factory.build_ws_client()` added, mirroring `build_rest_client`.
- `app.py` builds both REST and WS clients via the factory instead of
  hardcoding Binance; `sync_equity()` branches by provider (Binance:
  total margin balance; Bybit: wallet balance, documented as the more
  conservative figure).
- `startup.py`'s listen-key/renewal flow now only runs for Binance —
  Bybit's private WS authenticates per-connection via signed HMAC frames
  and needs none of it.

**Portfolio module (re-applied)**
- Rebuilt `src/portfolio/`: `allocator.py` (equal-weight / risk-parity /
  fixed-weight target notional, reusing the existing
  `risk.portfolio_risk.risk_parity_weights` rather than duplicating the
  math), `rebalancer.py` (drift detection only — never places orders,
  mirroring the reconcile/execute split used elsewhere), `service.py`
  (facade). Unit tests added under `tests/portfolio/` (not executed in
  this environment — no network access to install `pydantic`/`pytest`;
  verified by `ast.parse` + `py_compile` only, run locally before deploy).

**Backtesting (re-applied)**
- `SlippageModel` / `CommissionModel` converted to `abc.ABC` with
  `@abstractmethod`, so a subclass missing `apply`/`calculate` fails at
  instantiation instead of at first backtest run.

**New fixes this pass**
- `EmailNotifier.send()` called blocking `smtplib` directly inside an
  `async def`, contradicting its own docstring ("runs in executor") —
  this stalled the entire event loop for up to the 10s SMTP timeout on
  every alert. Fixed with `run_in_executor`; also wired `EmailNotifier`
  into `app.py._setup_notifications` (was never constructed even when
  fully configured) and added `NotificationConfig.email_enabled`.
- Replaced three bare `assert`s guarding request/connection invariants
  (`base_client._execute_request`, both WebSocket clients) with explicit
  checks + exceptions — asserts are stripped under `python -O`, silently
  turning a clear "not connected" error into a deeper `AttributeError`.

## [1.1.10] - Targeted re-pin of the 9 packages with open Dependabot alerts

### 48 open alerts (9 high / 22 moderate / 17 low), all against unpinned deps

Confirmed via `GET /repos/{owner}/{repo}/dependabot/alerts?state=open`
(not assumed from the summary count Dependabot posts on push): 48 open
alerts across 9 packages, all in `requirements.txt`/`pyproject.toml`.
`aiohttp` alone accounts for 34 of them (`<=3.14.2` through `<=3.10.10`
across a run of separate CVEs); the rest are `cryptography` (7),
`black`, `msgpack`, `orjson`, `lightgbm` (1 high each), and
`python-dotenv`, `pytest`, `scikit-learn` (1 moderate each).

This is the direct, expected consequence of "Fixed broken pyproject.toml
pushed directly to main" below: with every dependency unpinned, the
resolver is free to land on any vulnerable release, and apparently did.
That entry's tradeoff was made deliberately and explicitly, and stays in
effect — this fix does **not** re-pin the whole dependency list.

**Fixed only the 9 packages with a currently-open alert**, each pinned
to the lowest version that resolves every CVE against it (computed
from `first_patched_version` across that package's alerts, then
verified to exist on PyPI before use — not copied from memory):
`aiohttp>=3.14.3`, `cryptography>=49.0.0`, `orjson>=3.11.6`,
`msgpack>=1.2.1`, `python-dotenv>=1.2.2`, `scikit-learn>=1.5.0`,
`lightgbm>=4.6.0` (production, `pyproject.toml` + `requirements.txt`),
and `pytest>=9.0.3`, `black>=26.3.1` (dev extras in `pyproject.toml`;
`requirements-dev.txt` was already pinned above both floors —
`pytest==9.1.1`, `black==26.5.1` — no change needed there). Every
other dependency is left exactly as unpinned as it was.

Also corrected the comment above `pyproject.toml`'s `dependencies`
list, which claimed lower bounds "match requirements.txt's patched
pins exactly" — false at the time this was found, since every
dependency in both files was a bare name with no version at all.

Verified: `pip install -e ".[dev]"` in a clean venv resolves
`aiohttp==3.14.3`, `cryptography==50.0.0`, `orjson==3.11.9`,
`msgpack==1.2.1`, `python-dotenv==1.2.2`, `scikit-learn==1.9.0`,
`lightgbm==4.7.0`, `pytest==9.1.1`, `black==26.5.1` — every resolved
version at or above its floor; full suite (2166 tests) passes in that
same venv, `--no-cov`.

Not yet independently re-verified: whether GitHub's Dependabot alert
count has actually dropped to 0 after this fix, since alert re-scan
timing is outside this environment's control — check again after this
commit is pushed and given time to propagate (the prior "pyproject.toml
lower bounds were still vulnerable" entry below is a cautionary tale
about not assuming the count dropped just because the manifest changed).

## [1.1.9] - Trailing stop / breakeven wired into live execution (was dead code)

### ExecutionEngine never actually managed trailing stops or breakeven

Its own module docstring listed step 7 as "Manage trailing stop /
breakeven", but no code did it — traced the whole chain and found three
separate implementations of this logic, all unreferenced outside tests:
`src/risk/trailing_stop.py`, `src/risk/breakeven.py`, and
`Position.set_breakeven()` were never called from `src/`. In practice,
once a position opened with its initial SL/TP resting orders, nothing
ever tightened the stop until SL/TP was hit outright or the position
closed some other way — no trailing, no breakeven, no partial TP, for
the entire life of every trade.

Fixed by wiring `strategy/exit.py`'s `evaluate_exit_actions` (the newer,
already-tested ExitPlan module) into `ExecutionEngine`:

- Subscribes to `TickReceivedEvent` and re-evaluates open positions on
  every mark-price tick (`manage_trailing_exits=True` by default,
  toggle off via constructor arg).
- Hard stop/TP crossing is still left entirely to the exchange-side
  resting orders — this handler only ever *tightens* the protective
  stop, never force-closes locally, so it can't diverge from what
  actually happens on the exchange.
- A stop update is a cancel-then-resubmit at the exchange: if the cancel
  fails (e.g. the old stop just filled), the update is skipped rather
  than risking two live stop orders. If the resubmit fails after a
  successful cancel, fires the existing CRITICAL `unprotected_position`
  alert rather than silently leaving the position unprotected.
- Breakeven's target price is computed without mutating position state
  first; `breakeven_triggered`/`stop_loss` are only committed after the
  exchange-side order swap succeeds, so a failed update can't leave
  local state claiming breakeven is live when it isn't.
- ATR for regime-aware trailing is seeded from the signal that opened
  the position (stored in `Position.tags["entry_atr"]`) since nothing
  currently pushes a live ATR feed to the execution layer once a
  position is open — noted as a known limitation, not a live feed.

Removed the two now-fully-superseded modules (`src/risk/trailing_stop.py`,
`src/risk/breakeven.py`) and their dedicated test files, since keeping
duplicate, semi-overlapping, still-partially-unwired implementations
around alongside the real one is exactly the kind of disconnect this
codebase keeps needing to be re-audited for. `tests/unit/test_risk.py`'s
`TestTrailingStop`/`TestBreakeven` classes now exercise
`strategy/exit.py`'s equivalents instead so coverage isn't lost.

Added `tests/execution/test_execution_engine.py::TestTickDrivenExitManagement`
covering: mark-price propagation, breakeven trigger + order replace,
trail-only-improves gating, stop/TP-hit deferring to the exchange order,
cancel-failure not double-submitting, and the disable toggle.

## [1.1.8] - Strategy scoring/filters/exit enhancements

### Confluence scoring: graded instead of binary per-factor

`score_confluence` previously gave a flat, all-or-nothing point award for
order blocks (20pts) and FVGs (15pts) the instant price touched one,
regardless of how old the OB was or how small the FVG was relative to
ATR. Two setups with a 90-bar-old OB and a fresh one scored identically.

- Order blocks now grade by freshness (full credit ≤50 bars old, decaying
  to a 60% floor by the 100-bar max age) and by proximity (direct touch
  vs. within 1%). When multiple OBs qualify, the highest-scoring one is
  used instead of the first one found.
- FVGs now grade by gap size relative to ATR (a gap smaller than 0.5x ATR
  is a shallow imbalance, not a real institutional footprint) plus
  proximity, and also pick the best-scoring candidate.
- Fixed a real gap in liquidity-sweep scoring: any swept pool scored 8pts
  regardless of which side it was on. A LONG setup was getting "sweep
  confirmation" credit even from a swept buy-side (equal highs) pool,
  which doesn't support a long entry. Now only pools swept on the
  direction-aligned side count, graded up (to a 12pt cap) by how many
  prior touches built up at that level; a misaligned sweep drops to 4pts.

All existing pinned-score unit tests are unaffected (verified by hand:
the default test fixtures are fresh/direct-touch/full-size, which still
score full points under the new grading). New tests added for the aged
OB, near-touch OB, shallow FVG, and misaligned/graded liquidity cases.

### Filters: spread/weekend/correlation checks are now actually wired up

`apply_all_filters`'s docstring and the module docstring both described
spread, weekend, and correlated-position filtering, but the function only
ever applied score/RR/ATR checks — `filter_by_spread` and
`filter_by_weekend` existed but were dead code from `apply_all_filters`'s
perspective. Added `filter_by_correlated_positions` and wired all three
into `apply_all_filters` as opt-in checks (only run when their inputs are
supplied), so existing callers are unaffected.

### Exit: volatility-regime-aware trailing stops + unified exit evaluation

- Added `classify_volatility_regime` / `regime_aware_trail_multiplier`:
  the trailing-stop ATR multiplier was a fixed 2.0x, too tight for choppy
  markets (stopped out by noise) and too loose for quiet ones. Now scales
  1.5x/2.0x/2.75x by low/normal/high ATR-relative-to-price regime.
- Added `evaluate_exit_actions`, returning a single `ExitPlan` that
  correctly prioritizes stop/TP hits over trailing/breakeven adjustments
  in one call, instead of callers having to re-derive that ordering.

## [1.1.7] - src/persistence/ coverage pass, SQLite connect() bug fix

### Database.connect() rejected SQLite despite advertised support

Found while writing test coverage for `src/persistence/database.py`
(not searched for independently): `Database.connect()`
unconditionally passed `pool_size`, `max_overflow`, and `pool_timeout`
to `create_async_engine()`. These are Postgres-connection-pool-
specific kwargs; SQLAlchemy's SQLite async dialect rejects them
outright (`TypeError: Invalid argument(s) ... for connection pool`).
This directly contradicted `DatabaseConfig.validate_database_url()`,
which explicitly allows URLs starting with `"sqlite"` as well as
`"postgresql"` — meaning SQLite was a formally advertised, supported
configuration that could never actually connect. This matters beyond
testing convenience: it blocks any real local-development or
lightweight-deployment path that intentionally chooses SQLite over
running a full Postgres instance.

Fixed: `connect()` now only attaches the Postgres pool kwargs when
`self._config.url.startswith("postgresql")`; other dialects (SQLite)
get a plain `create_async_engine(url, echo=...)` call.

### src/persistence/ test coverage: 0% -> 96-100% per module

Added `tests/persistence/` (conftest.py + 6 test files), using a real
in-memory SQLite database (`sqlite+aiosqlite:///:memory:`) rather than
mocking SQLAlchemy — repository upsert semantics, unique-constraint
duplicate detection, and filtering are only meaningfully verified
against a real database engine executing real SQL. Added `aiosqlite`
as a dev/test dependency in both `requirements-dev.txt` and
`pyproject.toml`'s dev extras (keeping the two manifests in sync, per
this project's own documented "two-manifest trap" lesson).

- `test_repository.py`: BaseRepository's save/upsert/find_by_id/
  find_all (including multi-column filtering and the silently-ignored-
  unknown-column behavior)/delete/delete_by_id/count, exercised
  through the concrete OrderORM model.
- `test_orders.py`, `test_trades.py`, `test_signals.py`,
  `test_metrics.py`: each repository's domain-to-ORM field mapping,
  verified against actual persisted rows, not just constructed objects.
- `test_database.py`: connect/disconnect lifecycle, create_tables's
  not-connected guard and real DDL execution, session()'s commit-on-
  success and rollback-on-exception-with-propagation semantics
  (verified with a real second session confirming durability/absence),
  and the get_database() singleton.
- `test_migrations.py`: run_migrations' Alembic Config wiring (mocked,
  since exercising real migration scripts against a real DB is out of
  scope for unit tests) including the default-vs-custom
  migrations_dir path and failure propagation; create_all_tables'
  integration with the Database singleton.

One real, non-trivial test-design finding along the way: an initial
db_session fixture yielded a bare session with no explicit commit.
BaseRepository.save()/upsert() only flush() (visible within the
current transaction) by design, deferring commit to a higher-level
unit-of-work boundary (Database.session(), which does commit on
success) -- so a bare, uncommitted session made every write silently
vanish when the fixture tore down. This surfaced as a specific test
failure (a row was unexpectedly None after a rollback) which was
traced to its root cause via raw SQL introspection (checking table
row counts before/after the implicit teardown) rather than adjusted
away superficially. Fixed by having the fixture commit on teardown,
mirroring Database.session()'s real production pattern.

Coverage impact: repository.py 0%->96%, orders.py/trades.py/
signals.py/metrics.py/migrations.py 0%->100%, database.py 0%->97%.

Verified: full suite (576 tests total) passes, --no-cov.

## [1.1.6] - Misleading alert severity fix found during coverage work

### ExecutionEngine conflated take-profit-only failures with unprotected positions

Found while writing test coverage for `src/execution/execution_engine.py`
(not searched for independently): `_place_exit_orders()` correctly
gives the stop-loss leg its own retry budget and treats a take-profit-
only failure as a documented "degraded-but-safe" state in its own code
comments (stop-loss is live; the position is not at unbounded risk,
only unable to auto-lock-in gains). However, it still re-raised the
bare exchange exception for a take-profit failure, and the caller
(`_on_entry_filled`) caught it with the same generic
`except Exception` used for a genuinely failed stop-loss — firing the
identical CRITICAL `unprotected_position` alert either way. In
production this means an on-call engineer paged for "UNPROTECTED
POSITION" could actually be looking at a fully stop-loss-protected
position where only the take-profit order failed to place — a
real, misleading-severity incident-response gap.

Fixed:
- Added `TakeProfitPlacementError` (`src/core/exceptions.py`), raised
  by `_place_exit_orders()` specifically for the TP-only failure case
  instead of the bare underlying exception.
- `_on_entry_filled()` now catches `TakeProfitPlacementError` before
  the generic `except Exception`, firing a distinct
  `take_profit_placement_failed` / `AlertSeverity.WARNING` alert
  instead of the CRITICAL `unprotected_position` one used for an
  actual stop-loss failure.

Added regression tests (`tests/execution/test_execution_engine.py`,
`TestPlaceExitOrdersUnprotectedPositionAlert`) covering: stop-loss
success followed by take-profit success (both orders placed), a
genuine stop-loss failure after exhausting all 3 retries (fires
CRITICAL `unprotected_position`), and take-profit-only failure with a
live stop-loss (fires WARNING `take_profit_placement_failed`,
distinct from and not conflated with the CRITICAL case).

Verified: full suite (505 tests total) passes, --no-cov.

## [1.1.5] - Fixed broken pyproject.toml pushed directly to main

Commits `57b49c0`/`c22408e` were pushed directly to `main` (not through
this session) and stripped all version pins/lower-bounds from both
`pyproject.toml` and `requirements.txt`, and introduced a syntax error
in `pyproject.toml` (a stray `o` character prepended to
`[build-system]`, making the file invalid TOML — confirmed via
`tomllib.load()` failing with "Expected '=' after a key in a
key/value pair"). This would have made `pip install -e .` fail
outright for anyone pulling `main`.

Fixed only the syntax error (`o[build-system]` -> `[build-system]`)
per explicit instruction to leave the unpinned dependency style as-is
rather than restore this session's earlier version pins.

**Security note, stated plainly rather than silently accepted:** this
intentionally re-exposes the dependency graph to whatever the
unconstrained resolver picks up at install time, which reverses the
"Security patch pass" and "pyproject.toml lower bounds were still
vulnerable" fixes earlier in this CHANGELOG. Both of those entries
remain below as a historical record of exactly which versions were
confirmed to resolve the 45 original Dependabot alerts, in case
re-pinning becomes desirable later.

Verified: `pip install -e ".[dev]"` succeeds against the fixed,
unpinned manifests; full suite (804 tests) passes, `--no-cov`.

## [1.1.4] - pyproject.toml lower bounds were still vulnerable

### Follow-up to the requirements.txt security patch pass

After pushing the `requirements.txt`/`requirements-dev.txt` fix (prior
CHANGELOG entry, same day), GitHub's Dependabot alert count remained
at 45 with unchanged timestamps for several minutes past the push,
which is longer than typical re-scan latency. Investigated via the
GitHub API rather than assuming it was scan lag:

- Confirmed via `GET /repos/{owner}/{repo}/contents/requirements.txt`
  that the patched pins were genuinely live on the default branch.
- Confirmed via `GET /repos/{owner}/{repo}/dependency-graph/sbom` that
  the dependency graph still resolved `aiohttp` partly to `3.9.5`
  and partly to an open `>=3.10.5` constraint — neither reflecting the
  `requirements.txt` fix.
- Used the GitHub code-search API
  (`GET /search/code?q=aiohttp+repo:...`) to find every file in the
  repo referencing the package, which surfaced `pyproject.toml` as a
  second, independent manifest.

Root cause: `pyproject.toml`'s own `dependencies` list used open lower
bounds (e.g. `aiohttp>=3.10.5`) that were never updated alongside
`requirements.txt`. An open lower bound below the patched version does
not exclude vulnerable versions as a *valid* resolution for the
project's declared dependency graph — it happening to resolve safely
via `pip install -e .` today (because pip prefers the newest
compatible release) does not change what the manifest itself permits,
which is what Dependabot's scanner evaluates.

Fixed by raising the lower bound for all 7 affected packages in
`pyproject.toml` to match the exact patched versions: `aiohttp>=3.14.1`,
`cryptography>=46.0.6`, `orjson>=3.11.9`, `msgpack>=1.2.1`,
`python-dotenv>=1.2.2`, `scikit-learn>=1.8.0`, `lightgbm>=4.6.0`
(production), and `pytest>=9.1.1`, `black>=26.5.1` (dev extras).

Verified: `pip install -e ".[dev]"` succeeds with the tightened
bounds; installed versions confirmed to match the new floors exactly;
full suite (471 tests) passes, `--no-cov`.

Not yet independently re-verified: whether GitHub's Dependabot alert
count has actually dropped to 0 after this second fix, since alert
re-scan timing is outside this environment's control. This should be
checked again after this commit is pushed and given time to propagate.

## [1.1.3] - Correctness fix found during coverage work

### CorrelationTracker.matrix() zero-variance guard

Found while writing test coverage for `src/risk/portfolio_risk.py`
(not something searched for independently): a symbol with a constant
price series (e.g. a stale or illiquid feed reporting the same price
repeatedly) produces zero-variance log-returns. Passing these to
`np.corrcoef` triggers an internal divide-by-zero, which numpy
resolves to `NaN` but not before emitting a `RuntimeWarning`. This
project's own `pyproject.toml` sets `filterwarnings = ["error", ...]`
for pytest, so this warning would fail any test (or, in principle,
any code path with warnings-as-errors enabled) that exercised this
input shape, even though the final numeric result was already
correct.

Fixed in `src/risk/exposure.py`'s `CorrelationTracker.matrix()`: added
an explicit `np.std(a) == 0.0 or np.std(b) == 0.0` check before calling
`np.corrcoef`, returning `NaN` directly and consistent with the
existing "insufficient data -> NaN" contract, rather than letting
numpy raise the warning internally.

Added regression tests (`tests/risk/test_exposure.py`,
`TestCorrelationTrackerMatrixZeroVariance`) covering both symbols
constant and one-constant-one-varying, asserting the call does not
raise/warn and that both matrix cells are `NaN`.

Verified: full suite passes (--no-cov), and this specific input shape
no longer raises under pytest's strict warning filter.

## [1.1.2] - Security patch pass

### Dependency vulnerability remediation

Triggered by 45 open Dependabot alerts (8 high, 20 moderate, 17 low)
reported against `requirements.txt` / `requirements-dev.txt` after the
v1.1.1 push. Fetched the real alert list via the GitHub Dependabot API
(`GET /repos/{owner}/{repo}/dependabot/alerts`) rather than working from
the summary count alone; all 45 alerts collapsed to 8 distinct packages.

Root cause: `pyproject.toml`'s dependency list uses open lower-bound
constraints (`>=`), so `pip install -e .` was already resolving to
patched versions in every environment tested this session. However,
`requirements.txt` and `requirements-dev.txt` use hard `==` pins that
had drifted to older, vulnerable versions. This mattered in practice,
not just for the Dependabot scan: `Dockerfile` installs directly from
`requirements.txt`, so the production container build path was
genuinely pulling vulnerable dependency versions.

Bumped, each to the exact version already verified installed and
passing the full test suite in this environment (not simply the latest
PyPI release, to avoid introducing unverified upgrade risk):

- `aiohttp` 3.10.5 -> 3.14.1 (31 alerts, including GHSA-6mq8-rvhq-8wgg
  [high]; remainder low/medium — see PR/commit for the full GHSA list)
- `cryptography` 42.0.8 -> 46.0.6 (5 alerts: GHSA-537c-gmf6-5ccf [high],
  GHSA-r6ph-v2qm-q3c2 [high], GHSA-m959-cc7f-wv43,
  GHSA-79v4-65xg-pq4g, GHSA-h4gh-qq45-vh27)
- `orjson` 3.10.7 -> 3.11.9 (GHSA-hx9q-6w63-j58v [high])
- `msgpack` 1.1.0 -> 1.2.1 (GHSA-6v7p-g79w-8964 [high])
- `python-dotenv` 1.0.1 -> 1.2.2 (GHSA-mf9w-mj56-hr94)
- `scikit-learn` 1.5.2 -> 1.8.0 (GHSA-jw8x-6495-233v)
- `lightgbm` 4.3.0 -> 4.6.0 (GHSA-2586-f3p4-hq84 [high])
- `pytest` 8.2.0 -> 9.1.1, `black` 24.4.0 -> 26.5.1 (dev-only;
  GHSA-6w46-j5rx-g56g, GHSA-3936-cmfr-pm3m [high])

Also fixed, found incidentally while re-installing from
`requirements-dev.txt` in a clean environment (not part of the
Dependabot report): `types-pytz==2024.1.0` was an invalid,
never-published version string (real releases carry a trailing
`.YYYYMMDD` build segment, e.g. `2024.1.0.20240203`), which made
`pip install -r requirements-dev.txt` fail outright in any environment
without a cached/stale copy already present. Corrected to
`types-pytz==2024.1.0.20240203`.

Verified before considering this resolved:
- `pip install --no-cache-dir -r requirements.txt` succeeds in a fresh
  virtualenv (the exact command `Dockerfile` runs)
- `pip install --no-cache-dir -r requirements-dev.txt` succeeds
- Full test suite: 359/359 passing against the patched dependency set

Not verified (explicitly, so this isn't overclaimed): the Docker image
itself was not built in this session — no Docker daemon was available
in the sandbox used to make this change. The `requirements.txt`
install step was verified in isolation instead, which is the part of
the Dockerfile these changes touch.

## [1.1.1] - Bug-fix pass

### Packaging & build verification (second pass)

Triggered by an attempt to actually build the project's distributable
artifacts (wheel, sdist, Docker image). Findings below are from running
real builds/installs and diffing exact dependency-pinned test runs against
each other — not from static inspection.

- **[Critical, previously undetected] `PathConfig.model_dir` collides with
  Pydantic's reserved `model_` protected-namespace prefix**
  (`src/core/config.py`), raising a `UserWarning` on every settings load.
  This bug pre-dates this changelog entry and was NOT caught by the
  "Bug-fix pass" above: an earlier `pip install -e ".[dev]"` in that
  session used `pyproject.toml`'s `>=` floor constraints rather than
  `requirements.txt`'s exact pins, which silently resolved
  `pydantic-settings` to a newer version whose behavior happened to mask
  this warning during that session's test runs. Rebuilding a venv from
  the *exact* `requirements.txt` pins reproduced it immediately. Fixed by
  renaming the field to `models_dir` (keeping the `MODEL_DIR` env alias
  unchanged, so no deployed configuration needs to change) and adding
  `protected_namespaces=()` to the model config.
- **[Python 3.13 compatibility] 10 of 33 pinned runtime dependencies in
  `requirements.txt` had no published wheel for Python 3.13** — the
  Dockerfile's own base image (`python:3.13-slim`) — meaning
  `pip install -r requirements.txt` would fail to build the Docker image
  at all. Verified empirically per-package via
  `pip download --python-version 313 --only-binary=:all:` against
  multiple manylinux platform tags (a naive single-platform-tag check
  produces false positives, e.g. `lightgbm` initially appeared
  incompatible but was not — it ships an ABI-independent universal
  wheel). Each of the 10 affected packages was bumped to the *minimum*
  version adding a Python 3.13 wheel, not the latest release:
  `aiohttp` 3.9.5→3.10.5, `asyncpg` 0.29.0→0.30.0, `numpy` 1.26.4→2.1.0,
  `pandas` 2.2.2→2.2.3, `PyYAML` 6.0.1→6.0.2, `scikit-learn` 1.4.2→1.5.2,
  `scipy` 1.13.0→1.14.1, `orjson` 3.10.3→3.10.7, `msgpack` 1.0.8→1.1.0,
  `uvloop` 0.19.0→0.21.0, `matplotlib` 3.8.4→3.9.2. `pyproject.toml`'s
  dependency floors were raised to match. The full test suite (371 tests)
  was run against both the original and the bumped pin sets, in separate,
  independently-built venvs, with `RuntimeWarning` and `UserWarning`
  promoted to hard errors: **results were identical on both** — same
  371/371 pass, and direct numeric spot-checks (VaR/CVaR/Omega ratio,
  RandomForest predictions and `feature_importances_`) produced
  bit-for-bit identical output across the numpy 1.26.4→2.1.0 and
  scikit-learn 1.4.2→1.5.2 boundaries for this codebase's usage patterns.
  `pandas` is declared as a dependency but is not actually imported
  anywhere in `src/`, so its bump carries no behavioral risk here.
- **[Security hardening] `Dockerfile` rebuilt as a genuine multi-stage
  build**: previously `gcc`/`libpq-dev` (needed only to compile `asyncpg`
  from source when no prebuilt wheel existed) were installed in a `base`
  stage that every subsequent stage — including the final, shipped `app`
  image — inherited from, meaning the production container carried a C
  compiler and dev headers unnecessarily. Verified empirically that with
  the version bumps above, **zero** packages in `requirements.txt` require
  a source build on Python 3.13
  (`pip install --only-binary=:all: -r requirements.txt` succeeds
  end-to-end). The Dockerfile now builds a venv in a discarded `builder`
  stage (which still installs `gcc`/`libpq-dev` as defense-in-depth
  against a future dependency regressing to source-only), then copies
  just the built venv into a clean runtime stage that never installs a
  compiler at all.
- Wheel and sdist rebuilt against the corrected pins; both verified via
  `twine check` (PASSED) and by installing into two independent, fully
  clean venvs (`pip install dist/*.whl` and `pip install dist/*.tar.gz`)
  and confirming every fixed module imports correctly from the installed
  package, including a direct check that `PathConfig().models_dir`
  resolves as expected.

### Known limitations of this pass
- **The Docker image itself was not actually built.** This sandbox has no
  `docker`/`podman`/`buildah` binary and Docker Hub is not reachable
  through its network allowlist, so `docker build` could not be run. The
  Dockerfile changes above are verified by: (a) confirming every `COPY`
  source path exists, (b) confirming zero packages need a source build
  against the exact pins, (c) manually simulating the venv-build-then-copy
  mechanism outside Docker (build a venv, copy it, set `PATH`, confirm it
  runs) and checking that venv scripts embed absolute paths that must
  match between stages, which they do here. **Please run an actual
  `docker build .` before deploying this image** — that step has not been
  performed or verified.
- All dependency compatibility testing above ran on Python 3.12.3 (the
  actual Python 3.13 wheels were downloaded and confirmed to exist on
  PyPI, but their contents were not executed on a real 3.13 interpreter,
  which was not obtainable in this environment). The wheel-existence
  check is strong evidence of installability; it is not proof of runtime
  correctness on 3.13 specifically.
- `numpy` 1.26→2.1 and `scikit-learn` 1.4→1.5 are the two version bumps
  with the most inherent risk (a major version and a substantial minor
  version respectively). Both were spot-checked with direct numeric
  comparisons in addition to the test suite, but a spot-check is not
  exhaustive — any code path not exercised by the existing 371 tests
  could theoretically behave differently and was not separately audited.

All items below were found via test failures, targeted static analysis
(mypy strict, ruff `RUF006`/`B905`/`S110`), or manual inspection guided by
those signals — then confirmed against the actual runtime behavior before
being changed, and covered by new regression tests. Full suite: 371/371
passing, including under `-W error::RuntimeWarning`.

### Fixed
- **[Critical/financial-safety] Bybit order-type mapping**
  (`src/exchange/adapters/bybit/parser.py`): `to_bybit_order_type()` mapped
  plain `OrderType.LIMIT` to `"Market"` instead of `"Limit"` — a limit
  order sent to Bybit would have had its price protection silently
  bypassed. Only `STOP_LIMIT`/`TAKE_PROFIT_LIMIT` were previously handled
  correctly. Now all three `*_LIMIT` types map to `"Limit"`.
- **[ML signal quality] Dead liquidity-sweep feature**
  (`src/machine_learning/features.py`): `extract_features()` read
  `getattr(pool, "swept", False)` but `LiquidityPool`'s real field is
  `is_swept` — the `has_liquidity_sweep` feature was silently always 0.0
  for every signal ever scored by the ML filter, regardless of actual
  sweep state.
- **[Type correctness] `extract_features` signature lied about its
  contract**: the `analysis` parameter was typed as non-optional
  `SMCAnalysis`, but the implementation was already fully and correctly
  degrading every analysis-derived feature to a safe default when
  `analysis=None` (exactly as `SignalFilter.should_trade()`'s docstring
  documents). The annotation now reads `SMCAnalysis | None`, matching the
  real, tested behavior and restoring type-checking's ability to catch
  genuine `None`-handling bugs here in future.
- **[Data integrity] `CorrelationTracker.returns()`**
  (`src/risk/exposure.py`): `np.log()` on non-positive prices (corrupted
  market data / bad feed values) silently produced NaN/-inf that poisoned
  the correlation matrix used for risk-scaled position sizing. Now
  detects and drops non-positive prices with a warning log before taking
  the log.
- **[Reliability] Dangling asyncio task in `EventBus.publish_sync()`**
  (`src/core/events.py`, ruff `RUF006`): the task scheduled via
  `loop.create_task()` held no reference, so it could be garbage-collected
  mid-execution and any exception it raised was only ever logged (if at
  all) at destruction with no way to observe it. Tracked in a
  `_background_tasks` set with a done-callback that reaps completed tasks
  and surfaces exceptions via `EventBus.stats["errors"]`.
- **[Observability] Silent failure in `HeartbeatService`**
  (`src/monitoring/heartbeat.py`): a broken caller-supplied context
  function (`add_context`) failed with `except Exception: pass` — forever,
  invisibly, in the one component meant to surface operational problems.
  Now logged as a warning each beat; other context functions and the
  heartbeat loop itself are unaffected.
- **[Reliability] Overbroad exception suppression in `cancel_and_wait()`**
  (`src/utils/async_utils.py`): `except (asyncio.CancelledError, Exception):
  pass` silently swallowed *any* exception raised while a cancelled task
  unwound, not just the expected `CancelledError` — hiding real bugs in
  cleanup code at the worst possible time (shutdown). Now only suppresses
  `CancelledError`; anything else is logged and re-raised.
- **[Packaging] Invalid PEP 517 build backend** (`pyproject.toml`):
  `build-backend = "setuptools.backends.legacy:build"` does not exist in
  any setuptools release and broke every editable/PEP 517 install.
  Corrected to `setuptools.build_meta`.
- **[Tooling] Deprecated ruff configuration**: top-level `select`/`ignore`/
  `unfixable`/`per-file-ignores` moved under `[tool.ruff.lint]`
  (deprecated keys would stop working on a future ruff release); removed
  the now-nonexistent `ANN101`/`ANN102` rule codes from `ignore`.
- **[Defense-in-depth] `zip()` without `strict=`** (ruff `B905`) in three
  call sites; the one in `src/machine_learning/signal_filter.py` was a
  genuine silent-mismatch risk (a stale/schema-drifted model's
  `feature_importances_` could be mis-attributed to the wrong feature
  names with no error); the other two were length-invariant but hardened
  anyway.
- **[Type correctness] `Decimal.as_tuple().exponent` false-positive mypy
  errors** (`src/domain/instrument.py`): narrowed with an explicit
  `assert isinstance(exponent, int)` documenting why the NaN/Infinity
  sentinel case is impossible here (Pydantic validates `tick_size`/
  `step_size` as finite positive `Decimal` via `Field(gt=0)`).
- **[Readability] Shadowed variable across incompatible types**
  (`src/bootstrap/exchange_factory.py`): a mypy strict-mode false positive
  (not a runtime bug — the two branches are independent) caused by reusing
  the name `cfg` for both `BinanceConfig` and `BybitConfig` in different
  branches. Renamed to `binance_cfg`/`bybit_cfg`.

### Added
- `tests/unit/test_events.py` — `EventBus` had no dedicated test file;
  10 new tests including regression coverage for the `publish_sync`
  dangling-task fix.
- `tests/unit/test_heartbeat.py` — `HeartbeatService` had no test
  coverage; 7 new tests including regression coverage for the silent
  context-function-failure fix.
- `tests/unit/test_async_utils.py` — `src/utils/async_utils.py` had no
  test coverage; 7 new tests including regression coverage for the
  `cancel_and_wait` exception-suppression fix.
- `tests/unit/test_ml_features.py` — `src/machine_learning/features.py`
  had no test coverage; 17 new tests covering feature-vector shape,
  finiteness, the `None`-analysis degradation path, and per-feature
  correctness against constructed `SMCAnalysis` fixtures (this is what
  caught the `has_liquidity_sweep` bug above).

### Known limitations of this pass
- Verified on Python 3.12.3 (the project targets `>=3.13`; 3.13 was not
  obtainable in the environment this pass ran in — not installable via
  apt or any allowed package mirror). No 3.13-exclusive syntax is used
  anywhere in `src/`, but this was not verified by actually running on
  3.13. `requires-python` in `pyproject.toml` has been left at `>=3.12`
  to reflect what was actually verified rather than restating an
  unverified `>=3.13` claim; please re-run the full suite on 3.13 before
  relying on that constraint.
- mypy strict mode still reports a substantial number of findings beyond
  what's listed above (missing third-party stubs, `no-any-return` on
  intentionally-dynamic returns, etc.). Only findings that indicated an
  actual bug or a genuine false-positive were addressed this pass, per
  the stated priority of correctness over exhaustive style conformance.
  A full mypy-clean pass was not attempted and should not be assumed.
- ruff reports ~900 remaining findings on `src/`, the large majority of
  which are style/annotation/docstring-format rules (magic values,
  `ANN401`, import-sorting preferences) rather than correctness issues.
  This pass triaged and fixed the categories most likely to hide real
  bugs (`RUF006`, `B905`, `S110`/`SIM105`) rather than doing a blanket
  `ruff --fix`.

## [1.1.0] - Senior-quant hardening pass

### Added
- **Multi-exchange support**: `src/exchange/adapters/bybit/` — a full
  `IExchangeClient` REST/market-data/order-routing implementation for
  Bybit V5 linear perpetuals (auth, parser, REST client), selectable via
  `EXCHANGE_PROVIDER=bybit` and `bootstrap/exchange_factory.py`. The live
  user-data WebSocket path remains Binance-specific — see `ROADMAP.md`.
- **ML signal filter wiring**: the previously-scaffolded
  `machine_learning.SignalFilter` is now an optional gate in
  `SMCStrategy.on_candle()` (`src/main.py`), toggled via
  `configs/strategy.yaml: ml_filter` (disabled by default — no trained
  model ships with the repo).
- **Portfolio-level risk engine** (`src/risk/portfolio_risk.py`,
  previously a stub): correlation-adjusted position sizing
  (`correlation_adjusted_size`), portfolio-level VaR/CVaR via the full
  covariance matrix (`portfolio_var_cvar`), and inverse-volatility
  risk-parity capital weights (`risk_parity_weights`), all built on the
  existing `CorrelationTracker`.
- **Advanced quant metrics** (`src/analytics/advanced_metrics.py`):
  probabilistic Sharpe ratio, deflated Sharpe ratio (corrects for the
  number of backtest/parameter-search trials — the "tried 200 confluence
  weightings, kept the best one" problem), Omega ratio, tail ratio, Ulcer
  index, historical and parametric VaR/CVaR.
- **Real-time dashboard**: `src/monitoring/dashboard.py` adds a
  WebSocket push feed (`Dashboard.start_ws()`) alongside the existing
  HTTP polling endpoints, now wired into `bootstrap/startup.py` /
  `shutdown.py` / `bootstrap/app.py` (the dashboard was previously built
  but never instantiated by the running application).
- **Redis Streams event bus** (`src/core/events_redis.py`):
  `RedisEventBus` mirrors every published event onto a Redis Stream for
  other processes to tail (`RedisEventConsumer`), opt-in via
  `EVENT_BUS_BACKEND=redis`; in-process dispatch semantics are unchanged.
- Tests: `tests/unit/test_advanced_metrics.py`,
  `tests/risk/test_portfolio_risk.py`, `tests/exchange/test_bybit_adapter.py`.

### Changed
- `EventLoopWatchdog` gained `is_alive()` / `age_seconds` for external
  health reporting (previously write-only via `ping()`).
- `configs/strategy.yaml`, `configs/exchanges.yaml`, `.env.example`
  updated for the new `ml_filter`, Bybit, dashboard-WS, and event-bus
  settings.

### Known limitations introduced by this pass (tracked in ROADMAP.md)
- `correlation_adjusted_size()` is available but not yet called from
  `RiskManager`'s order-validation path.
- Bybit adapter has no private WebSocket client yet (REST/market-data/
  order-routing only); `Application` still assumes Binance for the live
  user-data stream.
- No `scripts/train_signal_filter.py` ships yet, so `ml_filter.enabled`
  has nothing to load until a model is trained.

  (All three resolved in later `[Unreleased]` passes above — see
  "Correlation-adjusted position sizing wired in", "Re-applied dropped
  exchange-provider/portfolio/backtesting fixes", and
  "ML signal filter training script + backtest signal_id propagation
  fix" respectively. Left here rather than edited in place so this
  entry still accurately reflects what v1.1.0 shipped with at the time.)

## [1.0.1]

### Fixed
- **`SignalFilterTrainer.cross_validate()` AUC scorer regressed to silent
  NaN**: `make_scorer(roc_auc_score, needs_proba=True)` used the
  `needs_proba` kwarg scikit-learn removed in 1.6 (this project's sandbox
  now runs 1.8.0). It doesn't raise — `make_scorer` still accepts
  arbitrary kwargs — so every CV fold's AUC silently scored as `NaN`
  instead of erroring. This is the same fix noted as applied in a prior
  session for `trainer.py`'s `cross_validate()` (see the 1.6+
  `response_method` note in earlier changelog entries) that had reverted;
  replaced with `response_method="predict_proba"`. Verified against a
  smoke test: pre-fix, CV AUC was `nan` on every fold; post-fix, real
  scores.
- Removed the obsolete `use_label_encoder=False` kwarg from
  `SignalFilterTrainer._default_estimator()`'s `XGBClassifier`
  construction. Unnecessary since xgboost 1.3, deprecate-warns as of
  1.7, and `use_label_encoder=True` raises outright since 1.6 — dead
  flag left in only as warning noise / a future break.

### Added
- **`SignalFilterTrainer.tune()`** — Optuna-based hyperparameter search
  (TPE sampler) for the signal-filter classifier, scored via
  `TimeSeriesSplit` cross-validation so tuning respects the same
  no-look-ahead constraint as the rest of the system. Mirrors the
  existing pattern in `src/backtesting/optimization.py`. Updates the
  trainer's estimator in place to an unfitted clone configured with the
  best-found params; `train()` still does the actual fit, so the caller
  controls what data the final model sees. Default search space is
  chosen automatically depending on whether XGBoost or the sklearn
  `GradientBoostingClassifier` fallback is active (their param names
  don't fully overlap). Folds with a single class present are skipped
  rather than allowed to silently distort a trial's score.
- `scripts/train_signal_filter.py` gained `--tune`, `--tune-trials`
  (default 50), and `--tune-metric` (`auc`/`f1`) flags. When `--tune` is
  set, hyperparameter search runs on the training split before the
  reported cross-validation and final fit, so the CV numbers logged
  reflect the tuned configuration. Requires `pip install optuna`; not a
  new hard dependency of `src/machine_learning/` itself, matching how
  `optimization.py` already treats optuna as optional.

### Added
- **Test coverage — `bootstrap` (previously 0% covered)**: full suites for
  every module in `src/bootstrap/`:
  - `tests/bootstrap/test_exchange_factory.py` — provider selection
    (binance/bybit), `ConfigurationError` guards when the matching
    sub-config is missing, unknown-provider rejection, for both
    `build_rest_client` and `build_ws_client`.
  - `tests/bootstrap/test_dependency_container.py` — `wire_container()`
    registers every documented component name/singleton; includes a
    regression test documenting that it is **not** idempotent
    (`ComponentRegistry.register()` raises `ValueError` on a second call
    against the same registry, since `overwrite` defaults to `False`).
  - `tests/bootstrap/test_lifecycle.py` — `LifecycleManager.run()`
    start/shutdown ordering, shutdown-still-runs-if-startup-raises,
    signal handler installation (SIGINT/SIGTERM), the watchdog-ping main
    loop, and the reconciliation scheduler loop (interval firing, error
    isolation, `CancelledError` propagation).
  - `tests/bootstrap/test_shutdown.py` — full teardown sequence:
    `SystemShutdownEvent` published first, orchestrator/engine/scheduler/
    order-cancellation/monitoring/websocket/REST/database steps, listen-key
    cleanup ordering (after WS stop, non-fatal on failure), optional
    position-closing, and error isolation between every step (one step
    failing must not block the rest).
  - `tests/bootstrap/test_startup.py` — full startup sequence including
    the production live-trading equity guard (raises on
    zero/negative equity, no-op in paper), binance-only listen-key
    creation vs. bybit's signed-WS path, `AlertFiredEvent` →
    `Notifier.send()` and `PositionClosedEvent` →
    `Notifier.send_trade()` event-bus wiring, scheduled-task
    registration, and the Prometheus/dashboard monitoring toggles.
  - `tests/bootstrap/test_app.py` — real `Application` construction from
    a minimal `Settings` object: binance/bybit client selection,
    account-service selection, correlation-tracker gating (only
    constructed for 2+ enabled symbols), notification-channel wiring for
    all four channels (telegram/discord/slack/email, including each
    channel's "only added when fully configured" guard),
    `sync_equity()`'s binance/bybit branching, dashboard data providers,
    and `create_application()` (logging configured first, redis vs.
    in-memory event-bus backend selection).
- **Test coverage — `src/monitoring/metrics.py` and
  `src/monitoring/telemetry.py`** (previously 0% covered):
  - `tests/monitoring/test_metrics.py` — every Prometheus metric's type
    and label set, that counters/gauges/histograms are actually usable
    (`.inc()`/`.set()`/`.observe()`), and `start_metrics_server()`.
  - `tests/monitoring/test_telemetry.py` — `configure_logging()`'s
    JSON-vs-console renderer selection, log-level application (including
    the silent fallback to INFO on an invalid level string), noisy
    third-party logger silencing, and `get_logger()`.

### Known gap (re-tracked)
- Test suites for `src/monitoring/{alerts,dashboard,health,watchdog}.py`,
  `src/notifications/*`, and `src/machine_learning/{trainer,signal_filter}.py`
  were written in a prior session but are again absent from this
  archive — see the recurring "fixes/tests not persisting to GitHub
  main" issue tracked across sessions. `tests/unit/test_heartbeat.py`
  is the only monitoring-adjacent suite that survived. Re-writing these
  is the next pending item; verifying they land in `main` (not just a
  local working tree) matters more than re-writing them once more.

## [1.0.0] - Initial Release

### Added
- Core architecture: async event bus, DI container, component registry,
  Pydantic-based configuration with environment + YAML layering.
- Domain layer: `Order`, `Position`, `Trade`, `Signal`, `Account`,
  `Portfolio`, `Instrument`, `Candle`, `Balance`, `Leverage` value objects
  and entities, all using `Decimal` for financial precision.
- Binance USDT-M Futures exchange integration: REST client (HMAC-signed),
  WebSocket streaming client with auto-reconnect, exchange-info/instrument
  parsing, order/account services.
- Market data layer: in-memory candle cache with TTL, candle service with
  warm-up + live subscription, market data feed publishing to the event
  bus.
- Technical indicators: ATR, EMA, SMA, RSI, MACD, Bollinger Bands, volume
  indicators (OBV, VWAP, relative volume), momentum (Stochastic, Williams
  %R, ROC).
- Smart Money Concepts engine: market structure (swing detection, HH/HL/
  LH/LL classification), BOS, CHoCH, order blocks (with mitigation
  tracking), fair value gaps (with fill tracking), liquidity pools (equal
  highs/lows, swing liquidity, sweep detection), inducement detection,
  premium/discount zones with OTE, trading sessions and ICT kill zones,
  weighted confluence scoring engine.
- Strategy layer: `SMCSignalEngine` orchestrating the full SMC pipeline,
  multi-timeframe context, strategy orchestrator bridging market data
  events to strategies, entry/exit logic, signal filters.
- Risk management: central `RiskManager` pre-trade gate, fixed-fractional
  and Kelly position sizing, circuit breaker with cooldown, drawdown
  tracker, stop-loss/take-profit/trailing-stop/breakeven calculators,
  margin and liquidation-price calculations, portfolio exposure tracking.
- Execution layer: `ExecutionEngine` (signal → order → position
  lifecycle), `OrderManager` (idempotent submission, exchange bridge),
  `PositionManager` (position tracking, trade generation), fill engine
  (processes Binance user-data-stream execution reports), exchange
  reconciliation service, retry helper.
- Persistence: async SQLAlchemy 2.0 + PostgreSQL, repository pattern,
  ORM models and repositories for trades, orders, signals, metrics.
- Analytics: comprehensive performance report (win rate, profit factor,
  expectancy, Sharpe/Sortino/Calmar ratios, drawdown, streaks, duration
  stats), R-multiple and System Quality Number analysis.
- Backtesting: event-driven engine with strict anti-look-ahead-bias
  guarantees (next-bar-open fills), commission and slippage models,
  Optuna-based walk-forward parameter optimization.
- Paper trading: simulated broker driven by live market data, simulated
  fill engine.
- Monitoring: Prometheus metrics registry, structured logging
  (structlog), health checker, heartbeat service, event-loop watchdog,
  alert manager with cooldowns, lightweight HTTP status dashboard.
- Notifications: Telegram, Discord, Slack, and SMTP email channels behind
  a unified `Notifier` fan-out dispatcher.
- Infrastructure: Dockerfile (non-root, multi-stage), `docker-compose.yml`
  (app, PostgreSQL, Redis, Prometheus, Grafana), `Makefile`, full
  `pyproject.toml` with ruff/mypy/pytest configuration, CLI scripts for
  backtesting, optimization, migrations, data export, and health checks.
- Test suite: unit tests for indicators, SMC modules, domain models, risk
  calculations, analytics, and core infrastructure; integration tests for
  market data cache, the SMC signal engine pipeline, the risk manager
  validation gate, and execution-layer components.

### Known Limitations
See [DESIGN.md](DESIGN.md#known-limitations).
