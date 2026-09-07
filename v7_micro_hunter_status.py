from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import settings
import micro_scalp_hunter as hunter


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def main() -> int:
    print('=' * 82)
    print('V7.0 MICRO-SCALP HUNTER STATUS - DEMO ONLY')
    print('=' * 82)
    print('Architecture: RESEARCH BRAIN -> MICRO HUNTER -> HARD GATES -> SUPERLEARNER -> LUNA -> DEMO ORDER')
    print('Luna: one compact candidate packet in, one tiny structured answer out; no raw-tick polling.')
    print()
    print('THRESHOLDS')
    print(
        f"XAU watch/arm/trigger={settings.V7_MICRO_HUNTER_XAU_WATCH_SCORE:.0f}/"
        f"{settings.V7_MICRO_HUNTER_XAU_ARM_SCORE:.0f}/{settings.V7_MICRO_HUNTER_XAU_TRIGGER_SCORE:.0f} | "
        f"OIL={settings.V7_MICRO_HUNTER_OIL_WATCH_SCORE:.0f}/{settings.V7_MICRO_HUNTER_OIL_ARM_SCORE:.0f}/{settings.V7_MICRO_HUNTER_OIL_TRIGGER_SCORE:.0f} | "
        f"BTC={settings.V7_MICRO_HUNTER_BTC_WATCH_SCORE:.0f}/{settings.V7_MICRO_HUNTER_BTC_ARM_SCORE:.0f}/{settings.V7_MICRO_HUNTER_BTC_TRIGGER_SCORE:.0f}"
    )
    print(f"ARM TTL={settings.V7_MICRO_HUNTER_ARM_TTL_SECONDS}s | paid Luna cap/day={settings.SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY} | bot spend guard=${settings.SPARTAN_GPT_BOT_BUDGET_USD:.2f}")

    con = sqlite3.connect(settings.DATABASE_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        hunter.ensure_tables(con)
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        rows = con.execute(
            """
            SELECT symbol,playbook,side,phase,score,bar_time,triggered_at,outcome_r,strategy_id,execution_tier
            FROM micro_hunter_setups WHERE updated_at>=? ORDER BY id DESC
            """, (since,)
        ).fetchall()
        print('\nLAST 60 MIN HUNTER FUNNEL')
        if not rows:
            print('No WATCH/ARM/TRIGGER state yet. The hunter is waiting for a high-score 2-5 candle setup.')
        else:
            total = Counter(str(r['phase']) for r in rows)
            print('TOTAL:', ' | '.join(f'{k}={v}' for k,v in total.most_common()))
            by = defaultdict(Counter)
            for r in rows:
                by[str(r['symbol'])][str(r['phase'])] += 1
            for symbol in settings.SYMBOLS:
                c = by.get(symbol, Counter())
                print(symbol + ': ' + (' | '.join(f'{k}={v}' for k,v in c.most_common()) if c else 'none'))

        print('\nLATEST MICRO SETUPS')
        latest = con.execute(
            """
            SELECT symbol,playbook,side,phase,score,bar_time,strategy_id,execution_tier,outcome_r
            FROM micro_hunter_setups ORDER BY id DESC LIMIT 12
            """
        ).fetchall()
        if not latest:
            print('none')
        for r in latest:
            side = 'BUY' if int(r['side']) == 1 else 'SELL'
            outcome_text = "-" if r["outcome_r"] is None else f"{_f(r['outcome_r']):+.2f}"
            print(
                f"{r['bar_time']} | {r['symbol']} {side} {r['playbook']} "
                f"score={_f(r['score']):.1f} phase={r['phase']} "
                f"carrier={r['strategy_id'] or '-'} tier={r['execution_tier'] or '-'} "
                f"outcomeR={outcome_text}"
            )

        print('\nACTUAL DEMO PLAYBOOK MEMORY')
        mem = con.execute(
            """
            SELECT symbol,playbook,side,observations,wins,losses,reward_sum,reward_ewma,mfe_sum,mae_sum
            FROM micro_playbook_memory ORDER BY observations DESC,reward_ewma DESC LIMIT 20
            """
        ).fetchall()
        if not mem:
            print('No V7 micro-hunter broker-DEMO closes yet. Shadow/rejected research is NOT counted here.')
        for r in mem:
            n=max(1,int(r['observations'] or 0)); side='BUY' if int(r['side'])==1 else 'SELL'
            print(
                f"{r['symbol']} {side} {r['playbook']} n={r['observations']} "
                f"win={int(r['wins'] or 0)/n:.1%} mean={_f(r['reward_sum'])/n:+.3f}R "
                f"EWMA={_f(r['reward_ewma']):+.3f}R MFE={_f(r['mfe_sum'])/n:.2f}R MAE={_f(r['mae_sum'])/n:.2f}R"
            )

        print('\nLUNA - LAST 24H')
        since24=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
        try:
            g=con.execute(
                """SELECT COUNT(*) n,COALESCE(SUM(input_tokens),0) tin,COALESCE(SUM(output_tokens),0) tout
                   FROM gpt_usage_events WHERE timestamp>=? AND call_type='pretrade'""", (since24,)
            ).fetchone()
            print(f"pretrade calls={int(g['n'] or 0)} input_tokens={int(g['tin'] or 0)} output_tokens={int(g['tout'] or 0)}")
        except sqlite3.Error:
            print('usage table unavailable')

        print('\nSAFETY')
        print('DEMO hard lock remains authoritative. Hunter cannot bypass session/spread/Spartan/adaptive/Bayesian/Edge Recovery/risk sizing.')
        print('Hunter risk multiplier never increases risk above 1.0x and playbook memory is learned from actual closed DEMO trades only.')
    finally:
        con.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
