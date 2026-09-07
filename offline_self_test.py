from __future__ import annotations

import sys
import sqlite3
import tempfile
import types
import traceback
from pathlib import Path
from types import SimpleNamespace

try:
    import MetaTrader5  # noqa: F401
except ImportError:
    fake = types.ModuleType("MetaTrader5")
    fake.TIMEFRAME_M1 = 1
    sys.modules["MetaTrader5"] = fake

import numpy as np
import pandas as pd

import settings
import trading_machine as tm
import spartan_pro as spartan
import spartan_llm
import spartan_posttrade
import spartan_decision_memory as gpt_memory
import scalp_ai_policy as scalp_ai
import scalp_lab
import capital_growth
import micro_scalp_hunter as micro_hunter
import institutional_alpha as institutional_alpha
import v9_execution_first as v9_exec
import v9_2_adaptive_scalper as v92
import v10_superhuman_scalper as v10



def test_gpt_connection(run_network: bool = False) -> bool:
    """Offline by default; optionally perform the real API check when requested."""
    assert bool(getattr(settings, "SPARTAN_LLM_REVIEW_ENABLED", False))
    assert float(getattr(settings, "SPARTAN_MIN_CONFIDENCE", 0.0)) >= 0.70
    assert str(getattr(settings, "GPT_MODEL", "")).strip()
    source = Path(spartan_llm.__file__).read_text(encoding="utf-8")
    assert "client.responses.parse" in source
    assert "SYSTEM_PROMPT" in source
    assert 'Literal["confirm", "hold"]' in source
    assert '"store": False' in source
    if run_network:
        from gpt_connection_test import test_gpt_connection as live_test
        ok, message = live_test()
        print(message)
        return bool(ok)
    return True


def synthetic_bars(count: int = 2400) -> pd.DataFrame:
    rng = np.random.default_rng(260806)
    returns = rng.normal(0.00002, 0.0008, count)
    close = 2000.0 * np.exp(np.cumsum(returns))
    open_ = np.r_[close[0], close[:-1]]
    noise = np.maximum(0.05, np.abs(rng.normal(0.25, 0.08, count)))
    high = np.maximum(open_, close) + noise
    low = np.minimum(open_, close) - noise
    return pd.DataFrame(
        {
            "time": pd.date_range("2022-01-01", periods=count, freq="min", tz="UTC"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "tick_volume": rng.integers(20, 300, count),
            "spread": np.full(count, 20),
            "real_volume": np.zeros(count),
        }
    )


def insert_backtest_row(connection, strategy_id: int, symbol: str, score: float = 3.0) -> None:
    connection.execute(
        """
        INSERT INTO backtests(
            strategy_id, symbol, timeframe, bars, trades, wins, losses,
            win_rate, net_profit, return_pct, max_drawdown_pct,
            profit_factor, score, tested_at, metrics_json
        ) VALUES (?, ?, 'M1', 2400, 50, 28, 22, .56, 12, .04, .03, 1.25, ?, ?, '{}')
        """,
        (strategy_id, symbol, score, tm.utc_now()),
    )
    connection.execute(
        """
        INSERT INTO strategy_diagnostics(
            strategy_id, symbol, train_trades, train_profit_factor, train_score,
            oos_trades, oos_profit_factor, oos_score, stability_score,
            validation_reason, updated_at
        ) VALUES (?, ?, 35, 1.2, 3, 15, 1.1, 2, 2.5, 'test', ?)
        """,
        (strategy_id, symbol, tm.utc_now()),
    )


def main() -> int:
    original = {
        "DATABASE_PATH": settings.DATABASE_PATH,
        "REPORTS_DIR": settings.REPORTS_DIR,
        "DATA_DIR": settings.DATA_DIR,
        "LOGS_DIR": settings.LOGS_DIR,
    }
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="tm_parallel_test_") as tmp:
        root = Path(tmp)
        settings.DATABASE_PATH = root / "test.db"
        settings.REPORTS_DIR = root / "reports"
        settings.DATA_DIR = root / "data"
        settings.LOGS_DIR = root / "logs"
        try:
            tm._INITIALIZED_DATABASE_PATH = None
            tm.init_database()

            for regime in tm.REGIME_FAMILY_WEIGHTS:
                family = max(
                    tm.REGIME_FAMILY_WEIGHTS[regime],
                    key=tm.REGIME_FAMILY_WEIGHTS[regime].get,
                )
                params = tm.guided_strategy(family, regime, 0.003)
                assert params == tm.normalize_params(family, params)

            tm.prepare_symbol = lambda _symbol: SimpleNamespace(
                visible=True,
                point=0.01,
                trade_tick_value=1.0,
                trade_tick_size=0.01,
                volume_min=0.01,
                volume_max=100.0,
                volume_step=0.01,
                digits=2,
            )
            frame = tm.add_features(synthetic_bars(), {5, 12, 20, 45, 50})
            definition = tm.StrategyDefinition(
                strategy_id=1,
                symbol="TEST",
                family="trend",
                params=tm.normalize_params(
                    "trend",
                    {
                        "fast": 12,
                        "slow": 45,
                        "rsi_low": 45,
                        "rsi_high": 55,
                        "stop_atr": 1.2,
                        "take_atr": 2.0,
                        "max_hold": 60,
                        "dom_threshold": 0.0,
                    },
                ),
            )
            walk = tm.walk_forward_metrics(frame, definition, 300.0)
            assert len(walk["folds"]) >= 2

            with tm.db_connect() as connection:
                approved_id, _ = tm.insert_strategy(
                    connection, "TEST", "trend", definition.params
                )
                connection.execute(
                    "UPDATE strategies SET status='shadow_approved' WHERE id=?",
                    (approved_id,),
                )
                insert_backtest_row(connection, int(approved_id), "TEST", 5.0)

                historical_params = dict(definition.params)
                historical_params["fast"] = 13
                historical_id, _ = tm.insert_strategy(
                    connection, "TEST", "trend", historical_params
                )
                connection.execute(
                    "UPDATE strategies SET status='historical_validated' WHERE id=?",
                    (historical_id,),
                )
                insert_backtest_row(connection, int(historical_id), "TEST", 9.0)

                legacy_params = dict(definition.params)
                legacy_params["fast"] = 14
                legacy_id, _ = tm.insert_strategy(
                    connection, "TEST", "trend", legacy_params
                )
                connection.execute(
                    "UPDATE strategies SET status='needs_revalidation' WHERE id=?",
                    (legacy_id,),
                )
                insert_backtest_row(connection, int(legacy_id), "TEST", 7.0)
                connection.execute(
                    """
                    UPDATE strategy_diagnostics
                    SET oos_trades=25, oos_profit_factor=1.20,
                        stability_score=2.5, validation_reason='validated'
                    WHERE strategy_id=?
                    """,
                    (legacy_id,),
                )
                connection.commit()

                selected = tm.candidate_strategies(connection, "TEST", "trend")
                assert selected
                assert all(item[0].strategy_id == approved_id for item in selected)
                # The production V7 setting intentionally disables the legacy trial
                # bridge. Temporarily enable it only to regression-test that dormant
                # compatibility path without changing live execution policy.
                original_trial_bridge = settings.ENABLE_DEMO_TRIAL_BRIDGE
                settings.ENABLE_DEMO_TRIAL_BRIDGE = True
                try:
                    trial_selected = tm.demo_trial_candidate_strategies(
                        connection, "TEST", "trend"
                    )
                finally:
                    settings.ENABLE_DEMO_TRIAL_BRIDGE = original_trial_bridge
                assert any(item[0].strategy_id == legacy_id for item in trial_selected)
                tier_candidates, tier_name = tm.execution_candidate_set(
                    connection, "TEST", "trend"
                )
                assert tier_name == "shadow_approved"
                assert all(item[0].strategy_id == approved_id for item in tier_candidates)

                # V5.4 adaptive execution learning: one loss creates a persisted
                # setup cooldown, repeated wins recover confidence, and risk can
                # scale above 1x only after enough positive live evidence.
                demo_columns = {
                    row["name"] for row in connection.execute(
                        "PRAGMA table_info(demo_positions)"
                    ).fetchall()
                }
                assert {
                    "execution_tier", "setup_key", "context_json",
                    "learning_confidence", "risk_multiplier",
                    "mfe_r", "mae_r", "super_probability",
                    "super_decision_json", "sl_multiplier", "tp_multiplier",
                }.issubset(demo_columns)
                approved_def = tm.StrategyDefinition(
                    strategy_id=int(approved_id), symbol="TEST",
                    family="trend", params=definition.params,
                )
                loss_state = tm.update_adaptive_learning(
                    connection, int(approved_id), "TEST", "trend", 1,
                    -1.0, "offline_test", {"case": "loss"},
                )
                assert loss_state["observations"] == 1
                assert loss_state["blocked"]
                allowed, reason, gate = tm.adaptive_learning_gate(
                    connection, approved_def, "trend", 1, "shadow_approved",
                    {"buy_votes": 3, "sell_votes": 0, "buy_weight": 4.0, "sell_weight": 0.0},
                )
                assert not allowed and "cooldown" in reason.lower()
                assert tm.adaptive_risk_multiplier(loss_state, "shadow_approved") <= 1.0

                # Expire the single-loss cooldown, then feed enough winning live
                # outcomes to verify confidence/risk recovery.
                connection.execute(
                    "UPDATE adaptive_trade_memory SET blocked_until='2000-01-01T00:00:00+00:00' "
                    "WHERE strategy_id=? AND symbol='TEST' AND regime='trend' AND side=1",
                    (approved_id,),
                )
                recovery_wins = max(10, int(settings.LEARNING_RISK_UPSCALE_MIN_OBSERVATIONS) + 2)
                for _ in range(recovery_wins):
                    win_state = tm.update_adaptive_learning(
                        connection, int(approved_id), "TEST", "trend", 1,
                        1.2, "offline_test", {"case": "recovery"},
                    )
                assert win_state["confidence"] > settings.LEARNING_RISK_UPSCALE_MIN_CONFIDENCE
                learned_multiplier = tm.adaptive_risk_multiplier(
                    win_state, "shadow_approved"
                )
                assert learned_multiplier > 1.0
                # Broker-step sizing can therefore exceed 0.01 when equity and
                # stop-risk support it; it is not forced when they do not.
                tm.mt5.ORDER_TYPE_BUY = 0
                tm.mt5.ORDER_TYPE_SELL = 1
                tm.mt5.order_calc_profit = (
                    lambda _order_type, _symbol, volume, _entry, _stop: -100.0 * volume
                )
                learned_risk_fraction = min(
                    settings.MAX_DEMO_MIN_LOT_RISK_PCT,
                    settings.RISK_PER_TRADE * learned_multiplier,
                )
                learned_plan = tm.volume_plan(
                    "TEST", 1, 100.0, 99.0, 2000.0,
                    allow_minimum_bridge=True,
                    risk_fraction=learned_risk_fraction,
                    hard_ceiling_fraction=settings.MAX_DEMO_MIN_LOT_RISK_PCT,
                )
                assert learned_plan["volume"] > 0.01
                adaptive_adjustment, adaptive_meta = tm.adaptive_candidate_adjustment(
                    connection, int(approved_id), "TEST", "trend"
                )
                assert adaptive_meta["observations"] >= recovery_wins
                assert adaptive_adjustment > 0

                # V5.5 proactive layer: collective memory transfers learning,
                # Bayesian probability is bounded, market quality is measurable,
                # and the persistent online classifier can learn feature/outcome pairs.
                for reward in (1.0, 0.8, -0.5, 1.2, 0.6):
                    tm.update_collective_memory(
                        connection, "TEST", "trend", "trend", 1, reward, 1.0
                    )
                collective = tm.collective_memory_snapshot(
                    connection, "TEST", "trend", "trend", 1
                )
                assert collective["observations"] >= 5
                assert 0.05 <= collective["win_probability"] <= 0.95
                rolling = tm.rolling_setup_performance(
                    connection, int(approved_id), "TEST", "trend", 1, 20
                )
                bayes = tm.bayesian_loss_probability(win_state, rolling)
                assert 0.02 <= bayes <= 0.98

                market = tm.market_quality_metrics(
                    frame, float(frame.iloc[-2]["atr_14"]), 0.01,
                    {
                        "available": True, "imbalance": 0.20,
                        "persistence": 0.15, "change": 0.02, "score": 0.18,
                    },
                )
                assert 0.05 <= market["hurst"] <= 0.95
                assert 0.0 <= market["entropy"] <= 1.0
                assert 0.0 <= market["drift"] <= 1.0

                super_features = {name: 0.0 for name in tm.SUPER_FEATURE_NAMES}
                super_features.update({
                    "side": 1.0, "rsi14": 0.2, "trend_strength": 1.0,
                    "adaptive_confidence": 0.75, "bayes_loss": 0.30,
                    "collective_win_prob": 0.65, "ensemble_dominance": 2.0,
                    "family_trend": 1.0, "regime_trend": 1.0,
                })
                for _ in range(settings.SUPERLEARNER_MIN_ONLINE_UPDATES + 4):
                    tm.update_online_model(
                        connection, "TEST", super_features, 1.0, 1.0
                    )
                prediction = tm.online_model_probability(
                    connection, "TEST", super_features
                )
                assert prediction["active"]
                assert prediction["probability"] > 0.50
                model_count = connection.execute(
                    "SELECT COUNT(*) AS n FROM superlearner_model_state"
                ).fetchone()["n"]
                assert model_count >= 2

                dynamic_multiplier, dynamic_meta = tm.dynamic_ensemble_multiplier(
                    connection, approved_def, "trend", 1
                )
                assert settings.DYNAMIC_ENSEMBLE_MIN_MULTIPLIER <= dynamic_multiplier <= settings.DYNAMIC_ENSEMBLE_MAX_MULTIPLIER
                assert "collective_win_probability" in dynamic_meta

                # V6.5 GPT state-cache + veto-shadow learning. The second identical
                # review must not call the reviewer again; a material flow change must.
                gpt_snapshot = {
                    "symbol": "XAUUSDm",
                    "candidate": {"action": "buy", "confluence_score": 7, "confidence": 0.82},
                    "market": {"bid": 2400.00, "ask": 2400.10, "spread_atr": 0.10},
                    "technical": {"close": 2400.05, "atr14": 1.0, "rsi7": 61.0, "adx14": 31.0},
                    "derived_context": {"price_vs_ema200_pct": 0.25, "order_flow_bias": "bid_heavy"},
                    "order_flow": {"bid_share": 0.65, "imbalance_pct": 30.0, "persistence": 0.2},
                    "smc": {"bos": "bullish", "choch": None, "fvg": "bullish", "order_block": "bullish", "liquidity_sweep": "sell_side"},
                    "higher_timeframes": {"H1": {"trend": "bullish"}},
                    "news": {"available": True, "locked": False},
                    "session": {"allowed": True},
                    "ml": {"probability": 0.74, "probability_active": True},
                    "agents_vote": {"technical": "buy", "ml": "buy"},
                }
                calls = {"n": 0}
                def fake_veto(_snapshot):
                    calls["n"] += 1
                    return {
                        "enabled": True, "approved": False, "action": "hold",
                        "decision": "hold", "confidence": 0.66,
                        "context_summary": "test veto", "reasoning": "test veto",
                        "contradictions": ["test"], "risk_flags": [],
                        "agents_vote": {}, "latency_ms": 1.0,
                        "decision_source": "openai", "model": "offline-test",
                        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                    }
                review1, meta1 = gpt_memory.review_with_cache(
                    connection, gpt_snapshot, strategy_id=int(approved_id), family="trend",
                    regime="trend", side=1, bar_time="2026-01-01T00:01:00Z", reviewer=fake_veto,
                )
                review2, meta2 = gpt_memory.review_with_cache(
                    connection, gpt_snapshot, strategy_id=int(approved_id), family="trend",
                    regime="trend", side=1, bar_time="2026-01-01T00:01:00Z", reviewer=fake_veto,
                )
                assert calls["n"] == 1
                assert not meta1["cache_hit"] and meta2["cache_hit"]
                assert review2["decision_source"] == "decision_fingerprint_cache"

                changed_snapshot = dict(gpt_snapshot)
                changed_snapshot["order_flow"] = dict(gpt_snapshot["order_flow"])
                changed_snapshot["order_flow"]["bid_share"] = 0.76
                changed_snapshot["order_flow"]["imbalance_pct"] = 52.0
                _review3, meta3 = gpt_memory.review_with_cache(
                    connection, changed_snapshot, strategy_id=int(approved_id), family="trend",
                    regime="trend", side=1, bar_time="2026-01-01T00:01:00Z", reviewer=fake_veto,
                )
                assert calls["n"] == 2 and not meta3["cache_hit"]

                shadow_id = gpt_memory.open_veto_shadow(
                    connection, review_id=int(meta1["review_id"]), fingerprint=str(meta1["fingerprint"]),
                    symbol="XAUUSDm", strategy_id=int(approved_id), family="trend", regime="trend",
                    side=1, entry=2400.10, stop=2399.10, take=2401.60,
                    opened_bar_time="2026-01-01T00:01:00Z", max_hold_bars=15,
                    superlearner_features=super_features, context={"market_session": "new_york"},
                )
                assert shadow_id
                # Idempotent: cache reuse cannot open another shadow for same fingerprint.
                assert gpt_memory.open_veto_shadow(
                    connection, review_id=int(meta1["review_id"]), fingerprint=str(meta1["fingerprint"]),
                    symbol="XAUUSDm", strategy_id=int(approved_id), family="trend", regime="trend",
                    side=1, entry=2400.10, stop=2399.10, take=2401.60,
                    opened_bar_time="2026-01-01T00:01:00Z", max_hold_bars=15,
                    superlearner_features=super_features, context={"market_session": "new_york"},
                ) == shadow_id
                closed_veto = gpt_memory.reconcile_veto_shadows(
                    connection, symbol="XAUUSDm", bid=2399.00, ask=2399.10
                )
                assert len(closed_veto) == 1
                assert closed_veto[0]["outcome_label"] == "veto_saved_loss"
                calibration = gpt_memory.decision_memory_snapshot(connection, "XAUUSDm", "trend", 1)
                assert calibration["veto"]["observations"] == 1
                assert calibration["veto"]["quality_ewma"] > 0
                gpt_memory.record_confirm_outcome(
                    connection, fingerprint=None, symbol="XAUUSDm", regime="trend", side=1,
                    reward_r=1.25, closed_at=tm.utc_now(),
                )
                calibration = gpt_memory.decision_memory_snapshot(connection, "XAUUSDm", "trend", 1)
                assert calibration["confirm"]["observations"] == 1
                assert calibration["confirm"]["quality_ewma"] > 0

                live = {
                    "observations": settings.SHADOW_APPROVAL_TRADES,
                    "reward_mean": settings.SHADOW_APPROVAL_MEAN_R + 0.01,
                    "profit_factor": settings.SHADOW_APPROVAL_PROFIT_FACTOR + 0.1,
                    "max_drawdown_r": settings.SHADOW_APPROVAL_MAX_DRAWDOWN_R - 1.0,
                }
                assert tm.maybe_promote_shadow_candidate(
                    connection, int(historical_id), live
                )
                status = connection.execute(
                    "SELECT status FROM strategies WHERE id=?", (historical_id,)
                ).fetchone()["status"]
                assert status == "shadow_approved"
                connection.commit()

            assert test_gpt_connection(run_network=False)
            spartan_result = spartan.offline_self_test()
            assert all(bool(value) for value in spartan_result.values())

            # Windows refuses to rename/delete an SQLite file while any
            # process still owns an open handle.  This is a direct regression
            # test for the installer failure reported on Python 3.13.
            released_path = root / "test.released.db"
            settings.DATABASE_PATH.replace(released_path)
            released_path.replace(settings.DATABASE_PATH)

            parser = tm.build_parser()
            subparser_action = next(
                action for action in parser._actions
                if hasattr(action, "choices") and action.choices
            )
            required_commands = {
                "generator-loop", "backtest-loop", "shadow", "library", "demo"
            }
            assert required_commands.issubset(set(subparser_action.choices))

            root_path = Path(__file__).resolve().parent
            supervisor_source = (root_path / "machine_supervisor.py").read_text(
                encoding="utf-8"
            )
            bootstrap_source = (root_path / "start_supervisor.py").read_text(
                encoding="utf-8"
            )
            state_source = (root_path / "runtime_state.py").read_text(
                encoding="utf-8"
            )
            assert "CREATE_NEW_CONSOLE" in supervisor_source
            assert '["cmd.exe", "/d", "/s", "/c"' not in supervisor_source
            assert "TM_START_TOKEN" in supervisor_source
            assert "pid == process.pid" not in bootstrap_source
            assert "taskkill" not in bootstrap_source
            assert "atomic_write_text" in state_source
            machine_source = (root_path / "trading_machine.py").read_text(
                encoding="utf-8"
            )
            assert "MAX_PENDING_STRATEGIES_TOTAL" in machine_source
            assert "demo_trial_candidate_strategies" in machine_source
            assert "adaptive_learning_gate" in machine_source
            assert "adaptive_trade_memory" in machine_source
            assert "class SuperLearner" in machine_source
            assert "superlearner_model_state" in machine_source
            assert "collective_market_memory" in machine_source
            assert "session_family_memory" in machine_source
            assert "SCALP_FAMILIES" in machine_source
            assert "micro_momentum" in machine_source
            assert "pullback_scalp" in machine_source
            assert "breakout_scalp" in machine_source
            assert "mean_revert_scalp" in machine_source
            assert "SCALP_FAST_PRESCREEN_BARS" in machine_source
            assert "market_session_tag" in machine_source
            assert "market_sentiment_proxy" in machine_source
            assert "order_book_microstructure" in machine_source
            assert "hurst_exponent" in machine_source
            assert "spartan.build_gate_snapshot" in machine_source
            assert "spartan_manage_demo_positions" in machine_source
            assert "micro_hunter.observe_closed_bar" in machine_source
            assert "micro_hunter.intrabar_trigger" in machine_source
            assert "micro_hunter.record_demo_outcome" in machine_source
            assert (root_path / "micro_scalp_hunter.py").exists()
            assert (root_path / "spartan_pro.py").exists()
            assert (root_path / "spartan_llm.py").exists()
            assert (root_path / "spartan_posttrade.py").exists()
            post_source = (root_path / "spartan_posttrade.py").read_text(encoding="utf-8")
            assert "enqueue_closed_trade_review" in post_source
            assert "automatic_live_parameter_changes" not in post_source or "live_parameter_mutation" in post_source
            assert "spartan_posttrade_enqueue" in machine_source
            assert machine_source.index("SUPER_LEARNER.predict") < machine_source.index("gpt_memory.review_with_cache")
            assert machine_source.index("gpt_memory.review_with_cache") < machine_source.index("portfolio_risk_mult, portfolio_risk_state = portfolio_soft_risk_multiplier(guard)")
            assert "gpt_memory.open_veto_shadow" in machine_source
            assert "gpt_memory.reconcile_veto_shadows" in machine_source
            assert (root_path / "spartan_decision_memory.py").exists()

            # V6.7 SCALP LAB + LUNA budget-brain regressions.
            soft = SimpleNamespace(
                approved=False, probability_active=True, probability=0.52, threshold=0.58,
                reason="calibrated scalp probability below threshold",
                market={"expected_r": 0.20, "break_even_probability": 0.40},
            )
            hard = SimpleNamespace(
                approved=False, probability_active=True, probability=0.57, threshold=0.58,
                reason="calibrated scalp probability below threshold; Bayesian setup loss probability too high",
                market={"expected_r": 0.12},
            )
            soft_ok, soft_info = scalp_ai.superlearner_borderline_eligible(soft)
            hard_ok, hard_info = scalp_ai.superlearner_borderline_eligible(hard)
            assert soft_ok and soft_info["expected_r"] > 0.0
            assert not hard_ok and hard_info["hard_reject"]

            # V6.9.4 regression: high-R:R positive-expectancy scalp just below a
            # low calibrated threshold must be allowed to ask Luna, not die in
            # an impossible fixed-minimum dead-zone.
            expectancy_soft = SimpleNamespace(
                approved=False, probability_active=True, probability=0.367, threshold=0.400,
                reason="calibrated scalp probability 0.37 below 0.40",
                market={"expected_r": 0.578, "reward_to_risk": 3.30,
                        "break_even_probability": 1.0 / 4.30},
            )
            expectancy_ok, expectancy_info = scalp_ai.superlearner_borderline_eligible(expectancy_soft)
            assert expectancy_ok
            assert expectancy_info["min_probability"] < expectancy_soft.threshold
            assert expectancy_soft.probability >= expectancy_info["min_probability"]

            mc = scalp_lab.monte_carlo_rewards([0.9, -0.35, 0.45, -0.20, 0.7, -0.25], samples=250)
            assert mc["samples"] == 250
            assert "p95_max_drawdown_r" in mc
            assert 0.0 <= mc["probability_negative_final_r"] <= 1.0

            compact = spartan_llm.compact_snapshot({
                "review_mode": "borderline_review",
                "symbol": "XAUUSDm",
                "opportunity_score": 83.4,
                "candidate": {"action": "sell", "confluence_score": 6, "confidence": 0.81},
                "strategy_context": {"strategy_id": 77, "family": "micro_momentum", "regime": "trend", "execution_tier": "demo_trial"},
                "condition_scores": {"available_passed": 6, "available_total": 7, "core_passed": 4, "core_total": 4},
                "ml": {"probability": 0.52, "threshold": 0.58, "expected_r": 0.08, "probability_active": True},
            })
            assert compact["v"] == "8.0"
            assert compact["mode"] == "borderline_review"
            assert compact["opportunity"] == 83.4
            assert compact["side"] == "sell"

            # V7 micro-scalp hunter: a deliberately strong 2-3 candle impulse
            # must produce a specialist setup, while the hunter can only select
            # a carrier from the already-qualified candidate list.
            hunter_bars = synthetic_bars(420)
            # Create a clean upward trend and a strong final closed-bar burst.
            hunter_bars["close"] = 2000.0 + np.arange(len(hunter_bars)) * 0.03
            hunter_bars["open"] = hunter_bars["close"].shift(1).fillna(hunter_bars["close"].iloc[0] - 0.02)
            hunter_bars["high"] = np.maximum(hunter_bars["open"], hunter_bars["close"]) + 0.05
            hunter_bars["low"] = np.minimum(hunter_bars["open"], hunter_bars["close"]) - 0.05
            hunter_bars.loc[len(hunter_bars)-2, "open"] = hunter_bars.loc[len(hunter_bars)-3, "close"] - 0.05
            hunter_bars.loc[len(hunter_bars)-2, "close"] = hunter_bars.loc[len(hunter_bars)-3, "close"] + 1.25
            hunter_bars.loc[len(hunter_bars)-2, "high"] = hunter_bars.loc[len(hunter_bars)-2, "close"] + 0.08
            hunter_bars.loc[len(hunter_bars)-2, "low"] = hunter_bars.loc[len(hunter_bars)-2, "open"] - 0.04
            hunter_bars.loc[len(hunter_bars)-2, "tick_volume"] = 2500
            hunter_frame = tm.add_features(hunter_bars, {5,13,20,50,200})
            original_hunter_thresholds = (
                settings.V7_MICRO_HUNTER_XAU_WATCH_SCORE,
                settings.V7_MICRO_HUNTER_XAU_ARM_SCORE,
                settings.V7_MICRO_HUNTER_XAU_TRIGGER_SCORE,
            )
            settings.V7_MICRO_HUNTER_XAU_WATCH_SCORE = 70.0
            settings.V7_MICRO_HUNTER_XAU_ARM_SCORE = 75.0
            settings.V7_MICRO_HUNTER_XAU_TRIGGER_SCORE = 80.0
            hunter_setups = micro_hunter.detect_playbooks(hunter_frame, "XAUUSDm", None)
            settings.V7_MICRO_HUNTER_XAU_WATCH_SCORE, settings.V7_MICRO_HUNTER_XAU_ARM_SCORE, settings.V7_MICRO_HUNTER_XAU_TRIGGER_SCORE = original_hunter_thresholds
            assert hunter_setups and hunter_setups[0].side in (-1,1)
            assert hunter_setups[0].playbook in micro_hunter.PLAYBOOK_FAMILIES
            non_scalp_carrier, _ = micro_hunter.select_carrier([(definition, 10.0)], hunter_setups[0])
            assert non_scalp_carrier is None
            scalp_carrier = tm.StrategyDefinition(
                strategy_id=99, symbol="XAUUSDm", family="micro_momentum",
                params=tm.normalize_params("micro_momentum", {
                    "fast": 4, "slow": 12, "rsi_low": 42, "rsi_high": 56,
                    "min_body_atr": 0.20, "min_volume_ratio": 0.90,
                    "stop_atr": 0.60, "take_atr": 1.10, "max_hold": 5, "dom_threshold": 0.0,
                }),
            )
            carrier, _ = micro_hunter.select_carrier([(scalp_carrier, 10.0)], hunter_setups[0])
            assert carrier is scalp_carrier
            with tm.db_connect() as hunter_con:
                micro_hunter.ensure_tables(hunter_con)
                risk_mult, _ = micro_hunter.execution_risk_multiplier(hunter_con, hunter_setups[0])
                assert 0.0 < risk_mult <= 1.0

            # V8 institutional alpha: adaptive score memory, DEMO-only native
            # carrier and cheap pre-Luna shortlist are independently testable.
            with tm.db_connect() as v8_con:
                institutional_alpha.ensure_tables(v8_con)
                institutional_alpha.record_score_samples(
                    v8_con, "XAUUSDm", hunter_setups[0].bar_time,
                    [(hunter_setups[0].playbook, hunter_setups[0].side, hunter_setups[0].score)],
                )
                th = institutional_alpha.dynamic_thresholds(v8_con, "XAUUSDm")
                assert th["watch"] < th["trigger"]
                native = tm.v8_native_alpha_strategy(v8_con, hunter_setups[0])
                assert native.strategy_id > 0
                native_status = v8_con.execute("SELECT status FROM strategies WHERE id=?", (native.strategy_id,)).fetchone()[0]
                assert native_status == "v8_native_demo"
                fake_super = SimpleNamespace(
                    approved=True, probability=.58, threshold=.42,
                    market={"expected_r": .55, "reward_to_risk": 2.4},
                )
                ok_local, local_score, _meta = institutional_alpha.local_pre_luna_gate(
                    v8_con, symbol="XAUUSDm", strategy_id=native.strategy_id,
                    side=hunter_setups[0].side,
                    details={"micro_hunter": hunter_setups[0].as_dict()},
                    super_decision=fake_super,
                    spartan_snapshot={
                        "candidate": {"confidence": .85},
                        "condition_scores": {"available_ratio": .86},
                    },
                )
                assert ok_local and local_score >= float(getattr(settings, "V8_PRE_LUNA_LOCAL_SCORE", 56.0))

                # V8.1: continuation contradictions must be stopped locally (zero API),
                # while a genuinely strong reversal playbook may still reach Luna.
                direction_snap = {
                    "candidate": {"confidence": .84},
                    "condition_scores": {"available_ratio": .82},
                    "derived_context": {"regime": "trending_down"},
                    "smc": {"structure": "bearish"},
                    "higher_timeframes": {
                        "M5": {"available": True, "trend": "down"},
                        "M15": {"available": True, "trend": "down"},
                        "H1": {"available": True, "trend": "down"},
                    },
                    "agents_vote": {
                        "technical": "sell", "smc": "sell", "flow": "sell",
                        "htf": "sell", "volume_profile": "sell", "regime": "sell",
                    },
                }
                bad_super = SimpleNamespace(market={"expected_r": .50}, probability=.56, threshold=.44, approved=True)
                bad_ok, _, bad_meta = institutional_alpha.local_pre_luna_gate(
                    v8_con, symbol="XAUUSDm", strategy_id=native.strategy_id, side=1,
                    details={"micro_hunter": {"playbook": "momentum_burst", "score": 92.0, "context": {"evidence": {"trend": 0.0}}}},
                    super_decision=bad_super, spartan_snapshot=direction_snap,
                )
                assert not bad_ok and "direction/structure contradiction" in bad_meta["reason"]
                rev_super = SimpleNamespace(market={"expected_r": .45}, probability=.53, threshold=.44, approved=True)
                rev_ok, _, rev_meta = institutional_alpha.local_pre_luna_gate(
                    v8_con, symbol="XAUUSDm", strategy_id=native.strategy_id, side=1,
                    details={"micro_hunter": {"playbook": "wick_rejection", "score": 94.0, "context": {"evidence": {"wick": .92, "rsi": .84}}}},
                    super_decision=rev_super, spartan_snapshot=direction_snap,
                )
                assert rev_ok and rev_meta["direction"]["strong_reversal"]

            gpt_test_source = (root_path / "gpt_connection_test.py").read_text(encoding="utf-8")
            start_bat_source = (root_path / "00_START_ALL_24_7_DEMO.bat").read_text(encoding="utf-8")
            assert "local_gpt_readiness" in gpt_test_source
            assert "v11_startup_check.py" in start_bat_source
            startup_source = Path(tm.__file__).with_name("v11_startup_check.py").read_text(encoding="utf-8")
            assert "--startup-local" in startup_source  # legacy-mode local fallback
            assert "DEMO_ONLY_HARD_LOCK" in startup_source
            assert int(getattr(settings, "SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY", 99)) <= int(
                getattr(settings, "V11_PROBES_TOTAL_DAY", 24)
            )

            v7_compact = spartan_llm.compact_snapshot({
                "review_mode": "final_review", "symbol": "XAUUSDm",
                "candidate": {"action": "buy", "confluence_score": 7, "confidence": .84},
                "strategy_context": {"strategy_id": 1, "family": "micro_momentum", "regime": "trend", "execution_tier": "demo_trial"},
                "micro_hunter": hunter_setups[0].as_dict(),
            })
            assert v7_compact["v"] == "8.0"
            assert v7_compact["micro"]["playbook"] == hunter_setups[0].playbook
            assert v7_compact["micro"]["score"] is not None

            memory_source = (root_path / "spartan_decision_memory.py").read_text(encoding="utf-8")
            assert "api_budget_snapshot" in memory_source
            assert "estimated_usage_cost_usd" in memory_source
            assert "SPARTAN_GPT_BOT_BUDGET_USD" in memory_source
            assert str(getattr(settings, "SPARTAN_LLM_MODEL", "")).strip().lower() == "gpt-5.6-luna"
            assert float(getattr(settings, "SPARTAN_GPT_BOT_BUDGET_USD", 0.0)) <= 4.0
            assert int(getattr(settings, "SPARTAN_LLM_MAX_OUTPUT_TOKENS", 999)) <= 80

            with tm.db_connect() as growth_con:
                growth_con.execute("DELETE FROM capital_growth_days")
                g0 = capital_growth.update_state(growth_con, 100.0)
                g1 = capital_growth.update_state(growth_con, 115.1)
                g2 = capital_growth.update_state(growth_con, 120.1)
                assert g0["phase"] == "build" and g0["allow_new_entries"]
                assert g1["phase"] == "target_protect" and g1["risk_multiplier"] <= 0.50
                assert g2["phase"] == "stretch_lock" and not g2["allow_new_entries"]
                assert bool(g2["never_chase_target"])

            # V6.9 recent-edge self-correction regressions. Filtering happens
            # before Luna so poor historical/live evidence does not spend API.
            assert bool(getattr(settings, "EDGE_RECOVERY_ENABLED", False))
            assert machine_source.index("scalp_lab.edge_recovery_guard") < machine_source.index("gpt_memory.review_with_cache")
            assert "SCALP_SHADOW_SCORE_WEIGHT" in machine_source
            assert "SCALP_FAST_TRACK_SHADOW_ENABLED" in machine_source
            assert hasattr(scalp_lab, "demo_monte_carlo_by_symbol")
            assert hasattr(scalp_lab, "edge_recovery_guard")
            summary = scalp_lab._reward_summary([0.7, -0.2, 0.4, -0.1])
            assert summary["n"] == 4 and summary["mean_r"] > 0 and summary["profit_factor"] > 1.0

            print("PASS: database schema and migration")
            print("PASS: regime-guided strategy normalization")
            print("PASS: anchored walk-forward evaluator")
            print("PASS: strict execution selects shadow-approved strategies first")
            print("PASS: merged legacy passers are eligible only for tiny-risk DEMO trial fallback")
            print("PASS: historical -> live-shadow approval gate")
            print("PASS: SQLite file handle released for Windows cleanup")
            print("PASS: five split engine CLI commands are registered")
            print("PASS: Windows consoles launch without CMD START quoting")
            print("PASS: token-based READY handshake does not compare launcher/actual PID")
            print("PASS: bootstrap cannot kill a healthy supervisor on marker timeout")
            print("PASS: atomic runtime markers are outside the project/OneDrive path")
            print("PASS: generator queue throttle and DEMO trial bridge are present")
            print("PASS: persistent adaptive loss memory, cooldown and confidence recovery")
            print("PASS: confidence-based risk multiplier only upscales after positive evidence")
            print("PASS: adaptive risk can produce broker-valid volume above 0.01 when supported")
            print("PASS: V6 collective + session family/regime memory and Bayesian posterior")
            print("PASS: Hurst/entropy/concept-drift market quality metrics")
            print("PASS: persistent online model updates and becomes active after warm-up")
            print("PASS: dynamic ensemble multiplier consumes adaptive + collective/session evidence")
            print("PASS: V6 scalp families, fast prescreen, session/sentiment and SuperLearner code paths are present")
            print("PASS: GPT/OpenAI structured Responses wiring is enabled and confidence-gated")
            print("PASS: GPT final reviewer runs after SuperLearner and before deterministic risk sizing")
            print("PASS: GPT post-trade analyst is advisory-only and asynchronously journaled")
            print("PASS: GPT material-state fingerprint cache prevents duplicate API calls")
            print("PASS: GPT veto shadow outcomes calibrate reviewer and reduced-weight ML memory")
            print("PASS: Spartan-Pro EMA/ADX, SMC and Volume Profile deterministic feature engine")
            print("PASS: Spartan-Pro live hard-gate and DEMO trailing manager are wired")
            print("PASS: V6.8 soft-borderline Luna lane cannot override hard rejects")
            print("PASS: V6.8 Monte Carlo scalp-lab risk simulation")
            print("PASS: V6.8 compact Luna packet includes mode + opportunity + capital context")
            print("PASS: V6.8 Luna model + local bot-budget/output caps")
            print("PASS: V6.8 15/20 daily capital-growth objective protects gains without target-chasing risk")
            print("PASS: V6.9 edge recovery runs before Luna/API and never martingales")
            print("PASS: V6.9 symbol-level Monte Carlo and shadow-evidence re-ranking are wired")
            print("PASS: V6.9 strict scalp fast-track + early weak-strategy quarantine are wired")
            print("PASS: V7 micro-scalp hunter detects specialist 2-5 candle opportunities")
            print("PASS: V7 WATCH/ARM/TRIGGER lane remains available; V8 adds a DEMO-only native alpha carrier without bypassing hard gates")
            print("PASS: V7 playbook MFE/MAE learning uses broker-DEMO outcomes only and never upscales risk")
            print("PASS: V7 compact Luna packet carries micro playbook context while token caps remain enforced")
            print("PASS: V8 adaptive alpha thresholds learn symbol score distributions with hard floors")
            print("PASS: V8 DEMO-only native alpha carrier removes approved-scalp starvation without bypassing hard gates")
            print("PASS: V8 local pre-Luna shortlist and zero-token startup readiness reduce API waste")
            assert hasattr(institutional_alpha, "luna_disagreement_probe_policy")
            assert int(getattr(settings, "SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY", 99)) <= int(
                getattr(settings, "V11_PROBES_TOTAL_DAY", 24)
            )
            assert 0.05 <= float(getattr(settings, "V8_LUNA_DISAGREEMENT_RISK_MULTIPLIER", 0.10)) <= 0.20
            assert "luna_disagreement_probe_policy" in machine_source
            with sqlite3.connect(":memory:") as probe_con:
                probe_con.row_factory = sqlite3.Row
                fake_super = SimpleNamespace(approved=True, probability=0.60, market={"expected_r": 0.35})
                ok_probe, probe_mult, probe_meta = institutional_alpha.luna_disagreement_probe_policy(
                    probe_con, symbol="XAUUSDm", strategy_id=999001, side=1,
                    details={"micro_hunter": {"playbook": "momentum_burst", "score": 90.0}},
                    super_decision=fake_super,
                    local_alpha_meta={"score": 92.0, "direction_ok": True, "direction": {"aligned_votes": 4, "opposed_votes": 0}},
                    llm_review={"decision": "hold", "confidence": 0.92, "decision_source": "openai", "reasoning": "context cautious"},
                    spartan_snapshot={},
                )
                assert ok_probe and 0.05 <= probe_mult <= 0.20
                second_probe, _, _ = institutional_alpha.luna_disagreement_probe_policy(
                    probe_con, symbol="XAUUSDm", strategy_id=999002, side=1,
                    details={"micro_hunter": {"playbook": "momentum_burst", "score": 90.0}},
                    super_decision=fake_super,
                    local_alpha_meta={"score": 92.0, "direction_ok": True, "direction": {"aligned_votes": 4, "opposed_votes": 0}},
                    llm_review={"decision": "hold", "confidence": 0.92, "decision_source": "openai", "reasoning": "context cautious"},
                    spartan_snapshot={},
                )
                assert not second_probe

            print("PASS: V8.1 continuation contradictions are held locally before Luna/API")
            print("PASS: V8.1 strong reversal playbooks remain eligible despite countertrend context")
            print("PASS: V8.2 Luna is an ensemble member; high-quality disagreements can collect capped tiny-risk DEMO evidence")
            print("PASS: V8.2 disagreement probes are symbol/day capped and cannot bypass deterministic contradiction gates")
            assert hasattr(micro_hunter, "mark_wait_spread") and hasattr(micro_hunter, "spread_wait_resume")
            assert bool(getattr(settings, "V8_SPREAD_WAIT_ENABLED", False))
            assert 5 <= int(getattr(settings, "V8_SPREAD_WAIT_TTL_SECONDS", 0)) <= 60
            assert 0.05 <= float(getattr(settings, "V8_SPREAD_WAIT_MAX_DRIFT_ATR", 0.0)) <= 0.30
            with sqlite3.connect(":memory:") as spread_con:
                spread_con.row_factory = sqlite3.Row
                micro_hunter.ensure_tables(spread_con)
                setup = micro_hunter.MicroSetup(
                    symbol="USOILm", playbook="wick_rejection", side=1, phase="TRIGGER",
                    score=88.0, bar_time="2026-09-01T15:00:00+00:00", trigger_price=70.00,
                    invalidation_price=69.90, stop_atr=0.8, take_atr=1.2, max_hold_bars=3,
                    preferred_families=("mean_revert_scalp",), risk_multiplier=0.5, context={},
                )
                micro_hunter._persist_setup(spread_con, setup, phase="TRIGGERED")
                micro_hunter.mark_wait_spread(spread_con, setup, 0.02, 0.09)
                phase = spread_con.execute("SELECT phase FROM micro_hunter_setups LIMIT 1").fetchone()[0]
                assert phase == "WAIT_SPREAD"
            assert "V8 spread wait armed" in machine_source
            assert "spread_wait_resume" in machine_source
            print("PASS: V8.3 wide-spread micro triggers park briefly instead of being discarded")
            print("PASS: V8.3 spread wait never widens the hard spread limit and has TTL/no-chase guards")

            # V8.4: live micro triggers are execution-first and collect tiny-risk
            # broker evidence without inheriting stale trial-strategy memory.
            assert bool(getattr(settings, "V8_NATIVE_ALPHA_PRIMARY_FOR_HUNTER", False))
            assert "V8_NATIVE_ALPHA_PRIMARY_FOR_HUNTER" in machine_source
            assert "V8_MICRO_RECOVERY_SCORE_BONUS" in machine_source
            with sqlite3.connect(":memory:") as v84_con:
                v84_con.row_factory = sqlite3.Row
                borderline_super = SimpleNamespace(
                    approved=False, probability=0.43,
                    market={"expected_r": 0.35, "break_even_probability": 0.34},
                )
                v84_ok, v84_mult, v84_meta = institutional_alpha.luna_disagreement_probe_policy(
                    v84_con, symbol="BTCUSDm", strategy_id=999101, side=1,
                    details={"micro_hunter": {"playbook": "momentum_burst", "score": 91.0}},
                    super_decision=borderline_super,
                    local_alpha_meta={"score": 90.0, "direction_ok": True, "direction": {"aligned_votes": 4, "opposed_votes": 0}},
                    llm_review={"decision": "hold", "confidence": 0.91, "decision_source": "openai", "reasoning": "cautious"},
                    spartan_snapshot={},
                )
                assert v84_ok and 0.05 <= v84_mult <= 0.20
                assert bool(v84_meta.get("quant_positive"))
            print("PASS: V8.4 Micro Hunter uses native DEMO alpha as primary execution carrier")
            print("PASS: V8.4 positive-expectancy borderline Quant/Luna disagreement can collect a capped tiny-risk DEMO probe")

            legacy_profile = spartan.micro_gate_profile("momentum_burst", 95.0, "demo_trial")
            cont_profile = spartan.micro_gate_profile("momentum_burst", 75.0, "v8_native_alpha")
            rev_profile = spartan.micro_gate_profile("wick_rejection", 82.0, "v8_native_alpha")
            sq_profile = spartan.micro_gate_profile("squeeze_breakout", 82.0, "v8_native_alpha")
            assert not legacy_profile["active"] and legacy_profile["adx_min"] >= 20.0
            assert cont_profile["active"] and cont_profile["category"] == "continuation"
            assert 14.0 <= cont_profile["adx_min"] < legacy_profile["adx_min"]
            assert cont_profile["min_ratio"] < legacy_profile["min_ratio"]
            assert rev_profile["active"] and not rev_profile["require_adx"]
            assert sq_profile["active"] and not sq_profile["require_adx"]

            with sqlite3.connect(":memory:") as v85_con:
                v85_con.row_factory = sqlite3.Row
                v85_super = SimpleNamespace(
                    approved=True, probability=0.49, threshold=0.41,
                    market={"expected_r": 1.31, "reward_to_risk": 2.2,
                            "break_even_probability": 1.0/3.2},
                )
                v85_ok, v85_mult, v85_meta = institutional_alpha.budget_exhausted_quant_probe_policy(
                    v85_con, symbol="BTCUSDm", strategy_id=999201, side=-1,
                    details={"micro_hunter": {"playbook": "momentum_burst", "score": 96.0}},
                    super_decision=v85_super,
                    local_alpha_meta={"score": 88.0, "direction_ok": True,
                                      "direction": {"aligned_votes": 4, "opposed_votes": 0}},
                    llm_review={"decision": "hold", "decision_source": "daily_call_cap",
                                "reasoning": "Pre-trade GPT call cap reached"},
                    spartan_snapshot={},
                )
                assert v85_ok and 0.03 <= v85_mult <= 0.10
                assert v85_meta["probability_edge"] >= float(getattr(settings, "V8_BUDGET_PROBE_MIN_PROB_EDGE", 0.04))
                v85_second, _, _ = institutional_alpha.budget_exhausted_quant_probe_policy(
                    v85_con, symbol="BTCUSDm", strategy_id=999202, side=-1,
                    details={"micro_hunter": {"playbook": "momentum_burst", "score": 97.0}},
                    super_decision=v85_super,
                    local_alpha_meta={"score": 90.0, "direction_ok": True,
                                      "direction": {"aligned_votes": 4, "opposed_votes": 0}},
                    llm_review={"decision": "hold", "decision_source": "daily_call_cap"},
                    spartan_snapshot={},
                )
                assert not v85_second

            assert int(getattr(settings, "SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY", 99)) <= int(
                getattr(settings, "V11_PROBES_TOTAL_DAY", 24)
            )
            assert "budget_exhausted_quant_probe_policy" in machine_source
            print("PASS: V8.5 playbook-aware Spartan profiles stop using one generic ADX/confluence rule for every micro scalp")
            print("PASS: V8.5 Luna call-cap/budget exhaustion can collect capped zero-token Quant DEMO evidence without bypassing hard gates")

            # V9 execution-first: model outputs become advisory risk evidence for
            # live Micro Hunter entries. Hard cost/account/broker locks remain.
            assert bool(getattr(settings, "V9_EXECUTION_FIRST_ENABLED", False))
            assert "v9_execute_hunter_candidate" in machine_source
            assert "v9_exec.spread_limit_atr" in machine_source
            assert float(getattr(settings, "V9_BASE_RISK_PER_TRADE", 1.0)) <= 0.0015
            assert int(getattr(settings, "V9_MAX_TRADES_PER_DAY", 0)) == 300
            assert int(getattr(settings, "V9_MAX_TRADES_PER_SYMBOL_PER_DAY", 0)) == 100
            assert int(getattr(settings, "PORTFOLIO_FREEZE_AFTER_LOSSES", 0)) == 5
            assert not bool(getattr(settings, "V9_MARTINGALE_ENABLED", True))
            assert int(getattr(settings, "SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY", 99)) <= int(
                getattr(settings, "V11_PROBES_TOTAL_DAY", 24)
            )
            assert 0.18 <= v9_exec.spread_limit_atr(1.5) <= float(getattr(settings, "V9_ABSOLUTE_MAX_SPREAD_ATR", 0.28))
            v9_super = SimpleNamespace(
                approved=True, probability=0.49, threshold=0.41, probability_active=True,
                market={"expected_r": 0.65, "reward_to_risk": 2.2, "break_even_probability": 1.0/3.2},
            )
            v9_pre = v9_exec.pre_luna_score(
                micro_score=89.0, super_decision=v9_super,
                spartan_snapshot={
                    "approved": False,
                    "candidate": {"confidence": 0.52},
                    "condition_scores": {"available_ratio": 0.50},
                },
                adaptive_allowed=False, adaptive_gate={"snapshot": {"confidence": 0.45, "consecutive_losses": 0}},
                edge_allowed=False,
                playbook_memory={"observations": 8, "win_rate": 0.62, "reward_ewma": 0.18, "reward_mean": 0.12, "mfe_mean": 1.0, "mae_mean": 0.45},
            )
            v9_go = v9_exec.final_decision(
                pre=v9_pre, super_decision=v9_super,
                spartan_snapshot={"approved": False}, adaptive_allowed=False,
                edge_allowed=False,
                luna_review={"decision": "hold", "confidence": 0.90, "decision_source": "openai"},
            )
            assert v9_go["execute"]
            assert 0.20 <= v9_go["risk_multiplier"] < 1.0
            v9_bad_super = SimpleNamespace(
                approved=False, probability=0.18, threshold=0.42, probability_active=True,
                market={"expected_r": -0.45, "reward_to_risk": 1.5, "break_even_probability": 0.40},
            )
            v9_bad = v9_exec.pre_luna_score(
                micro_score=75.0, super_decision=v9_bad_super,
                spartan_snapshot={"approved": True, "candidate": {"confidence": 0.8}, "condition_scores": {"available_ratio": 0.8}},
                adaptive_allowed=True, adaptive_gate={"snapshot": {"confidence": 0.7, "consecutive_losses": 0}}, edge_allowed=True,
                playbook_memory={"observations": 8, "win_rate": 0.30, "reward_ewma": -0.18, "reward_mean": -0.10, "mfe_mean": 0.30, "mae_mean": 0.85},
            )
            assert v9_bad["hard_hold"]
            # V9.1 precision memory: weak evidence loses score while a strong
            # independent post-loss setup may recover only with capped risk.
            v91_mem = v9_exec.precision_memory_adjustment(
                micro_score=92.0, expected_r=0.70, probability_edge=0.12,
                playbook_memory={"observations": 8, "win_rate": 0.30, "reward_ewma": -0.12, "reward_mean": -0.08, "mfe_mean": 0.55, "mae_mean": 0.80},
                adaptive_gate={"snapshot": {"consecutive_losses": 2}},
            )
            assert v91_mem["score_delta"] < 0
            assert v91_mem["recovery_mode"]
            assert v91_mem["recovery_risk_cap"] <= 0.55
            print("PASS: V9.2 100/symbol capacity and five-loss freeze are configured without forcing quota trades")
            # V9.2 adaptive scalp overlay: microstructure/MTF/context memory are
            # bounded score/risk evidence, dynamic exits expose partial stages,
            # and first-five context trades are trial-risk capped.
            assert bool(getattr(settings, "V92_ADAPTIVE_SCALPING_ENABLED", False))
            assert "early_hunter_memory" in machine_source
            assert bool(getattr(settings, "V92_REJECTED_TRADE_LEARNING_ENABLED", False))
            assert not bool(getattr(settings, "V9_MARTINGALE_ENABLED", True))
            with sqlite3.connect(":memory:") as v92_con:
                v92_con.row_factory = sqlite3.Row
                v92.ensure_tables(v92_con)
                v92_snap = {
                    "session": {"session": "new_york"},
                    "order_flow": {"available": True, "score": 0.60},
                    "tick_flow": {"available": True, "delta_ratio": 0.45, "count": 90, "seconds": 30, "source": "test"},
                    "higher_timeframes": {"M5": {"available": True, "trend": "up"}, "M15": {"available": True, "trend": "up"}},
                    "volume_profile": {"available": True},
                    "derived_context": {"price_minus_poc_atr": 0.6, "price_minus_val_atr": 1.0, "price_minus_vah_atr": 0.2},
                    "technical": {"rsi7": 58.0},
                }
                v92_overlay = v92.entry_overlay(
                    v92_con, symbol="BTCUSDm", playbook="momentum_burst", side=1,
                    regime="trend", pre={"pre_score": 70.0, "hard_hold": False},
                    spartan_snapshot=v92_snap, micro={"available": True, "score": 0.55},
                )
                assert v92_overlay["score_delta"] > 0
                assert v92_overlay["risk_cap"] <= 0.25
                v92_applied = v92.apply_overlay({"pre_score": 70.0, "hard_hold": False}, v92_overlay)
                assert v92_applied["pre_score"] > 70.0
                v92_plan = v92.dynamic_exit_plan(
                    base_stop_atr=0.8, base_take_atr=1.2, atr_value=1.0,
                    frame=type("Frame", (), {"__getitem__": lambda self, key: type("S", (), {"astype": lambda self,*a,**k:self, "tail": lambda self,n:self, "tolist": lambda self:[100,100.2,100.4,100.7,101.0,101.2]})()})(),
                    grade="A", playbook="momentum_burst", memory={}, overlay=v92_overlay,
                )
                assert len(v92_plan["partials"]) == 2 and v92_plan["target_r"] > 0.5
            print("PASS: V9.2 microstructure/MTF/context overlays adjust score/risk without becoming serial vetoes")
            print("PASS: V9.2 dynamic exits expose partial-profit stages and new contexts start at tiny trial risk")
            # V10 fast expert router: broadens market-state coverage locally without
            # waiting for Luna or turning expert disagreements into serial vetoes.
            assert bool(getattr(settings, "V10_SUPERHUMAN_SCALPER_ENABLED", False))
            assert int(getattr(settings, "V9_MAX_TRADES_PER_SYMBOL_PER_DAY", 0)) == 100
            assert len(tuple(getattr(settings, "V10_EXPERT_ARCHETYPES", ()))) >= 8
            with sqlite3.connect(":memory:") as v10_con:
                v10_con.row_factory = sqlite3.Row
                v10.ensure_tables(v10_con)
                n=140
                close=np.full(n,100.0,dtype=float)
                close[-25:-2]=np.linspace(99.7,100.3,23)
                close[-2]=102.0
                open_=np.r_[close[0],close[:-1]]
                high=np.maximum(open_,close)+0.08
                low=np.minimum(open_,close)-0.08
                tick=np.full(n,100.0); tick[-2]=320.0
                vf=pd.DataFrame({"open":open_,"high":high,"low":low,"close":close,"tick_volume":tick})
                vf["ema_20"]=vf["close"].ewm(span=20,adjust=False).mean()
                vf["ema_50"]=vf["close"].ewm(span=50,adjust=False).mean()
                vf["atr_14"]=0.35
                vf["atr_ratio"]=vf["atr_14"]/vf["close"]
                vf["rsi_5"]=65.0; vf["rsi_7"]=62.0
                vf["momentum_3_atr"]=(vf["close"]-vf["close"].shift(3))/vf["atr_14"]
                vf["volume_ratio_20"]=vf["tick_volume"]/vf["tick_volume"].rolling(20).mean()
                vf["bar_body_atr"]=(vf["close"]-vf["open"]).abs()/vf["atr_14"]
                setup=v10.best_setup(v10_con,symbol="BTCUSDm",frame=vf,dom=0.35)
                assert setup is not None and setup.side==1
                assert setup.playbook in set(getattr(settings,"V10_EXPERT_ARCHETYPES"))
                assert setup.score >= float(getattr(settings,"V10_EXPERT_TRIGGER_MIN_SCORE",72.0))
            print("PASS: V10 eight-expert fast router can generate a local high-quality scalp candidate without Luna latency")
            print("PASS: V9.1 broker-outcome precision memory penalizes weak playbooks and caps recovery risk without martingale")
            print("PASS: V9 Micro Hunter alpha owns the DEMO entry; Spartan/SuperLearner/Luna are advisory risk evidence")
            print("PASS: V9 Luna HOLD reduces risk instead of acting as a serial kill switch")
            print("PASS: V9 severe negative Quant expectancy remains a hard execution hold")
            print("PASS: V9 cost-aware spread gate can accept reward-supported spread without exceeding its absolute ceiling")
        except Exception as error:
            traceback.print_exc()
            failures.append(f"{type(error).__name__}: {error}")
        finally:
            tm._INITIALIZED_DATABASE_PATH = None
            for key, value in original.items():
                setattr(settings, key, value)

    if failures:
        print("OFFLINE SELF TEST FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("OFFLINE SELF TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
