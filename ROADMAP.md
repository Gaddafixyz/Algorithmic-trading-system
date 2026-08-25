# Roadmap

## Near-term (correctness & robustness)

- [x] Implement Binance user-data-stream listen-key renewal (required every
      60 minutes) for unattended live operation.
      → `exchange/binance/rest.py` (`create_listen_key`, `renew_listen_key`,
        `delete_listen_key`); `exchange/binance/websocket.py`
        (`set_renewal_callback`, `_renewal_loop`) runs a background task every
        `BINANCE_LISTEN_KEY_REFRESH_MINUTES` (30) minutes.

- [x] Populate funding-rate accrual in the backtest engine from historical
      funding-rate data (currently only tracked structurally on `Position`).
      → `backtesting/simulator.py` (`FundingRate` dataclass, `_apply_funding_rates`);
        `backtesting/engine.py` `BacktestConfig.funding_rates` field.
        Pass a `list[FundingRate]` from `rest.get_funding_rate_history()` to
        `BacktestConfig` and the broker applies each 8-hour settlement to
        open positions as candles advance.

- [x] Add partial-fill and order-book-depth modeling to the backtest
      simulator for more realistic large-order fill assumptions.
      → `backtesting/simulator.py` (`PartialFillModel` dataclass); fills are
        capped at `volume_participation × bar_volume`; the remainder is
        re-queued for the next bar.  Pass `partial_fill=PartialFillModel()`
        to `BacktestConfig`.  Set `enabled=False` to restore legacy behaviour.

- [x] Empirically validate and tune the default SMC confluence scoring
      weights (`smart_money/confluence.py`) via `scripts/optimize.py`
      against multiple symbols/timeframes; current weights are reasoned
      defaults, not backtested.
      → `smart_money/confluence.py::ConfluenceWeights` makes the 8 factor
        budgets tunable (previously hardcoded literals scattered through
        `score_confluence()`); `.normalized()` keeps `min_score`/
        `classify_strength()` meaningful regardless of the raw values
        passed in. Threaded through `SMCSignalEngine` → `SMCStrategy` →
        `configs/strategy.yaml`'s `scoring_weights` block, which existed
        in the config file already but had **no reader anywhere** —
        editing it silently did nothing until this pass.
        `scripts/optimize.py` gained `--tune-weights` (adds all 8 to the
        Optuna search space alongside `min_score`), `--symbols`/
        `--timeframes` for multi-combination sweeps (fixed a real latent
        bug along the way: the old single-combo script hardcoded
        `symbols=["BTCUSDT"], timeframes=[Timeframe.H1]` inside
        `strategy_factory` regardless of the `--symbol`/`--timeframe`
        args, so e.g. `--symbol ETHUSDT` silently backtested zero trades
        every run), and a global-vs-per-symbol weights validation pass
        (`_run_global_weights_validation`) that checks whether one
        shared weight set generalizes across symbols/timeframes or each
        needs its own. Regression tests in
        `tests/strategy/test_confluence.py::TestConfluenceWeights` /
        `TestScoreConfluenceWithCustomWeights` confirm the defaults
        reproduce the original hardcoded scoring exactly.
        **Still open:** the actual empirical run — pulling real history
        across several symbols/timeframes and reviewing whether tuned
        weights should replace the shipped defaults — needs network/
        market-data access this sandbox doesn't have. The tooling to do
        that run is now complete; running it and deciding whether to
        adopt its output is on you.
- [x] Add Alembic migration scripts (currently only `create_all` from ORM
      models is implemented; `persistence/migrations.py::run_migrations`
      expects an `alembic/` directory that needs to be generated).
      → `src/persistence/alembic/` (env.py, script.py.mako, versions/);
        `alembic.ini` at project root; initial migration `001_initial_schema.py`
        creates all v1.0.0 tables with proper indexes.

## Medium-term (capability expansion)

- [x] Multi-symbol portfolio correlation tracking (`risk/exposure.py` has
      the scaffolding; needs a correlation matrix from price history).
      → `risk/exposure.py` `CorrelationTracker`: rolling log-return
        correlation matrix, `highest_correlated_pair()`, and
        `diversification_ratio()` helper.

- [x] Machine-learning signal filter (`machine_learning/` package
      currently scaffolded but not implemented) — train a classifier on
      historical SMC setups to filter low-quality confluence signals.
      → `src/machine_learning/` package:
        `features.py` (30-feature extraction from `SMCAnalysis` + `Signal`),
        `signal_filter.py` (`SignalFilter` wrapping any sklearn classifier),
        `trainer.py` (`SignalFilterTrainer` with XGBoost default, time-series
        cross-validation, save/load). **v1.1.0:** wired into
        `SMCStrategy.on_candle()` (`src/main.py`) as an optional post-score
        gate, toggled via `configs/strategy.yaml: ml_filter`; disabled by
        default since no trained model ships with the repo.

- [x] Multi-exchange support (currently Binance-only); the
      `IExchangeClient` protocol in `core/interfaces.py` is designed to
      support additional exchange adapters.
      → **v1.1.0:** `src/exchange/adapters/bybit/` implements the full
        `IExchangeClient` REST/market-data/order-routing contract for
        Bybit V5 linear perpetuals, selectable via `EXCHANGE_PROVIDER=bybit`
        (`bootstrap/exchange_factory.py`). Scope note: the live user-data
        WebSocket wiring in `bootstrap/startup.py` (listen-key renewal,
        `Application.rest_client`/`ws_client`) is still Binance-specific —
        Bybit's private WS uses a different auth handshake entirely, so
        `Application` isn't yet exchange-pluggable end-to-end. Remaining:
        wire a Bybit WS client and make `Application.__init__` branch on
        `exchange_provider` instead of asserting `settings.binance`.

- [x] WebSocket-based real-time dashboard (currently HTTP polling only).
      → **v1.1.0:** `src/monitoring/dashboard.py` now runs a `websockets`
        push server (`Dashboard.start_ws()`) alongside the existing HTTP
        endpoints, broadcasting a combined health/status/metrics snapshot
        every `broadcast_interval` seconds plus an immediate snapshot on
        connect. Configured via `DASHBOARD_WS_PORT` / `ENABLE_DASHBOARD_WS`.

## Long-term (scale & operations)

- [x] Kubernetes deployment manifests with horizontal scaling per symbol
      group (`deployments/kubernetes/` scaffolded).
      → `deployments/kubernetes/deployment.yaml` (Fargate-style pod spec,
        liveness/readiness probes, PVCs), `service.yaml`, `configmap.yaml`,
        `hpa.yaml` (CPU/memory autoscaler, scale-down stabilisation window).

- [x] Terraform infrastructure-as-code for cloud deployment
      (`deployments/terraform/` scaffolded).
      → `deployments/terraform/main.tf` (VPC, RDS PostgreSQL 16,
        ElastiCache Redis 7, ECS Fargate service, CloudWatch alarms,
        IAM roles, Secrets Manager integration),
        `variables.tf`, `outputs.tf`.

- [x] Replace in-process event bus with Redis Streams for multi-process/
      multi-host deployments, preserving the existing event schema.
      → **v1.1.0:** `src/core/events_redis.py` (`RedisEventBus`,
        `RedisEventConsumer`). `RedisEventBus` is an `EventBus` subclass —
        in-process dispatch is unchanged — that additionally mirrors every
        event onto a Redis Stream (`events:{EventType}`) for other
        processes to tail via a consumer group. Opt in with
        `EVENT_BUS_BACKEND=redis`. Scope note: this is fan-out/observability
        across process boundaries, not full distributed event-sourcing —
        no cross-process idempotency/ordering guarantees are added on top
        of what Redis Streams itself provides.
      → **Follow-up (this pass):** `RedisEventConsumer` had no caller
        anywhere in the codebase — the publish side (`RedisEventBus`) was
        wired into `create_application`, but the consume side this
        feature exists for was unimplemented, so the multi-process
        deployment this entry claims `[x]` for had no actual way to
        centralize notifications across processes. Added
        `scripts/notifications_worker.py`: a standalone process that
        subscribes via `RedisEventConsumer` and fans events out through
        the existing `Notifier`/channel classes (Telegram/Discord/Slack/
        Email), scalable independently of the trading engine via shared
        consumer groups.

- [x] Wire `fair_value_gap.py::fvg_at_price()` into exit management —
      implemented, correct, but had zero callers anywhere in `src/`.
      Required a strategy-behavior decision (target zones vs. reversal-risk
      signal vs. both) before implementing; decision was both. See
      `strategy/exit.py::evaluate_exit_actions()`'s new `fvg_target_zones`/
      `fvg_reversal_zones` params and `execution_engine.py`'s entry-time
      zone classification. Also surfaced and fixed a real bug along the
      way: `SignalGeneratedEvent` never carried `metadata`, so the FVG
      snapshot would have silently gone nowhere in the live system —
      same failure shape `atr`/`signal_id` were added to that event to
      fix previously.

## Newer follow-ons opened by this pass

- [x] Portfolio-level risk (`risk/portfolio_risk.py`, added in v1.1.0) —
      `correlation_adjusted_size()` is wired into
      `RiskManager.calculate_position_size()`, which runs in the
      pre-trade path (`ExecutionEngine` calls `validate_signal()` then
      `calculate_position_size()` for every signal — see
      `execution/execution_engine.py`). `Application.__init__` builds the
      `CorrelationTracker` and passes it into `RiskManager`;
      `RiskManager.start()` (invoked from `bootstrap/startup.py`)
      subscribes it to `CandleClosedEvent`. This item was already done
      when reviewed; the checkbox here was just stale. Covered by
      `tests/risk/test_risk_manager.py::TestRiskManagerCorrelationAdjustedSizing`.
- [x] Bybit private WebSocket client + `Application` exchange-provider
      branching — also already complete (`exchange_factory.py` branches
      both REST and WS client construction; `startup.py` skips the
      Binance-only listen-key flow for Bybit; `BybitWebSocket`'s private
      stream normalizes `execution`/`order` topics to the same
      `ORDER_TRADE_UPDATE` shape `FillEngine` expects from Binance).
      This checkbox was also stale — the gap found on review was test
      coverage, not implementation: added
      `tests/exchange/test_bybit_websocket.py` (auth signing, depth
      snapping, all four message normalizers).
- [x] `scripts/train_signal_filter.py` — added. Runs the strategy through
      `BacktestEngine`, captures each signal's `SMCAnalysis` snapshot at
      generation time (via `SMCSignalEngine.last_analysis`), joins it to
      closed-trade outcomes by `signal_id`, and trains/evaluates/saves a
      model via the existing `SignalFilterTrainer`, using a chronological
      (never shuffled) train/test split. Building this surfaced a real
      bug it depended on: `backtesting/simulator.py` was silently
      dropping `signal_id`/`strategy_id` when constructing `Position`/
      `Trade` (the live path in `execution_engine.py` already threaded
      these through correctly) — fixed, with a regression test
      (`TestSignalIdPropagation` in `tests/backtesting/test_simulator.py`).
- [x] Empirically validate and tune the default SMC confluence scoring
      weights (`smart_money/confluence.py`) via `scripts/optimize.py`
      against multiple symbols/timeframes; current weights are reasoned
      defaults, not backtested. **Correction to the note this item
      carried previously:** it was not purely an operational task.
      `scripts/optimize.py` only ever exposed `min_score` as tunable —
      the 8 confluence factor budgets were hardcoded literals with no
      path to tune them at all, and `configs/strategy.yaml` had a
      `scoring_weights` block that looked like it did something but was
      never read by any code. See the Near-term section above for what
      was actually built to close that gap. The remaining piece — an
      actual empirical run against real multi-symbol/timeframe history —
      is still operational and still needs Makaveli's local environment.

- [x] Wire `MTFContext` (`strategy/multi_timeframe.py`) — implemented,
      correct, but had zero callers anywhere in `src/`. Decision: wired as
      both a graded confluence-scoring factor and a hard pre-trade HTF
      confirmation filter. Two latent bugs in `MTFContext` itself fixed
      along the way (alignment-score denominator, fixed-vs-configured HTF
      mismatch) — see CHANGELOG.md [1.3.2].
- [x] LTF tier candle source (flagged as still-open by the item above) —
      wired end-to-end: `SMCSignalEngine`/`SMCStrategy`/`BacktestEngine`
      all gained `ltf_timeframe`/`ltf_candles` mirroring the existing HTF
      pattern exactly, `configs/strategy.yaml` gained `ltf_timeframe`, and
      the live feed-timeframe subscription in `main.py::async_main()` now
      actually adds it (the real candle source — everything upstream was
      inert without this). Also surfaced and fixed a second latent bug
      while touching the same call site: the HTF-alignment scoring
      factor's `alignment_score()` call wasn't using the explicit-`htf`
      override `[1.3.2]` added to `MTFContext` — only the hard filter
      was. See CHANGELOG.md [1.3.3].
