"""Offline deterministic tests. MT5 and API calls are ALWAYS stubbed here."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

# Never attach to an installed terminal, including when running on Windows.
stub = types.ModuleType("MetaTrader5")
stub.TIMEFRAME_M1 = 1
sys.modules["MetaTrader5"] = stub

import numpy as np
import pandas as pd
import settings
import trading_machine as tm
import scalp_advisor
import scalp_learning as sl
import scalp_runtime as rt
from scalp_execution import quote_problem, send_checked_deal
from scalp_policy import (ExitState, TradePlan, empirical_expectancy, evaluate_quote,
                          make_plan, net_virtual_reward, reward_summary)


STAMP = 1_780_000_000_000


def plan(**kwargs):
    defaults = dict(symbol="TEST", playbook="expert:trend_pullback", side=1, entry=100.,
                    atr=1., stop_atr=1., take_atr=2., max_hold_bars=5,
                    opened_msc=STAMP, price_step=.01, spread=.02,
                    commission_r=.03, slippage_r=.02)
    return make_plan(**{**defaults, **kwargs})


class PolicyTests(unittest.TestCase):
    def test_bad_geometry_rejected(self):
        with self.assertRaises(ValueError):
            TradePlan.from_dict({**plan().to_dict(), "side": 0})

    def test_nan_atr_rejected(self):
        with self.assertRaises(ValueError):
            plan(atr=float("nan"))

    def test_roundtrip(self):
        p = plan()
        self.assertEqual(p, TradePlan.from_dict(p.to_dict()))

    def test_buy_exits_at_bid(self):
        p = plan()
        action = evaluate_quote(p, ExitState.initial(p), bid=98.8, ask=98.82, time_msc=STAMP+1)
        self.assertEqual(action.reason, "stop_loss")
        self.assertAlmostEqual(action.gross_r, -1.2)

    def test_sell_exits_at_ask(self):
        p = plan(side=-1)
        action = evaluate_quote(p, ExitState.initial(p), bid=101.18, ask=101.2, time_msc=STAMP+1)
        self.assertEqual(action.reason, "stop_loss")
        self.assertAlmostEqual(action.gross_r, -1.2)

    def test_gap_does_not_assume_perfect_stop_fill(self):
        p = plan()
        a = evaluate_quote(p, ExitState.initial(p), bid=97, ask=97.02, time_msc=STAMP+1)
        self.assertEqual(a.gross_r, -3)

    def test_fees_net_without_double_spread(self):
        p = plan()
        a = evaluate_quote(p, ExitState.initial(p), bid=102, ask=102.02, time_msc=STAMP+1)
        self.assertAlmostEqual(net_virtual_reward(p, a), 1.95)

    def test_no_tiny_profit_grab(self):
        p = plan()
        a = evaluate_quote(p, ExitState.initial(p), bid=100.3, ask=100.32, time_msc=STAMP+100)
        self.assertEqual(a.action, "hold")

    def test_amend_needs_ack(self):
        p = plan()
        state = ExitState.initial(p)
        a = evaluate_quote(p, state, bid=100.9, ask=100.92, time_msc=STAMP+100)
        self.assertEqual(a.action, "amend_stop")
        self.assertEqual(state.active_stop, p.stop)

    def test_stop_never_widens(self):
        for side in (-1, 1):
            p = plan(side=side)
            state = ExitState.initial(p)
            previous = p.stop
            for i, gain in enumerate((.9, 1.3, 1.1, 1.5, 1.2)):
                mark = p.entry+side*gain
                bid, ask = (mark, mark+.02) if side == 1 else (mark-.02, mark)
                a = evaluate_quote(p, state, bid=bid, ask=ask, time_msc=STAMP+i+1)
                if a.action == "amend_stop":
                    self.assertGreaterEqual(side*(a.new_stop-previous), 0)
                    state.active_stop, previous = a.new_stop, a.new_stop

    def test_time_exit_even_with_unchanged_quote(self):
        p = plan(max_hold_bars=1)
        a = evaluate_quote(p, ExitState.initial(p), bid=100, ask=100.02,
                           time_msc=STAMP, now_msc=STAMP+60000)
        self.assertEqual(a.reason, "time_exit")

    def test_adverse_quote_flow_does_not_require_dom(self):
        p = plan()
        a = evaluate_quote(p, ExitState.initial(p), bid=99.3, ask=99.32,
                           time_msc=STAMP+30000, quote_flow=-.9, flow_samples=16)
        self.assertEqual(a.reason, "adverse_quote_flow")

    def test_inadequate_flow_is_not_fabricated(self):
        p = plan()
        a = evaluate_quote(p, ExitState.initial(p), bid=99.3, ask=99.32,
                           time_msc=STAMP+30000, quote_flow=-.9, flow_samples=2)
        self.assertEqual(a.action, "hold")

    def test_out_of_order_and_invalid_quote(self):
        p = plan()
        state = ExitState.initial(p)
        self.assertEqual(evaluate_quote(p, state, bid=100, ask=101, time_msc=STAMP-1).reason, "out_of_order_quote")
        self.assertEqual(evaluate_quote(p, state, bid=100, ask=99, time_msc=STAMP).reason, "invalid_quote")
        self.assertEqual(state.last_msc, STAMP)

    def test_same_timestamp_distinct_quotes_processed(self):
        p = plan()
        state = ExitState.initial(p)
        evaluate_quote(p, state, bid=100, ask=100.02, time_msc=STAMP)
        a = evaluate_quote(p, state, bid=99, ask=99.02, time_msc=STAMP)
        self.assertEqual(a.action, "close")

    def test_replay_does_not_amend_between_real_polls(self):
        p = plan()
        a = evaluate_quote(p, ExitState.initial(p), bid=101.5, ask=101.52,
                           time_msc=STAMP+100, allow_management=False)
        self.assertEqual(a.action, "hold")

    def test_tiny_winners_can_still_be_negative_expectancy(self):
        samples = [dict(event_key=str(i), source="broker", reward_r=.1 if i < 8 else -1) for i in range(10)]
        e = empirical_expectancy(samples)
        self.assertEqual(e["observed"]["win_rate"], .8)
        self.assertLess(e["expected_net_r"], 0)

    def test_broker_supersedes_matching_shadow(self):
        e = empirical_expectancy([dict(event_key="a", source="shadow", reward_r=3),
                                  dict(event_key="a", source="broker", reward_r=-1)])
        self.assertEqual(e["unique_events"], 1)
        self.assertEqual(e["broker_n"], 1)
        self.assertLess(e["expected_net_r"], 0)

    def test_legacy_and_incomplete_excluded(self):
        e = empirical_expectancy([dict(event_key="a", source="legacy", reward_r=20),
                                  dict(event_key="b", source="shadow", reward_r=20, quality="gap")])
        self.assertEqual(e["unique_events"], 0)
        self.assertEqual(e["source"], "unproven")

    def test_raw_score_does_not_override_economic_router(self):
        low_score = dict(key="a", mode="evidence_entry", evidence={"expected_net_r": .3, "effective_n": 10}, raw_score=72)
        high_score = dict(key="b", mode="demo_probe", evidence={"expected_net_r": 0, "effective_n": 0}, raw_score=99)
        self.assertIs(rt.choose_candidate([high_score, low_score]), low_score)

    def test_negative_and_unknown_have_different_modes(self):
        e = empirical_expectancy([dict(event_key=str(i), source="broker", reward_r=-1) for i in range(12)])
        self.assertEqual(rt.economic_mode(e)[0], "shadow_only")
        self.assertEqual(rt.economic_mode(empirical_expectancy([]))[0], "demo_probe")

    def test_shadow_only_does_not_claim_broker_validation(self):
        e = empirical_expectancy([dict(event_key=str(i), source="shadow", reward_r=.8) for i in range(100)])
        self.assertEqual(rt.economic_mode(e)[0], "demo_probe")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        sl.ensure_tables(self.con)

    def tearDown(self):
        self.con.close()

    def observe(self, key="a", p=None):
        return sl.observe_pair(self.con, key=key, plan=p or plan(), regime="trend", raw_score=80, bar_msc=STAMP-60000)

    def test_paired_prospective_open_idempotent(self):
        self.assertTrue(self.observe())
        self.assertFalse(self.observe())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_virtual_positions").fetchone()[0], 2)

    def test_no_overlapping_clones(self):
        self.observe()
        self.assertFalse(self.observe("b"))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_virtual_positions").fetchone()[0], 2)

    def test_gap_never_generates_training_labels(self):
        self.observe()
        self.assertEqual(sl.invalidate_open_trials(self.con, "TEST", "missing_ticks"), 2)
        sl.replay_quotes(self.con, "TEST", [dict(time_msc=STAMP+100, bid=103, ask=103.02)])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_outcomes").fetchone()[0], 0)

    def test_true_tick_order_changes_outcome(self):
        self.observe()
        sl.replay_quotes(self.con, "TEST", [dict(time_msc=STAMP+1, bid=99, ask=99.02), dict(time_msc=STAMP+2, bid=102, ask=102.02)])
        self.assertTrue(all(r[0] < 0 for r in self.con.execute("SELECT reward_r FROM v11_outcomes")))

    def test_result_idempotent(self):
        args = dict(key="one", plan=plan(), regime="trend", source="broker", reward_r=.3, closed_msc=STAMP+1, reason="tp")
        self.assertTrue(sl.record_outcome(self.con, **args))
        self.assertFalse(sl.record_outcome(self.con, **args))

    def test_same_policy_cost_increase_only(self):
        p = plan()
        for i in range(10):
            sl.record_outcome(self.con, key=str(i), plan=p, regime="trend", source="broker", reward_r=.3, closed_msc=STAMP+i+1, reason="tp")
        before = sl.evidence_for_plan(self.con, p, "trend")
        same = sl.evidence_for_plan(self.con, p, "range")
        more = sl.evidence_for_plan(self.con, replace(p, commission_r=.13), "trend")
        self.assertAlmostEqual(before["expected_net_r"], same["expected_net_r"])
        self.assertAlmostEqual(before["expected_net_r"]-more["expected_net_r"], .10)

    def pairs(self, n, balanced, runner):
        for i in range(n):
            stamp = STAMP+(i//20)*86400000+i*1000
            for variant, reward in (("balanced", balanced), ("runner", runner)):
                r = reward(i) if callable(reward) else reward
                sl.record_outcome(self.con, key=str(i), plan=plan(variant=variant), regime="trend", source="shadow",
                                  reward_r=r, closed_msc=stamp, reason="synthetic_test")

    def test_small_sample_cannot_promote(self):
        self.pairs(12, .1, .9)
        self.assertFalse(sl.evaluate_variant(self.con, "TEST", "expert:trend_pullback", 1)["promoted"])

    def test_less_bad_but_negative_cannot_promote(self):
        self.pairs(60, -.6, -.1)
        self.assertFalse(sl.evaluate_variant(self.con, "TEST", "expert:trend_pullback", 1)["promoted"])

    def test_bad_holdout_cannot_promote(self):
        self.pairs(60, .1, lambda i: .9 if i < 40 else -.2)
        self.assertFalse(sl.evaluate_variant(self.con, "TEST", "expert:trend_pullback", 1)["promoted"])

    def test_positive_paired_later_holdout_can_promote_once(self):
        self.pairs(60, .1, .4)
        result = sl.evaluate_variant(self.con, "TEST", "expert:trend_pullback", 1)
        self.assertTrue(result["promoted"])
        self.assertEqual(sl.active_variant(self.con, "TEST", "expert:trend_pullback", 1), "runner")
        sl.evaluate_variant(self.con, "TEST", "expert:trend_pullback", 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_policy_audits").fetchone()[0], 1)

    def test_probe_cap_counts_unknown_and_not_rejected(self):
        sl.update_probe(self.con, "a", "TEST", "unknown")
        sl.update_probe(self.con, "b", "TEST", "rejected")
        allowed, status = sl.probe_capacity(self.con, "TEST", timestamp_msc=sl.now_msc(), per_symbol=1, total_cap=10)
        self.assertFalse(allowed)
        self.assertEqual(status["total"], 1)


class AcknowledgementTests(unittest.TestCase):
    def broker(self):
        return NS(order_check=Mock(return_value=NS(retcode=0)), order_send=Mock())

    def test_no_ack_means_one_send_only(self):
        broker = self.broker()
        broker.order_send.return_value = None
        result = send_checked_deal(broker, {"symbol": "TEST"}, [2, 1, 0])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(broker.order_send.call_count, 1)

    def test_timeout_means_one_send_only(self):
        broker = self.broker()
        broker.order_send.return_value = NS(retcode=10012)
        self.assertEqual(send_checked_deal(broker, {}, [2, 1, 0])["state"], "unknown")
        self.assertEqual(broker.order_send.call_count, 1)

    def test_exception_after_send_is_unknown(self):
        broker = self.broker()
        broker.order_send.side_effect = TimeoutError()
        self.assertEqual(send_checked_deal(broker, {}, [2, 1, 0])["state"], "unknown")
        self.assertEqual(broker.order_send.call_count, 1)

    def test_only_invalid_fill_is_retried(self):
        broker = self.broker()
        broker.order_send.side_effect = [NS(retcode=10030), NS(retcode=10009, price=100)]
        self.assertEqual(send_checked_deal(broker, {}, [2, 1, 0])["state"], "sent")
        self.assertEqual(broker.order_send.call_count, 2)

    def test_rejection_is_not_blindly_retried(self):
        broker = self.broker()
        broker.order_send.return_value = NS(retcode=10019)
        self.assertEqual(send_checked_deal(broker, {}, [2, 1, 0])["state"], "rejected")
        self.assertEqual(broker.order_send.call_count, 1)

    def test_failed_check_never_sends(self):
        broker = self.broker()
        broker.order_check.return_value = None
        self.assertEqual(send_checked_deal(broker, {}, [2, 1, 0])["state"], "rejected")
        broker.order_send.assert_not_called()

    def test_stale_future_and_invalid_quotes(self):
        self.assertEqual(quote_problem(NS(bid=1, ask=2, time_msc=STAMP-20000), now_msc=STAMP, max_age_seconds=10), "stale_tick")
        self.assertEqual(quote_problem(NS(bid=1, ask=2, time_msc=STAMP+5000), now_msc=STAMP, max_age_seconds=10), "future_tick_clock_mismatch")
        self.assertEqual(quote_problem(NS(bid=2, ask=1, time_msc=STAMP), now_msc=STAMP, max_age_seconds=10), "invalid_bid_ask")


class FakeBroker:
    TIMEFRAME_M1 = 1
    ACCOUNT_TRADE_MODE_DEMO = 0
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TIME_GTC = 0, 1, 0
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
    TRADE_RETCODE_DONE = 10009
    COPY_TICKS_ALL = -1

    def __init__(self):
        self.account = NS(equity=10000., balance=10000., trade_mode=0, trade_allowed=True)
        self.info = NS(visible=True, point=.01, trade_tick_size=.01, trade_stops_level=0, trade_freeze_level=0,
                       volume_min=.01, volume_step=.01, volume_max=100., digits=2)
        self.tick = NS(bid=100., ask=100.02, time_msc=sl.now_msc())
        self.positions = []
        self.deals = []
        self.sent = []
        self.ack = "success"
        self.tick_rows = []
        self.bars = None

    def terminal_info(self):
        return NS(connected=True, trade_allowed=True)

    def account_info(self):
        return self.account

    def positions_get(self):
        return tuple(self.positions)

    def orders_get(self, **kwargs):
        return ()

    def symbol_info_tick(self, symbol):
        return self.tick

    def symbol_info(self, symbol):
        return self.info

    def symbol_select(self, symbol, visible):
        return True

    def order_calc_profit(self, side, symbol, volume, entry, stop):
        return (stop-entry)*(1 if side == 0 else -1)*volume*100

    def order_check(self, request):
        return NS(retcode=0)

    def order_send(self, request):
        self.sent.append(dict(request))
        if self.ack == "unknown":
            return None
        if request["action"] == self.TRADE_ACTION_SLTP:
            self.positions[0].sl = request["sl"]
            return NS(retcode=10009)
        if "position" in request:
            pos = self.positions.pop(0)
            self.deals.append(NS(position_id=pos.ticket, entry=1, profit=10., commission=-.1, fee=0., swap=0.,
                price=request["price"], volume=pos.volume, time=int(sl.now_msc()/1000), time_msc=sl.now_msc(),
                reason=3, symbol=pos.symbol, magic=settings.MAGIC_NUMBER, comment=request["comment"], order=pos.ticket+1))
            return NS(retcode=10009, order=pos.ticket+1, price=request["price"])
        ticket = 111
        pos = NS(ticket=ticket, identifier=ticket, symbol=request["symbol"], price_open=request["price"]+.01,
            volume=request["volume"], sl=request["sl"], tp=request["tp"], comment=request["comment"],
            magic=settings.MAGIC_NUMBER, time_msc=sl.now_msc(), type=request["type"])
        self.positions.append(pos)
        self.deals.append(NS(position_id=ticket, entry=0, profit=0., commission=-.1, fee=0., swap=0.,
            price=pos.price_open, volume=pos.volume, time=int(sl.now_msc()/1000), time_msc=pos.time_msc,
            reason=3, symbol=pos.symbol, magic=settings.MAGIC_NUMBER, comment=pos.comment, order=ticket))
        return NS(retcode=10009, order=ticket, price=pos.price_open)

    def history_deals_get(self, *args, **kwargs):
        if "position" in kwargs:
            return tuple(d for d in self.deals if d.position_id == kwargs["position"])
        return tuple(self.deals)

    def copy_rates_from_pos(self, *args):
        return self.bars

    def copy_ticks_range(self, *args):
        if self.tick_rows is None:
            return None
        return self.tick_rows or [dict(time_msc=self.tick.time_msc, bid=self.tick.bid, ask=self.tick.ask)]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v11_offline_")
        root = Path(self.temp.name)
        self.settings_patch = patch.multiple(settings, DATABASE_PATH=root/"test.db", REPORTS_DIR=root/"reports",
            DATA_DIR=root/"data", LOGS_DIR=root/"logs", SYMBOLS=["TEST"], V11_ASYNC_LUNA_ENABLED=False,
            SPARTAN_LLM_REVIEW_ENABLED=False)
        self.settings_patch.start()
        tm._INITIALIZED_DATABASE_PATH = None
        tm.init_database()
        self.broker = FakeBroker()
        self.broker_patch = patch.object(tm, "mt5", self.broker)
        self.broker_patch.start()
        self.runtime = rt.Runtime(tm, acquire_lock=False)
        self.runtime_patch = patch.object(rt, "_RUNTIME", self.runtime)
        self.runtime_patch.start()
        self.posttrade_patch = patch.object(tm, "spartan_posttrade_enqueue", return_value=None)
        self.posttrade_patch.start()
        self.con = sqlite3.connect(settings.DATABASE_PATH)
        self.con.row_factory = sqlite3.Row
        self.setup = tm.v10.V10Setup(symbol="TEST", playbook="trend_pullback", side=1, score=90,
                                     stop_atr=1., take_atr=2., max_hold_bars=1)
        n = 360
        close = 100+np.sin(np.arange(n)/9)*.2
        last = pd.Timestamp.now(tz="UTC").floor("min")
        self.frame = tm.add_features(pd.DataFrame(dict(time=pd.date_range(end=last, periods=n, freq="min"),
            open=close, high=close+.5, low=close-.5, close=close, tick_volume=np.full(n, 100),
            spread=np.full(n, 2), real_volume=np.zeros(n))), {5, 13, 20, 50, 200})
        self.runtime.frames["TEST"] = dict(frame=self.frame, bar=int(self.frame.iloc[-2]["time"].timestamp()*1000), fetched=1e30)
        p = rt.plan_for(self.setup, self.broker.tick, self.broker.info, float(self.frame.iloc[-2]["atr_14"]), "balanced", .03, .03)
        self.candidate = dict(key="test_key", setup=self.setup, plan=p, evidence=empirical_expectancy([]), mode="demo_probe",
                              regime="trend", bar_msc=p.opened_msc-60000, created_msc=sl.now_msc())
        self.runtime.candidates["TEST"] = [self.candidate]

    def tearDown(self):
        self.con.close()
        self.runtime.close()
        self.posttrade_patch.stop()
        self.runtime_patch.stop()
        self.broker_patch.stop()
        self.settings_patch.stop()
        tm._INITIALIZED_DATABASE_PATH = None
        self.temp.cleanup()

    def enter(self):
        self.runtime.entry(self.con, "TEST", self.candidate, self.broker.account, [], {}, {"risk_multiplier": 1.0})
        self.con.commit()

    def bind(self):
        self.runtime.bind_intents(self.con, list(self.broker.positions))
        self.con.commit()

    def test_end_to_end_plan_fill_time_exit_net_learning(self):
        self.enter()
        self.assertEqual(len(self.broker.sent), 1)
        self.bind()
        row = self.con.execute("SELECT * FROM demo_positions").fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["entry_price"], self.broker.positions[0].price_open)
        context = json.loads(row["context_json"])
        context["v11"]["plan"]["opened_msc"] = sl.now_msc()-61000
        context["v11"]["state"]["last_msc"] = sl.now_msc()-61000
        self.con.execute("UPDATE demo_positions SET context_json=?", (json.dumps(context),))
        tm.spartan_manage_demo_positions(self.con)
        self.assertEqual(len(self.broker.sent), 2)
        self.assertIn("time_exit", self.broker.sent[-1]["comment"])
        tm.reconcile_demo_positions(self.con)
        outcome = self.con.execute("SELECT * FROM v11_outcomes WHERE source='broker'").fetchone()
        self.assertAlmostEqual(outcome["pnl"], 9.8)  # BOTH entry and exit commission
        self.assertEqual(outcome["reason"], "time_exit")
        self.assertEqual(self.con.execute("SELECT status FROM demo_positions").fetchone()[0], "closed")
        tm.reconcile_demo_positions(self.con)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_outcomes").fetchone()[0], 1)

    def test_unknown_order_persists_and_blocks_duplicate_after_restart(self):
        self.broker.ack = "unknown"
        self.enter()
        self.assertEqual(self.con.execute("SELECT state FROM v11_order_intents").fetchone()[0], "unknown")
        restarted = rt.Runtime(tm, acquire_lock=False)
        restarted.frames = self.runtime.frames
        restarted.entry(self.con, "TEST", {**self.candidate, "key": "different"}, self.broker.account, [], {}, {"risk_multiplier": 1})
        self.assertEqual(len(self.broker.sent), 1)
        restarted.close()

    def test_ack_recovers_exact_position_without_second_order(self):
        self.enter()
        self.bind()
        self.bind()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM demo_positions").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT state FROM v11_order_intents").fetchone()[0], "bound")
        self.assertEqual(len(self.broker.sent), 1)

    def test_real_account_always_blocked_even_if_legacy_setting_off(self):
        self.broker.account.trade_mode = 2
        with patch.object(settings, "DEMO_ONLY_HARD_LOCK", False), self.assertRaises(RuntimeError):
            self.enter()
        self.assertFalse(self.broker.sent)

    def test_minimum_lot_above_probe_cap_holds(self):
        self.broker.info.volume_min = 5.
        self.enter()
        self.assertFalse(self.broker.sent)
        row = self.con.execute("SELECT reason FROM v11_decision_events WHERE stage='sizing'").fetchone()
        self.assertIn("risk", row[0])

    def test_stale_quote_cannot_trade(self):
        self.broker.tick.time_msc -= 11000
        self.enter()
        self.assertFalse(self.broker.sent)

    def test_news_lock_cannot_trade(self):
        with patch.object(tm.spartan, "news_status", return_value={"locked": True}):
            self.enter()
        self.assertFalse(self.broker.sent)

    def test_full_spread_gate_on_fresh_quote(self):
        self.broker.tick.ask += .5
        self.enter()
        self.assertFalse(self.broker.sent)

    def test_temporary_missing_tick_path_is_retried_without_invalidating(self):
        p = self.candidate["plan"]
        sl.observe_pair(self.con, key="v", plan=p, regime="trend", raw_score=80, bar_msc=p.opened_msc-60000)
        cursor = p.opened_msc-1000
        sl.set_state(self.con, "ticks:TEST", dict(time_msc=cursor, boundary=[]))
        self.broker.tick_rows = None
        self.runtime.replay(self.con, "TEST", self.broker.tick)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_virtual_positions WHERE status='open'").fetchone()[0], 2)
        self.assertEqual(sl.get_state(self.con, "ticks:TEST")["time_msc"], cursor)
        self.assertEqual(sl.get_state(self.con, "tick_health:TEST")["status"], "retry_pending")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_outcomes").fetchone()[0], 0)

    def test_tick_catchup_limit_excludes_unverifiable_path(self):
        p = self.candidate["plan"]
        sl.observe_pair(self.con, key="v", plan=p, regime="trend", raw_score=80, bar_msc=p.opened_msc-60000)
        sl.set_state(self.con, "ticks:TEST", dict(time_msc=self.broker.tick.time_msc-121000, boundary=[]))
        self.broker.tick_rows = None
        self.runtime.replay(self.con, "TEST", self.broker.tick)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_virtual_positions WHERE status='incomplete'").fetchone()[0], 2)
        self.assertEqual(sl.get_state(self.con, "ticks:TEST")["time_msc"], self.broker.tick.time_msc)

    def test_lagging_tick_history_advances_only_to_returned_tick_then_catches_up(self):
        p = self.candidate["plan"]
        sl.observe_pair(self.con, key="v", plan=p, regime="trend", raw_score=80, bar_msc=p.opened_msc-60000)
        start = self.broker.tick.time_msc-1000
        lagged = self.broker.tick.time_msc-100
        sl.set_state(self.con, "ticks:TEST", dict(time_msc=start, boundary=["100|100.02"]))
        self.broker.tick_rows = [dict(time_msc=start, bid=100, ask=100.02),
                                 dict(time_msc=lagged, bid=100.4, ask=100.42)]
        self.runtime.replay(self.con, "TEST", self.broker.tick)
        self.assertEqual(sl.get_state(self.con, "ticks:TEST")["time_msc"], lagged)
        self.assertEqual(sl.get_state(self.con, "tick_health:TEST")["status"], "catchup_pending")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_virtual_positions WHERE status='open'").fetchone()[0], 2)
        self.broker.tick_rows = [dict(time_msc=lagged, bid=100.4, ask=100.42),
                                 dict(time_msc=self.broker.tick.time_msc, bid=103, ask=103.02)]
        self.runtime.replay(self.con, "TEST", self.broker.tick)
        self.assertEqual(sl.get_state(self.con, "tick_health:TEST")["status"], "caught_up")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_outcomes WHERE source='shadow'").fetchone()[0], 2)

    def test_duplicate_tick_boundary_is_not_double_counted(self):
        self.runtime.replay(self.con, "TEST", self.broker.tick)
        self.broker.tick.bid += .01
        self.broker.tick.ask += .01
        self.broker.tick_rows = [dict(time_msc=self.broker.tick.time_msc, bid=100, ask=100.02),
                                 dict(time_msc=self.broker.tick.time_msc, bid=100.01, ask=100.03)]
        self.runtime.replay(self.con, "TEST", self.broker.tick)
        self.assertEqual(len(self.runtime.flow["TEST"]), 1)
        self.runtime.replay(self.con, "TEST", self.broker.tick)
        self.assertEqual(len(self.runtime.flow["TEST"]), 1)

    def test_observation_runs_before_global_risk_block(self):
        self.runtime.initialized = True
        observed = []
        with patch.object(self.runtime, "observe", side_effect=lambda con, symbol, tick, info, **kw: observed.append(symbol)), \
             patch.object(tm, "risk_guard", return_value=(False, "Daily loss limit reached", {})), \
             patch.object(tm.capital_growth, "update_state", return_value={}), \
             patch.object(self.runtime, "entry") as enter:
            self.runtime.cycle()
        self.assertEqual(observed, ["TEST"])
        enter.assert_not_called()

    def test_observation_runs_before_symbol_freeze(self):
        self.runtime.initialized = True
        observed = []
        with patch.object(self.runtime, "observe", side_effect=lambda con, symbol, tick, info, **kw: observed.append(symbol)), \
             patch.object(tm, "risk_guard", return_value=(True, "OK", {})), \
             patch.object(tm, "symbol_loss_guard", return_value=(False, "loss-streak freeze", {})), \
             patch.object(tm.capital_growth, "update_state", return_value={"allow_new_entries": True}), \
             patch.object(self.runtime, "entry") as enter:
            self.runtime.cycle()
        self.assertEqual(observed, ["TEST"])
        enter.assert_not_called()

    def test_cycle_falls_through_blocked_candidate_to_viable_candidate(self):
        self.runtime.initialized = True
        self.broker.info.trade_stops_level = 50
        bad_setup = replace(self.setup, stop_atr=.35, take_atr=.50)
        bad_plan = rt.plan_for(bad_setup, self.broker.tick, self.broker.info,
                               float(self.frame.iloc[-2]["atr_14"]), "balanced", .03, .03)
        bad = {**self.candidate, "key": "a_bad", "setup": bad_setup, "plan": bad_plan}
        good = {**self.candidate, "key": "z_good"}
        self.runtime.candidates["TEST"] = [bad, good]
        with patch.object(self.runtime, "observe"), \
             patch.object(tm, "risk_guard", return_value=(True, "OK", {})), \
             patch.object(tm, "symbol_loss_guard", return_value=(True, "OK", {})), \
             patch.object(tm.capital_growth, "update_state", return_value={"allow_new_entries": True, "risk_multiplier": 1}), \
             patch.object(tm, "cooldown_ready", return_value=True):
            self.runtime.cycle()
        self.assertEqual(len(self.broker.sent), 1)
        self.assertEqual(self.con.execute("SELECT event_key FROM v11_order_intents").fetchone()[0], "z_good")

    def test_active_superlearner_strong_conflict_vetoes_candidate(self):
        with patch.object(tm, "online_model_probability", return_value={
                "probability": .20, "active": True, "global_updates": 100, "symbol_updates": 50}):
            disposition = self.runtime.entry(
                self.con, "TEST", self.candidate, self.broker.account, [], {}, {"risk_multiplier": 1.0})
        self.assertEqual(disposition, "next_candidate")
        self.assertTrue(self.candidate.get("consumed"))
        self.assertFalse(self.broker.sent)
        self.assertEqual(self.con.execute(
            "SELECT reason FROM v11_decision_events WHERE stage='superlearner'").fetchone()[0],
            "strong_broker_net_conflict")

    def test_luna_veto_has_real_candidate_effect(self):
        with patch.object(scalp_advisor, "pretrade_gate", return_value={
                "state": "veto", "reason": "hold", "review": {"decision": "hold", "confidence": .8}}):
            disposition = self.runtime.entry(
                self.con, "TEST", self.candidate, self.broker.account, [], {}, {"risk_multiplier": 1.0})
        self.assertEqual(disposition, "next_candidate")
        self.assertTrue(self.candidate.get("consumed"))
        self.assertFalse(self.broker.sent)

    def test_luna_confirm_is_persisted_before_order(self):
        with patch.object(scalp_advisor, "pretrade_gate", return_value={
                "state": "confirm", "reason": "confirm", "review": {"decision": "confirm", "confidence": .91},
                "fingerprint": "fp", "review_id": 7}):
            disposition = self.runtime.entry(
                self.con, "TEST", self.candidate, self.broker.account, [], {}, {"risk_multiplier": 1.0})
        self.assertEqual(disposition, "order_attempted")
        payload = json.loads(self.con.execute("SELECT request_json FROM v11_order_intents").fetchone()[0])
        self.assertEqual(payload["context"]["spartan_llm"]["fingerprint"], "fp")
        self.assertEqual(len(self.broker.sent), 1)

    def test_existing_symbol_position_blocks_entry(self):
        self.runtime.initialized = True
        self.broker.positions = [NS(ticket=23, magic=999, symbol="TEST")]
        with patch.object(self.runtime, "observe"), patch.object(tm.capital_growth, "update_state", return_value={}), \
             patch.object(self.runtime, "entry") as enter:
            self.runtime.cycle()
        enter.assert_not_called()

    def test_sixth_loss_restarts_existing_five_loss_freeze(self):
        self.enter()
        self.bind()
        row = self.con.execute("SELECT * FROM demo_positions").fetchone()
        epoch = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        tm.state_set(self.con, "v9_2_1_symbol_loss_epoch", epoch)
        strategy = tm.load_strategy(self.con.execute("SELECT id,symbol,family,params_json FROM strategies WHERE id=?", (row["strategy_id"],)).fetchone())
        for i in range(5):
            tm.register_demo_position(self.con, 200+i, strategy, "trend", 1, .01, 100, 99, 102, 1, NS(order=200+i),
                                      rt.TIER, {}, .5, .2)
        self.con.execute("UPDATE demo_positions SET status='closed', reward_r=-1, closed_at=?", (tm.utc_now(),))
        allowed, _, state = tm.symbol_loss_guard(self.con, "TEST")
        self.assertFalse(allowed)
        self.assertEqual(state["loss_streak"], 6)
        self.assertGreater(state["freeze_remaining_seconds"], 890)
        old = (datetime.now(timezone.utc)-timedelta(seconds=901)).isoformat()
        self.con.execute("UPDATE demo_positions SET closed_at=?", (old,))
        self.assertTrue(tm.symbol_loss_guard(self.con, "TEST")[0])
        self.con.execute("UPDATE demo_positions SET closed_at=? WHERE id=(SELECT MAX(id) FROM demo_positions)", (tm.utc_now(),))
        self.assertFalse(tm.symbol_loss_guard(self.con, "TEST")[0])

    def test_daily_capacity_includes_v11_and_legacy(self):
        self.enter()
        self.bind()
        self.assertEqual(tm.v9_exec.daily_trade_counts(self.con, "TEST"), (1, 1))

    def test_optional_luna_no_key_has_no_thread_or_api(self):
        with patch.object(settings, "OPENAI_API_KEY", ""), patch.object(scalp_advisor.threading, "Thread") as thread:
            self.assertEqual(scalp_advisor.enqueue({}), "optional_advisor_disabled_or_no_key")
            thread.assert_not_called()

    def test_multiple_expert_candidates_are_observed_without_broker_entry(self):
        experts = [dict(playbook="trend_pullback", side=1, score=90, stop_atr=1, take_atr=2, max_hold_bars=5),
                   dict(playbook="range_edge_rotation", side=-1, score=76, stop_atr=.8, take_atr=1.3, max_hold_bars=4)]
        self.runtime.candidates.clear()
        with patch.object(tm.v10, "_score_components", return_value=(experts, {"regime": "range"})), \
             patch.object(tm.micro_hunter, "observe_closed_bar", return_value=None):
            self.runtime.observe(self.con, "TEST", self.broker.tick, self.broker.info)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_virtual_positions").fetchone()[0], 4)
        self.assertEqual(len(self.runtime.candidates["TEST"]), 2)
        self.assertFalse(self.broker.sent)

    def test_missing_beginning_of_tick_path_excluded(self):
        p = self.candidate["plan"]
        sl.observe_pair(self.con, key="v", plan=p, regime="trend", raw_score=80, bar_msc=p.opened_msc-60000)
        sl.set_state(self.con, "ticks:TEST", dict(time_msc=p.opened_msc-1000, boundary=["99.98|100"]))
        self.runtime.replay(self.con, "TEST", self.broker.tick)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_virtual_positions WHERE status='incomplete'").fetchone()[0], 2)

    def test_pending_broker_orders_block_entry(self):
        with patch.object(self.broker, "orders_get", return_value=(NS(ticket=4),)):
            self.enter()
        self.assertFalse(self.broker.sent)

    def test_missing_order_list_blocks_entry(self):
        with patch.object(self.broker, "orders_get", return_value=None):
            self.enter()
        self.assertFalse(self.broker.sent)

    def test_missing_protective_stop_requests_close(self):
        self.enter()
        self.bind()
        self.broker.positions[0].sl = 0
        tm.spartan_manage_demo_positions(self.con)
        self.assertEqual(self.broker.sent[-1]["position"], 111)
        row = self.con.execute("SELECT context_json FROM demo_positions").fetchone()
        self.assertEqual(json.loads(row[0])["v11"]["requested_close_reason"], "missing_broker_protective_stop")

    def test_unknown_close_is_not_retried(self):
        self.enter()
        self.bind()
        self.broker.ack = "unknown"
        self.broker.positions[0].sl = 0
        tm.spartan_manage_demo_positions(self.con)
        tm.spartan_manage_demo_positions(self.con)
        self.assertEqual(len(self.broker.sent), 2)  # one entry + one close, never a third

    def test_later_partial_entry_fills_update_actual_risk_basis(self):
        self.enter()
        self.bind()
        old = self.con.execute("SELECT risk_cash FROM demo_positions").fetchone()[0]
        self.broker.positions[0].volume *= 2
        tm.spartan_manage_demo_positions(self.con)
        new = self.con.execute("SELECT risk_cash FROM demo_positions").fetchone()[0]
        self.assertAlmostEqual(new, 2*old)

    def test_filled_before_registration_can_recover_from_deals(self):
        self.enter()
        self.broker.positions = []
        self.bind()
        row = self.con.execute("SELECT position_ticket,entry_price FROM demo_positions").fetchone()
        self.assertEqual(row[0], 111)
        self.assertAlmostEqual(row[1], self.broker.deals[0].price)

    def test_no_history_is_not_proof_of_unfilled_order(self):
        self.broker.ack = "unknown"
        self.enter()
        self.bind()
        self.assertEqual(self.con.execute("SELECT state FROM v11_order_intents").fetchone()[0], "unknown")

    def test_malformed_tick_order_excludes_virtuals(self):
        p = self.candidate["plan"]
        sl.observe_pair(self.con, key="v", plan=p, regime="trend", raw_score=80, bar_msc=p.opened_msc-60000)
        sl.set_state(self.con, "ticks:TEST", dict(time_msc=p.opened_msc-100, boundary=[]))
        self.broker.tick_rows = [dict(time_msc=p.opened_msc, bid=100, ask=100.02),
                                 dict(time_msc=p.opened_msc-1, bid=99, ask=99.02)]
        self.runtime.replay(self.con, "TEST", self.broker.tick)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_outcomes").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v11_virtual_positions WHERE status='incomplete'").fetchone()[0], 2)

    def test_status_counts_more_than_twenty_closes(self):
        import v11_status
        self.enter()
        self.bind()
        row = self.con.execute("SELECT * FROM demo_positions").fetchone()
        strategy = tm.load_strategy(self.con.execute("SELECT id,symbol,family,params_json FROM strategies WHERE id=?", (row["strategy_id"],)).fetchone())
        for i in range(24):
            tm.register_demo_position(self.con, 300+i, strategy, "trend", 1, .01, 100, 99, 102, 1, NS(order=300+i), rt.TIER, {}, .5, .2)
        self.con.execute("UPDATE demo_positions SET status='closed',reward_r=.2,pnl=.2,closed_at=?,opened_at=?",
            (tm.utc_now(), (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()))
        result = v11_status.collect(self.con, 6)
        self.assertEqual(result["broker"]["TEST:V11:broker"]["n"], 25)
        self.assertAlmostEqual(result["broker"]["TEST:V11:broker"]["sum_r"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
