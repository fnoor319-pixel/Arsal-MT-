from pathlib import Path
import os
import MetaTrader5 as mt5

PROJECT_DIR = Path(__file__).resolve().parent

# Load secrets/config from a local .env file when present.  The API key is never
# stored in source code or committed into the packaged defaults.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env", override=False)
except ImportError:
    pass

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
GPT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
OPENAI_MODEL = GPT_MODEL

DATABASE_PATH = PROJECT_DIR / "trading_machine.db"
REPORTS_DIR = PROJECT_DIR / "reports"
DATA_DIR = PROJECT_DIR / "data"
LOGS_DIR = PROJECT_DIR / "logs"

SYMBOLS = ["XAUUSDm", "USOILm", "BTCUSDm"]

# M1 research. MT5 only returns history available in the terminal/broker.
TIMEFRAME = mt5.TIMEFRAME_M1
TIMEFRAME_NAME = "M1"
HISTORY_BARS = 2_700_000
LIVE_BARS = 1_500
MAX_HISTORY_DOWNLOAD_BARS = 2_700_000
HISTORY_CHUNK_BARS = 5_000
HISTORY_FETCH_RETRIES = 4
HISTORY_RETRY_SECONDS = 1.0
HISTORY_RECENT_REFRESH_BARS = 20_000
HISTORY_FULL_REFRESH_HOURS = 24

# Backtest reference balance.
STARTING_BALANCE = 300.0

# Hard safety limits. Learning code cannot modify these values.
# Canonical Spartan checklist values.  RISK_PERCENT is a HARD MAX in percent,
# while the existing live target below remains the safer 0.15% per trade.
RISK_PERCENT = 0.5
SPARTAN_MAX_RISK_PERCENT = 0.5
ATR_PERIOD = 14
CONFIDENCE_THRESHOLD = 0.70
MAX_DAILY_DRAWDOWN = 5.0  # percent reference ceiling; live daily stop below is stricter (2%)

# User-requested fixed Pakistan-time trading windows.  These are authoritative
# for Spartan-Pro session gating; UTC timestamps are still used for storage/logs.
SESSION_TIMEZONE = "Asia/Karachi"
LONDON_START = "15:00"
LONDON_END = "23:30"
NY_START = "20:00"
NY_END = "03:00"

RISK_PER_TRADE = 0.0015       # 0.15% equity per trade
MAX_DAILY_LOSS_PCT = 0.02     # stop new DEMO entries at 2% daily equity loss
MAX_DRAWDOWN_PCT = 0.05       # stop new DEMO entries at 5% peak-to-equity drawdown
MAX_OPEN_POSITIONS = 4
MAX_ONE_POSITION_PER_SYMBOL = False  # legacy compatibility
MAX_POSITIONS_PER_SYMBOL = 1
MAX_SPREAD_ATR_FRACTION = 0.18
COOLDOWN_SECONDS = 15

# Historical validation. A strategy must pass full sample, final OOS and
# anchored walk-forward windows before it can enter the live shadow queue.
MIN_BACKTEST_TRADES = 30
MIN_PROFIT_FACTOR = 1.15
MIN_OOS_TRADES = 8
MIN_OOS_PROFIT_FACTOR = 1.05
MAX_VALIDATED_DRAWDOWN_PCT = 0.08
OOS_FRACTION = 0.30
WALK_FORWARD_FOLDS = 4
WALK_FORWARD_MIN_TRADES_PER_FOLD = 3
WALK_FORWARD_MIN_PROFIT_FACTOR = 1.00
WALK_FORWARD_MIN_PASS_RATIO = 0.75

# Ensemble selection. Fully approved strategies always have first priority.
ENSEMBLE_CANDIDATES = 30
ENSEMBLE_MIN_AGREE = 2
ENSEMBLE_CONSENSUS_RATIO = 1.25
EXECUTION_STRATEGY_STATUS = "shadow_approved"

# Merged V5.1 -> V5.2.5 DEMO trial bridge. The uploaded V5.1 database
# contains 355 strategies that passed its historical/OOS validation and were
# queued as needs_revalidation when anchored walk-forward was introduced.
# While the strict five-stage pipeline revalidates them, DEMO-only execution
# may use the strongest legacy/current historical passers as tiny-risk live
# trials when no shadow-approved ensemble exists. Real/contest accounts remain
# hard-blocked and rejected/generated strategies are never broker-executable.
ENABLE_DEMO_TRIAL_BRIDGE = True   # DEMO-only fallback: historical-validated strategies may trade at tiny risk when no shadow-approved set exists
DEMO_TRIAL_CANDIDATES = 36
DEMO_TRIAL_MIN_FULL_PROFIT_FACTOR = 1.10
DEMO_TRIAL_MIN_OOS_PROFIT_FACTOR = 1.05
DEMO_TRIAL_MIN_OOS_TRADES = 12
DEMO_TRIAL_MIN_STABILITY_SCORE = 0.0
DEMO_TRIAL_MIN_BACKTEST_SCORE = 0.0
DEMO_TRIAL_RISK_PER_TRADE = 0.0005       # 0.05% target risk
DEMO_TRIAL_MAX_MIN_LOT_RISK_PCT = 0.0035 # 0.35% hard ceiling
DEMO_TRIAL_MAX_OPEN_POSITIONS = 3
DEMO_TRIAL_MAX_POSITIONS_PER_SYMBOL = 1

# DEMO execution. verify_demo_account() hard-blocks real/contest accounts.
ENABLE_DEMO_ORDER_EXECUTION = True
DEMO_ONLY_HARD_LOCK = True
MAGIC_NUMBER = 260806
ORDER_COMMENT = "TradingMachineParallelDemo"
DEFAULT_DEVIATION_POINTS = 30

# Continuous research factory defaults. The generator is queue-throttled so
# it cannot create hypotheses faster than the full-history backtester can
# consume them. This prevents the 10,000+ untested-strategy backlog seen in
# the split-window build. Counts are per symbol and per batch.
INITIAL_RANDOM_STRATEGIES_PER_SYMBOL = 24
LIVE_GUIDED_STRATEGIES_PER_SYMBOL = 24
BACKTEST_BATCH_PER_SYMBOL = 240
EVOLVE_PARENTS_PER_SYMBOL = 18
CHILDREN_PER_PARENT = 3
FACTORY_SLEEP_SECONDS = 20
AUTO_REPORT_EVERY_MINUTES = 15
MAX_PENDING_STRATEGIES_TOTAL = 1200

# Demo sizing bridge. Normal target remains RISK_PER_TRADE.
ALLOW_DEMO_MINIMUM_LOT = True
MAX_DEMO_MIN_LOT_RISK_PCT = 0.0050   # 0.50% hard ceiling for broker min lot

# Adaptive execution learning V5.4.  These controls sit on top of the existing
# strategy/shadow pipeline; they do not replace the five-window architecture.
# Learning is persistent in SQLite and is keyed by strategy + symbol + regime +
# direction so one bad setup is penalized without globally disabling a strategy.
ENABLE_ADAPTIVE_EXECUTION_LEARNING = True
LEARNING_EWMA_ALPHA = 0.35
LEARNING_MEMORY_HALF_LIFE_HOURS = 720.0  # V7: remember evidence for ~30 days
LEARNING_STREAK_RESET_HOURS = 72.0
LEARNING_LOSS_COOLDOWN_BASE_SECONDS = 900
LEARNING_LOSS_COOLDOWN_MAX_SECONDS = 21600
LEARNING_RECOVERY_CONFIRM_AFTER_LOSSES = 2
LEARNING_RECOVERY_CONSENSUS_RATIO = 1.60
LEARNING_HARD_BLOCK_MIN_OBSERVATIONS = 4
LEARNING_HARD_BLOCK_CONFIDENCE = 0.38
LEARNING_CAUTION_CONFIDENCE = 0.50
LEARNING_DUPLICATE_SETUP_BLOCK = True

# Confidence-based position sizing.  Risk can increase only after enough live
# observations and positive adaptive evidence.  It is still clamped by the
# existing per-trade DEMO ceilings, daily-loss guard and drawdown guard.
LEARNING_RISK_UPSCALE_MIN_OBSERVATIONS = 30
LEARNING_RISK_UPSCALE_MIN_CONFIDENCE = 0.68
LEARNING_RISK_MIN_MULTIPLIER = 0.50
LEARNING_RISK_MAX_APPROVED_MULTIPLIER = 1.25
LEARNING_RISK_MAX_TRIAL_MULTIPLIER = 1.00
LEARNING_CANDIDATE_SCORE_WEIGHT = 10.0
LEARNING_CONSECUTIVE_LOSS_SCORE_PENALTY = 2.50

# V7 hierarchical live-learning kill switches. These use actual closed DEMO
# outcomes, pooled by strategy/family/direction, so sparse strategy IDs share
# useful experience instead of repeatedly relearning the same losing pattern.
V7_STRATEGY_BLOCK_MIN_TRADES = 8
V7_STRATEGY_BLOCK_MAX_MEAN_R = -0.12
V7_STRATEGY_BLOCK_MAX_PROFIT_FACTOR = 0.82
V7_REGIME_FAMILY_SIDE_BLOCK_MIN_TRADES = 10
V7_REGIME_FAMILY_SIDE_BLOCK_MAX_MEAN_R = -0.10
V7_REGIME_FAMILY_SIDE_BLOCK_MAX_PROFIT_FACTOR = 0.85
V7_FAMILY_SIDE_BLOCK_MIN_TRADES = 20
V7_FAMILY_SIDE_BLOCK_MAX_MEAN_R = -0.08
V7_FAMILY_SIDE_BLOCK_MAX_PROFIT_FACTOR = 0.90
V7_PERFORMANCE_WINDOW = 80
V7_MIN_EXPECTED_R = 0.08
V7_PROBABILITY_EDGE_MARGIN = 0.06

# Live shadow laboratory. Historically-passed strategies must prove themselves
# on current bid/ask data before execution. Rejected strategies may be observed
# for learning, but they can never be promoted directly to executable status.
SHADOW_CANDIDATES_PER_SYMBOL = 40
SHADOW_MAX_OPEN_PER_SYMBOL = 10
SHADOW_CYCLE_SECONDS = 10
SHADOW_MIN_TRAIN_PROFIT_FACTOR = 1.05
SHADOW_MIN_OOS_PROFIT_FACTOR = 1.00
SHADOW_MIN_OOS_TRADES = 12
SHADOW_MIN_BACKTEST_SCORE = 0.0
SHADOW_APPROVAL_TRADES = 40
SHADOW_APPROVAL_MEAN_R = 0.10
SHADOW_APPROVAL_PROFIT_FACTOR = 1.25
SHADOW_APPROVAL_MAX_DRAWDOWN_R = 4.0
SHADOW_REJECT_AFTER_TRADES = 15
SHADOW_REJECT_MEAN_R = -0.08

# Supervisor/restart behavior.
WORKER_RESTART_SECONDS = 10
SUPERVISOR_STATUS_SECONDS = 30

# ============================================================
# V5.5 SUPERLEARNER - proactive DEMO-only approval layer
# ============================================================
# No setting below can disable DEMO_ONLY_HARD_LOCK or exceed the existing
# per-trade/daily/drawdown hard ceilings above. This layer filters/adjusts only.
ENABLE_SUPERLEARNER = True
ENABLE_SUPERLEARNER_ONLINE_MODEL = True
SUPERLEARNER_MIN_ONLINE_UPDATES = 80
SUPERLEARNER_LEARNING_RATE = 0.05
SUPERLEARNER_L2 = 0.001
SUPERLEARNER_DRIFT_FORGET_RATE = 0.025
SUPERLEARNER_BASE_APPROVAL_PROBABILITY = 0.58
SUPERLEARNER_HARD_BAYES_VETO = 0.62
SUPERLEARNER_ROLLING_WINDOW = 20
SUPERLEARNER_HIGH_SPREAD_ATR = 0.12
SUPERLEARNER_HIGH_ENTROPY = 0.88
SUPERLEARNER_HIGH_DRIFT = 0.70
SUPERLEARNER_DOM_VETO_SCORE = 0.45

# Dynamic ensemble / shared experience bank. Shadow outcomes are discounted so
# simulated evidence helps selection but never counts the same as DEMO fills.
DYNAMIC_ENSEMBLE_MIN_MULTIPLIER = 0.40
DYNAMIC_ENSEMBLE_MAX_MULTIPLIER = 1.80
COLLECTIVE_EWMA_ALPHA = 0.15
COLLECTIVE_SHADOW_WEIGHT = 0.35
COLLECTIVE_EXPERIENCE_TARGET = 10_000

# Bayesian loss probability and dynamic temporary quarantine.
BAYESIAN_PRIOR_LOSS = 1.0
BAYESIAN_PRIOR_WIN = 1.0
BAYESIAN_RECENT_WEIGHT = 1.0
BAYESIAN_COOLDOWN_MIN_OBSERVATIONS = 4
BAYESIAN_COOLDOWN_START_PROB = 0.55
BAYESIAN_HEAVY_QUARANTINE_PROB = 0.70
BAYESIAN_HEAVY_QUARANTINE_SECONDS = 900

# Market randomness / microstructure / concept-drift controls.
DOM_HISTORY_SNAPSHOTS = 30
XAI_RECENT_LOSS_WINDOW = 8
XAI_COMMON_LOSS_FREQUENCY = 0.60

# Adaptive exits are bounded and position sizing is recalculated from the new
# stop distance, keeping cash-risk inside the existing hard DEMO ceilings.
ADAPTIVE_SL_MIN_MULTIPLIER = 0.80
ADAPTIVE_SL_MAX_MULTIPLIER = 1.25
ADAPTIVE_TP_MIN_MULTIPLIER = 0.80
ADAPTIVE_TP_MAX_MULTIPLIER = 1.40
SUPERLEARNER_SHADOW_MODEL_WEIGHT = 0.00  # V7: execution classifier learns from broker-demo outcomes only
MFE_MAE_ROLLING_WINDOW = 30

# Portfolio-level soft/hard loss-state protection. The hard daily/drawdown limits
# above still remain the final ceilings; this adds earlier de-risking/freeze.
PORTFOLIO_FREEZE_AFTER_LOSSES = 5
PORTFOLIO_FREEZE_SECONDS = 900
PORTFOLIO_HARD_FREEZE_ENABLED = True

# ============================================================
# V6 SCALP INTELLIGENCE - DEMO-only primary objective
# ============================================================
SCALP_PRIMARY_MODE = True
SCALP_EXECUTION_PRIORITY = True
SCALP_GENERATION_SHARE = 0.82
SCALP_ENSEMBLE_TARGET_SHARE = 0.75
SCALP_FAMILY_SCORE_BONUS = 4.0
SCALP_SESSION_SCORE_WEIGHT = 5.0
SCALP_COLLECTIVE_SCORE_WEIGHT = 3.0

# Two-stage backtesting: obvious junk is rejected on a recent sample before
# the expensive full-history/OOS/walk-forward pass.
SCALP_FAST_PRESCREEN_BARS = 40_000
SCALP_FAST_PRESCREEN_MIN_TRADES = 12
SCALP_FAST_PRESCREEN_MIN_PROFIT_FACTOR = 0.95
SCALP_FAST_PRESCREEN_MIN_EXPECTANCY_R = 0.00
BACKTEST_ABORT_DRAWDOWN_PCT = 0.12

# Scalping-specific quality gates. These are intentionally stricter about
# activity/holding time so a short-duration trend strategy cannot masquerade
# as a scalper.
SCALP_MIN_BACKTEST_TRADES = 60
SCALP_MIN_OOS_TRADES = 15
SCALP_MIN_FULL_PROFIT_FACTOR = 1.15
SCALP_MIN_OOS_PROFIT_FACTOR = 1.05
SCALP_MAX_AVG_HOLD_BARS = 15.0
SUPER_SCALP_MAX_AVG_HOLD_BARS = 7.0

# Session/context learning. Shadow outcomes train this at reduced weight,
# DEMO fills at full weight.
SESSION_MEMORY_EWMA_ALPHA = 0.18
SESSION_SHADOW_WEIGHT = 0.35
SESSION_MEMORY_MIN_OBSERVATIONS = 6.0
SESSION_BAD_WIN_PROBABILITY = 0.45
SESSION_GOOD_WIN_PROBABILITY = 0.58

# Loss streaks de-risk continuously instead of hard-stopping the whole
# research/execution engine. Broker orders can still be stopped by the hard
# daily/drawdown limits above; shadow/generator/backtester keep learning.
LOSS_STREAK_RISK_MULTIPLIER = 0.65



# ============================================================
# SPARTAN-SCALPER-PRO - additive XAUUSD / USOIL live gate
# ============================================================
# Existing research, shadow, SuperLearner and DEMO-only execution remain intact.
ENABLE_SPARTAN_PRO = True
SPARTAN_PRO_SYMBOL_KINDS = ("gold", "oil")

# Spartan fixed Pakistan-time session windows.  Gold: London + NY; Oil: NY only.
SPARTAN_SESSION_FILTER_ENABLED = True
SPARTAN_SESSION_TIMEZONE = SESSION_TIMEZONE
SPARTAN_GOLD_LONDON_START = LONDON_START
SPARTAN_GOLD_LONDON_END = LONDON_END
SPARTAN_US_START = NY_START
SPARTAN_US_END = NY_END
# Legacy UTC names remain only for backward compatibility with older reports.
SPARTAN_GOLD_LONDON_START_UTC = "10:00"
SPARTAN_GOLD_LONDON_END_UTC = "18:30"
SPARTAN_US_START_UTC = "15:00"
SPARTAN_US_END_UTC = "22:00"

# Hard market/data gates. Existing spread/portfolio guards can only be stricter.
SPARTAN_MIN_ADX = 20.0
SPARTAN_TREND_ADX = 25.0
SPARTAN_GOLD_MIN_ATR = 0.50
SPARTAN_OIL_MIN_ATR = 0.0  # calibrate from broker quote format/backtest before enforcing
SPARTAN_GOLD_MAX_ABS_SPREAD = 0.50
SPARTAN_MAX_TICK_AGE_SECONDS = 10.0
SPARTAN_MAX_ENTRY_DRIFT_ATR = 0.50
SPARTAN_ORDERBOOK_SHARE_THRESHOLD = 0.60  # == +/-20% signed imbalance
SPARTAN_REQUIRE_DOM = False               # enable only after broker DOM reliability is verified
SPARTAN_MIN_CONFLUENCE_SCORE = 6
SPARTAN_MIN_CONFIDENCE = CONFIDENCE_THRESHOLD
SPARTAN_MAX_TRADES_PER_DAY = 10

# SMC / volume profile / higher-timeframe context.
SPARTAN_SMC_LOOKBACK = 240
SPARTAN_VOLUME_PROFILE_LOOKBACK = 300
SPARTAN_HTF_BARS = 260
SPARTAN_TICK_FLOW_SECONDS = 60
SPARTAN_FOOTPRINT_BIN_POINTS = 5
SPARTAN_LONDON_KILL_START_UTC = "08:00"
SPARTAN_LONDON_KILL_END_UTC = "09:00"
SPARTAN_NY_KILL_START_UTC = "15:00"
SPARTAN_NY_KILL_END_UTC = "16:00"

# News adapter. The included file is provider-neutral and intentionally empty.
# Keep advisory until a reliable live provider writes fresh events to it.
SPARTAN_NEWS_FILE = PROJECT_DIR / "config" / "news_events.json"
SPARTAN_NEWS_HARD_GATE = False
SPARTAN_NEWS_PRE_LOCK_MINUTES = 30
SPARTAN_NEWS_POST_LOCK_MINUTES = 20
SPARTAN_NEWS_FEED_MAX_AGE_MINUTES = 180

# Optional OpenAI reviewer: veto/confirmation only, never risk or broker controls.
SPARTAN_LLM_REVIEW_ENABLED = True
SPARTAN_LLM_MODEL = GPT_MODEL
SPARTAN_LLM_FAIL_CLOSED = True
SPARTAN_LLM_TIMEOUT_SECONDS = 12.0
SPARTAN_LLM_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "low").strip() or "low"

# V6.5 GPT decision-memory / token-protection layer. A material-state fingerprint
# prevents repeated API calls for the same candle/setup, while meaningful changes
# (price/ATR bucket, flow, SMC, HTF, ML probability, news/regime) allow a fresh review.
SPARTAN_GPT_DECISION_MEMORY_ENABLED = True
SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY = 200
SPARTAN_GPT_MEMORY_EWMA_ALPHA = 0.25
SPARTAN_GPT_VETO_SHADOW_LEARNING_ENABLED = True
SPARTAN_GPT_VETO_SHADOW_MODEL_WEIGHT = 0.25
SPARTAN_GPT_VETO_SHADOW_COLLECTIVE_WEIGHT = 0.20

# GPT Level-3 post-trade analyst. This is deliberately advisory only: it may
# write a post-mortem/research hypothesis, but no live strategy/risk parameter
# reads these suggestions automatically. A failure here never blocks execution.
SPARTAN_POST_TRADE_REVIEW_ENABLED = True
SPARTAN_POST_TRADE_MODEL = GPT_MODEL
SPARTAN_POST_TRADE_TIMEOUT_SECONDS = 15.0
SPARTAN_POST_TRADE_REASONING_EFFORT = os.environ.get("OPENAI_POST_TRADE_REASONING_EFFORT", "low").strip() or "low"
SPARTAN_POST_TRADE_MAX_HYPOTHESES = 5

# Preserve backtest/live parity by default. Existing strategy ATR exits remain active.
SPARTAN_FORCE_FIXED_ATR_EXITS = False
SPARTAN_FIXED_SL_ATR = 1.0
SPARTAN_FIXED_TP_ATR = 1.5

# DEMO-only position management. Uses original stored risk distance.
SPARTAN_TRAILING_ENABLED = True
SPARTAN_BREAK_EVEN_AT_R = 0.75
SPARTAN_BREAK_EVEN_LOCK_R = 0.05
SPARTAN_TRAILING_START_R = 1.00
SPARTAN_TRAILING_DISTANCE_R = 0.70

# Optional Telegram notifications. Credentials come only from environment variables.
SPARTAN_TELEGRAM_ENABLED = False

# ============================================================
# V6.6 SMART SCALP BRAIN - DEMO-only efficiency + calibration
# ============================================================
# Entry logic is based on the last CLOSED M1 candle. Re-evaluating the same
# candle every few seconds created hundreds of thousands of duplicate logs and
# did not add a new strategy signal. Position management still runs every cycle.
ENTRY_EVALUATE_NEW_CLOSED_BAR_ONLY = True
DECISION_LOG_DEDUP_SECONDS = 55

# SuperLearner scalp calibration. Scalp strategies can be profitable with a
# lower win-rate when their realised reward:risk is high, so the primary gate is
# positive expected-R above breakeven rather than a fixed 0.68 win probability.
SUPERLEARNER_SCALP_EXPECTANCY_MODE = True
SUPERLEARNER_SCALP_MODEL_WEIGHT = 0.55
SUPERLEARNER_SCALP_ADAPTIVE_WEIGHT = 0.20
SUPERLEARNER_SCALP_COLLECTIVE_WEIGHT = 0.15
SUPERLEARNER_SCALP_SESSION_WEIGHT = 0.10
SUPERLEARNER_SCALP_MIN_PROBABILITY = 0.38
SUPERLEARNER_SCALP_MAX_PROBABILITY_THRESHOLD = 0.62
SUPERLEARNER_SCALP_EDGE_MARGIN = 0.05
SUPERLEARNER_SCALP_MIN_EXPECTED_R = 0.10

# Shadow research should not poison broker-execution memory. Rejected strategies
# remain useful for research/search, but their virtual outcomes no longer count
# as execution-family/session evidence. Stronger statuses keep reduced weight.
SHADOW_MEMORY_WEIGHT_BACKTEST_REJECTED = 0.0
SHADOW_MEMORY_WEIGHT_SHADOW_REJECTED = 0.0
SHADOW_MEMORY_WEIGHT_HISTORICAL_VALIDATED = 0.18
SHADOW_MEMORY_WEIGHT_SHADOW_APPROVED = 0.35

# Optional evidence (DOM/news feed) is neutral when unavailable. Hard safety
# checks still apply independently (session, spread, ADX, ATR, news lock, etc.).
SPARTAN_MIN_AVAILABLE_CONFLUENCE_RATIO = 0.72
SPARTAN_MIN_CORE_ALIGNMENT = 4

# Keep each GPT review compact: one small market packet in, one short structured
# decision out. This is a token/latency control, not a relaxation of hard gates.
SPARTAN_LLM_MAX_OUTPUT_TOKENS = 120
SPARTAN_LLM_MAX_REASON_CHARS = 140
SPARTAN_LLM_COMPACT_PACKET = True
SPARTAN_POST_TRADE_COMPACT_PACKET = True
SPARTAN_POST_TRADE_MAX_OUTPUT_TOKENS = 180

# One-time repair of collective/session execution memory after V6.5 research
# shadow outcomes were allowed to enter the same memory as broker DEMO trades.
V66_REBUILD_EXECUTION_MEMORY_ONCE = True

# ============================================================
# V6.7 SCALP LAB + $5 LUNA BUDGET GUARD (DEMO ONLY)
# ============================================================
# Luna remains the final context reviewer, never the lot/risk/broker authority.
# The bot tracks its own token usage and conservatively stops paid calls before
# consuming the user's full $5 API credit. Pricing constants match GPT-5.6 Luna
# at package build time and can be edited later without touching code.
SPARTAN_GPT_INPUT_USD_PER_MILLION = 0.20
SPARTAN_GPT_OUTPUT_USD_PER_MILLION = 1.20
SPARTAN_GPT_BOT_BUDGET_USD = 4.00  # leave ~US$1 account reserve for tests/other calls
SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY = 80
SPARTAN_GPT_MAX_POSTTRADE_API_CALLS_PER_DAY = 8
SPARTAN_GPT_PRETRADE_REASONING_EFFORT = os.environ.get("OPENAI_PRETRADE_REASONING_EFFORT", "none").strip() or "none"
SPARTAN_LLM_MAX_OUTPUT_TOKENS = 80
SPARTAN_LLM_MAX_REASON_CHARS = 80
SPARTAN_GPT_PROMPT_CACHE_KEY = "spartan-scalp-pretrade-v68"
SPARTAN_POST_TRADE_REASONING_EFFORT = os.environ.get("OPENAI_POST_TRADE_REASONING_EFFORT", "none").strip() or "none"
SPARTAN_POST_TRADE_MAX_OUTPUT_TOKENS = 110
SPARTAN_POST_TRADE_MAX_HYPOTHESES = 1

# Narrow DEMO-only second-opinion lane for soft ML dead-zones. Hard Spartan,
# spread, account, adaptive-learning and Bayesian hard vetoes remain absolute.
SPARTAN_GPT_BORDERLINE_REVIEW_ENABLED = True
SPARTAN_GPT_BORDERLINE_MIN_PROBABILITY = 0.30
SPARTAN_GPT_BORDERLINE_MAX_GAP = 0.08
SPARTAN_GPT_BORDERLINE_BREAK_EVEN_MARGIN = 0.05
SPARTAN_GPT_BORDERLINE_MIN_EXPECTED_R = 0.15
SPARTAN_GPT_BORDERLINE_MIN_CONFIDENCE = 0.82
SPARTAN_GPT_BORDERLINE_MAX_TRADES_PER_DAY = 2
SPARTAN_GPT_BORDERLINE_RISK_MULTIPLIER = 0.30

# 60-second in-memory funnel telemetry; DB remains the authoritative history.
SCALP_FLOW_SUMMARY_SECONDS = 60

# V6.7 laboratory evidence. Cost stress is advisory by default because broker
# commission/quote conventions differ; it reports robustness without silently
# changing historical qualification until calibrated against real DEMO fills.
LAB_COST_STRESS_EXTRA_SPREAD_MULTIPLIER = 0.35
LAB_COST_STRESS_SLIPPAGE_SPREAD_MULTIPLIER = 0.20
LAB_COST_STRESS_GATE_ENABLED = False
LAB_MONTE_CARLO_SAMPLES = 3000
LAB_MONTE_CARLO_WINDOW = 120
LAB_LUNA_MIN_VALUE_SAMPLES = 40
LAB_EXECUTION_QUALITY_ENABLED = True
LAB_TICK_REPLAY_MAX_TRADES = 20
LAB_CORRELATION_RISK_ENABLED = True
LAB_CORRELATION_BARS = 240
LAB_CORRELATION_EXPOSURE_THRESHOLD = 0.72
LAB_EXECUTION_MIN_SAMPLES_FOR_RISK = 10

# V6.9 EDGE RECOVERY / SELF-CORRECTION (DEMO ONLY)
# The purpose is to improve *selection quality*, not to chase trade count.
# Weak recent broker-demo evidence can de-risk or quarantine a strategy before
# Luna is called, while strong shadow evidence can fast-track only exceptionally
# robust scalp candidates into the existing shadow-approved tier.
EDGE_RECOVERY_ENABLED = True
EDGE_STRATEGY_WINDOW = 30
EDGE_FAMILY_WINDOW = 60
EDGE_SYMBOL_WINDOW = 60
EDGE_OVERALL_WINDOW = 120
EDGE_MIN_STRATEGY_SAMPLES = 6
EDGE_HARD_QUARANTINE_SAMPLES = 8
EDGE_HARD_QUARANTINE_MEAN_R = -0.15
EDGE_HARD_QUARANTINE_PF = 0.85
EDGE_FAMILY_CAUTION_SAMPLES = 12
EDGE_FAMILY_CAUTION_MEAN_R = -0.05
EDGE_SYMBOL_MC_MIN_SAMPLES = 20
EDGE_SYMBOL_MC_CAUTION_PROB_NEG = 0.60
EDGE_SYMBOL_MC_POOR_PROB_NEG = 0.75
EDGE_OVERALL_CAUTION_MIN_SAMPLES = 40
EDGE_RECOVERY_FAMILY_RISK_MULTIPLIER = 0.60
EDGE_RECOVERY_SYMBOL_CAUTION_RISK_MULTIPLIER = 0.65
EDGE_RECOVERY_SYMBOL_POOR_RISK_MULTIPLIER = 0.40
EDGE_RECOVERY_OVERALL_RISK_MULTIPLIER = 0.75
EDGE_RECOVERY_STRATEGY_CAUTION_RISK_MULTIPLIER = 0.50

# Shadow evidence receives far more weight in execution ranking than it did in
# V6.8. Catastrophically weak strategies can be skipped early instead of waiting
# for a large historical score to dominate the ranking.
SCALP_SHADOW_SCORE_WEIGHT = 30.0
SCALP_SHADOW_EARLY_QUARANTINE_TRADES = 8
SCALP_SHADOW_EARLY_QUARANTINE_MEAN_R = -0.25

# Fast-track is deliberately stricter than normal shadow approval and remains
# DEMO-only. It is meant to grow the approved scalp pool without weakening the
# evidence standard.
SCALP_FAST_TRACK_SHADOW_ENABLED = True
SCALP_FAST_TRACK_TRADES = 20
SCALP_FAST_TRACK_MEAN_R = 0.18
SCALP_FAST_TRACK_PROFIT_FACTOR = 1.40
SCALP_FAST_TRACK_MAX_DRAWDOWN_R = 2.50
SCALP_FAST_TRACK_MIN_OOS_PF = 1.10
SCALP_FAST_TRACK_MIN_STABILITY = 0.70
# Force the trading brain to Luna unless a dedicated OPENAI_SCALP_MODEL override
# is intentionally supplied. This prevents an old OPENAI_MODEL=terra/sol value
# from silently burning the small trading credit balance.
SCALP_AI_MODEL = os.environ.get("OPENAI_SCALP_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
SPARTAN_LLM_MODEL = SCALP_AI_MODEL
SPARTAN_POST_TRADE_MODEL = SCALP_AI_MODEL
SPARTAN_POST_TRADE_PROMPT_CACHE_KEY = "spartan-scalp-posttrade-v67"

# ============================================================
# V6.8 CAPITAL GROWTH CONTROLLER (DEMO ONLY)
# ============================================================
# 15% / 20% are tracked as DAILY OBJECTIVES, not guaranteed returns. The
# controller never increases risk to chase a target. It only protects gains
# after strong growth while normal sizing continues to compound from equity.
ENABLE_CAPITAL_GROWTH_CONTROLLER = True
CAPITAL_GROWTH_TARGET_DAILY_PCT = 0.15
CAPITAL_GROWTH_STRETCH_DAILY_PCT = 0.20
CAPITAL_GROWTH_AFTER_TARGET_RISK_MULTIPLIER = 0.50
CAPITAL_GROWTH_BLOCK_NEW_ENTRIES_AFTER_STRETCH = True
CAPITAL_GROWTH_PEAK_PROTECT_TRIGGER_PCT = 0.08
CAPITAL_GROWTH_MAX_GIVEBACK_FROM_PEAK_PCT = 0.03
CAPITAL_GROWTH_GIVEBACK_RISK_MULTIPLIER = 0.35
CAPITAL_GROWTH_NEVER_MARTINGALE = True
CAPITAL_GROWTH_NEVER_RISK_UP_TO_CHASE_TARGET = True


# ============================================================
# V8.3 SPREAD-AWARE TRIGGER PRESERVATION
# ============================================================
# A high-quality micro trigger is not discarded forever because spread is
# temporarily wide on the exact trigger tick.  The setup enters WAIT_SPREAD and
# may resume only if spread normalizes before the short TTL, price has not
# invalidated, and price has not run too far from the original trigger.
V8_SPREAD_WAIT_ENABLED = True
V8_SPREAD_WAIT_TTL_SECONDS = 35
V8_SPREAD_WAIT_MAX_DRIFT_ATR = 0.18
V8_SPREAD_WAIT_REQUIRE_SAME_BAR = True

# ============================================================
# V7.0 MICRO-SCALP HUNTER - DEMO-ONLY FAST EXECUTION BRAIN
# ============================================================
# The research factory remains slow/deep. This separate execution brain hunts
# 2-5 candle patterns and may ARM a setup, then watch live ticks for a trigger.
# It can execute only through an already qualified shadow-approved/DEMO-trial
# carrier strategy; generated/rejected strategies never become broker-executable.
V7_MICRO_HUNTER_ENABLED = True
V7_MICRO_HUNTER_MAX_STATES_PER_BAR = 3
V7_MICRO_HUNTER_ARM_TTL_SECONDS = 150
V7_MICRO_HUNTER_TRIGGER_BUFFER_ATR = 0.02
V7_MICRO_PLAYBOOK_EWMA_ALPHA = 0.22

# Symbol-specific opportunity thresholds. Oil is deliberately stricter because
# recent DEMO evidence and spread behaviour have been weaker than XAU/BTC.
V7_MICRO_HUNTER_XAU_WATCH_SCORE = 84.0
V7_MICRO_HUNTER_XAU_ARM_SCORE = 88.0
V7_MICRO_HUNTER_XAU_TRIGGER_SCORE = 92.0
V7_MICRO_HUNTER_OIL_WATCH_SCORE = 86.0
V7_MICRO_HUNTER_OIL_ARM_SCORE = 90.0
V7_MICRO_HUNTER_OIL_TRIGGER_SCORE = 94.0
V7_MICRO_HUNTER_BTC_WATCH_SCORE = 84.0
V7_MICRO_HUNTER_BTC_ARM_SCORE = 88.0
V7_MICRO_HUNTER_BTC_TRIGGER_SCORE = 92.0
V7_MICRO_HUNTER_WATCH_SCORE = 84.0
V7_MICRO_HUNTER_ARM_SCORE = 88.0
V7_MICRO_HUNTER_TRIGGER_SCORE = 92.0

# Luna remains a low-cost final second brain. The $4 local spend guard still
# dominates; these call caps only reduce unnecessary request frequency further.
SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY = 40
SPARTAN_GPT_MAX_POSTTRADE_API_CALLS_PER_DAY = 4
SPARTAN_GPT_PROMPT_CACHE_KEY = "spartan-micro-scalp-pretrade-v70"
SPARTAN_POST_TRADE_PROMPT_CACHE_KEY = "spartan-micro-scalp-posttrade-v70"
V7_MICRO_PLAYBOOK_SUPER_WEIGHT = 0.15
V7_MICRO_PLAYBOOK_MIN_BLEND_OBS = 5

# ============================================================
# V8.0 INSTITUTIONAL MICRO-SCALP ENGINE - DEMO ONLY
# ============================================================
# Core change: live alpha is opportunity-first and independent of having a
# matching approved/trial scalp strategy. A native DEMO-only carrier may let a
# strong specialist playbook reach the existing hard gates, SuperLearner, Edge
# Recovery, Luna and broker risk stack. Real/contest accounts remain hard-blocked.
V8_NATIVE_ALPHA_ENABLED = True
V8_NATIVE_ALPHA_RISK_MULTIPLIER = 0.30
V8_NATIVE_ALPHA_MAX_TRADES_PER_DAY = 6
V8_NATIVE_ALPHA_MAX_TRADES_PER_SYMBOL_PER_DAY = 3

# Adaptive symbol score distribution. Fixed V7 thresholds remain fallback only;
# V8 learns WATCH/ARM/TRIGGER cutoffs from each symbol's recent opportunity scores.
V8_ALPHA_SCORE_LOOKBACK = 360
V8_ALPHA_MIN_SCORE_SAMPLES = 45
V8_ALPHA_WATCH_QUANTILE = 0.70
V8_ALPHA_ARM_QUANTILE = 0.84
V8_ALPHA_TRIGGER_QUANTILE = 0.94

# Deterministic alpha score becomes one member of the SuperLearner ensemble.
V8_ALPHA_SCORE_SUPER_WEIGHT = 0.35
V8_ALPHA_SCORE_BLEND_MIN = 55.0
SUPERLEARNER_SCALP_MIN_PROBABILITY = 0.34
SUPERLEARNER_SCALP_MAX_PROBABILITY_THRESHOLD = 0.58
SUPERLEARNER_SCALP_EDGE_MARGIN = 0.035
SUPERLEARNER_SCALP_MIN_EXPECTED_R = 0.05

# Luna remains the final second brain, but only after a cheap local shortlist.
# This keeps a ~$5 funded API account from burning requests on restarts/no-trade noise.
V8_PRE_LUNA_LOCAL_SCORE = 56.0
V8_PRE_LUNA_LOCAL_DAILY_PASS_CAP = 16
SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY = 12
SPARTAN_GPT_MAX_POSTTRADE_API_CALLS_PER_DAY = 2
SPARTAN_LLM_MAX_OUTPUT_TOKENS = 56
SPARTAN_LLM_MAX_REASON_CHARS = 48
SPARTAN_GPT_PROMPT_CACHE_KEY = "institutional-micro-scalp-pretrade-v80"
SPARTAN_POST_TRADE_PROMPT_CACHE_KEY = "institutional-micro-scalp-posttrade-v80"
V8_NEGATIVE_EDGE_EXPLORATION_SCORE_BONUS = 4.0

# V8.1 direction-aware Luna efficiency
V8_DIRECTION_COHERENCE_GATE_ENABLED = True

# ============================================================
# V8.2 QUANT-LUNA ENSEMBLE / DEMO DISAGREEMENT LEARNING
# ============================================================
# Luna remains a second brain, but is not a single point of failure. When the
# deterministic stack + SuperLearner strongly approve and no local hard
# contradiction exists, a Luna HOLD may be sampled at tiny DEMO risk so the
# machine can learn whether Luna is saving losses or over-vetoing winners.
V8_LUNA_DISAGREEMENT_PROBE_ENABLED = True
V8_LUNA_DISAGREEMENT_RISK_MULTIPLIER = 0.10
V8_LUNA_DISAGREEMENT_MAX_PER_DAY = 2
V8_LUNA_DISAGREEMENT_MAX_PER_SYMBOL_PER_DAY = 1
V8_LUNA_DISAGREEMENT_MIN_LOCAL_SCORE = 82.0
V8_LUNA_DISAGREEMENT_MIN_MICRO_SCORE = 80.0
V8_LUNA_DISAGREEMENT_MIN_EXPECTED_R = 0.10

# Keep the ~$5 Luna budget conservative. V8.2 spends only on serious shortlisted
# candidates and uses tiny replies. The local $4 spend guard remains authoritative.
SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY = 8
SPARTAN_GPT_MAX_POSTTRADE_API_CALLS_PER_DAY = 2
SPARTAN_LLM_MAX_OUTPUT_TOKENS = 52
SPARTAN_LLM_MAX_REASON_CHARS = 44
SPARTAN_GPT_PROMPT_CACHE_KEY = "institutional-micro-scalp-pretrade-v82"

# ============================================================
# V8.4 EXECUTION-FIRST DEMO EVIDENCE MODE
# ============================================================
# Live Micro Hunter playbooks use their own persistent DEMO-only carrier so
# stale legacy trial/approved strategy memory cannot choke the alpha path.
V8_NATIVE_ALPHA_PRIMARY_FOR_HUNTER = True

# After repeated losses, a fresh high-score micro trigger can collect reduced-risk
# evidence instead of failing a legacy multi-vote rule that does not fit a
# single-playbook state machine.
V8_MICRO_RECOVERY_SCORE_BONUS = 6.0
V8_MICRO_RECOVERY_RISK_MULTIPLIER = 0.35

# If Luna says HOLD but the already-hard-gated quant setup is still above
# break-even with positive Expected-R, allow a tiny capped DEMO evidence probe.
# This is DEMO-only and does not bypass direction/spread/risk/broker hard locks.
V8_EVIDENCE_PROBE_MIN_EXPECTED_R = 0.20
V8_EVIDENCE_PROBE_MIN_PROB_EDGE = 0.03
V8_LUNA_DISAGREEMENT_MAX_PER_DAY = 3
V8_LUNA_DISAGREEMENT_MAX_PER_SYMBOL_PER_DAY = 1

# Keep Luna useful but cheap. Local shortlist + cache still run first.
SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY = 6
SPARTAN_GPT_MAX_POSTTRADE_API_CALLS_PER_DAY = 1
SPARTAN_LLM_MAX_OUTPUT_TOKENS = 44
SPARTAN_LLM_MAX_REASON_CHARS = 40
SPARTAN_GPT_PROMPT_CACHE_KEY = "institutional-micro-scalp-pretrade-v84"


# ============================================================
# V8.5 EXECUTION-CONVERSION / PLAYBOOK-AWARE HARD GATES
# ============================================================
# The live Micro Hunter is now active enough that the remaining failure mode is
# conversion: continuation/reversal/squeeze playbooks were being judged by one
# generic Gold/Oil ADX/confluence profile, and a spent Luna-call cap could become
# a complete execution dead-end. V8.5 keeps the truly hard safety gates but
# makes the *micro* Spartan profile playbook-aware and permits tiny local DEMO
# evidence probes when the paid Luna budget/call-cap is exhausted.
V8_PLAYBOOK_AWARE_SPARTAN_ENABLED = True
V8_MICRO_SPARTAN_MIN_SCORE_FOR_PROFILE = 68.0

# Continuation micro-scalps: still require trend quality, but the micro trigger
# itself supplies additional evidence. ADX 16 is a floor, not an invitation to
# trade flat noise; direction/confluence/Expected-R remain downstream.
V8_MICRO_SPARTAN_CONT_ADX_MIN = 16.0
V8_MICRO_SPARTAN_CONT_MIN_CONFLUENCE_RATIO = 0.60
V8_MICRO_SPARTAN_CONT_MIN_CORE_ALIGNMENT = 3
V8_MICRO_SPARTAN_CONT_MIN_CONFIDENCE = 0.60

# Reversal playbooks should not be killed merely because ADX is below 20.
V8_MICRO_SPARTAN_REV_REQUIRE_ADX = False
V8_MICRO_SPARTAN_REV_MIN_CONFLUENCE_RATIO = 0.55
V8_MICRO_SPARTAN_REV_MIN_CORE_ALIGNMENT = 2
V8_MICRO_SPARTAN_REV_MIN_CONFIDENCE = 0.56

# A squeeze setup can occur precisely while ADX is low before expansion.
V8_MICRO_SPARTAN_SQUEEZE_REQUIRE_ADX = False
V8_MICRO_SPARTAN_SQUEEZE_MIN_CONFLUENCE_RATIO = 0.58
V8_MICRO_SPARTAN_SQUEEZE_MIN_CORE_ALIGNMENT = 2
V8_MICRO_SPARTAN_SQUEEZE_MIN_CONFIDENCE = 0.58

# If Luna cannot be called because the daily call-cap or local dollar guard is
# exhausted, a very strong, above-break-even Quant setup may collect a tiny
# broker-DEMO evidence trade instead of failing closed forever.
V8_BUDGET_EXHAUSTED_QUANT_PROBE_ENABLED = True
V8_BUDGET_PROBE_RISK_MULTIPLIER = 0.07
V8_BUDGET_PROBE_MAX_PER_DAY = 3
V8_BUDGET_PROBE_MAX_PER_SYMBOL_PER_DAY = 1
V8_BUDGET_PROBE_MIN_LOCAL_SCORE = 82.0
V8_BUDGET_PROBE_MIN_MICRO_SCORE = 82.0
V8_BUDGET_PROBE_MIN_EXPECTED_R = 0.30
V8_BUDGET_PROBE_MIN_PROB_EDGE = 0.04

# Keep Luna bounded; compact structured packets make 24 serious reviews/day
# inexpensive, while the independent bot-dollar guard remains authoritative.
SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY = 24
SPARTAN_GPT_MAX_POSTTRADE_API_CALLS_PER_DAY = 1
SPARTAN_LLM_MAX_OUTPUT_TOKENS = 40
SPARTAN_LLM_MAX_REASON_CHARS = 36
SPARTAN_GPT_PROMPT_CACHE_KEY = "institutional-micro-scalp-pretrade-v85"

# V8.6: paid Luna quota cannot veto otherwise-qualified local Quant execution.
V8_QUOTA_DECOUPLED_EXECUTION = True

# ============================================================
# V9.0 EXECUTION-FIRST SCALPER — DEMO EVIDENCE ARCHITECTURE
# ============================================================
# Research/Spartan/SuperLearner/Luna remain advisory ensemble inputs for live
# Micro Hunter entries. Hard execution locks remain spread/cost, account risk,
# position/cooldown, fresh-price, sizing and MT5/broker validity.
V9_EXECUTION_FIRST_ENABLED = True
V9_MAX_TRADES_PER_DAY = 300
V9_MAX_TRADES_PER_SYMBOL_PER_DAY = 100
V9_BASE_RISK_PER_TRADE = 0.0010       # 0.10% equity target before ensemble multiplier
V9_MAX_MIN_LOT_RISK_PCT = 0.0035      # 0.35% absolute DEMO minimum-lot ceiling
V9_EXECUTE_SCORE_MIN = 62.0
V9_MIN_RISK_MULTIPLIER = 0.20
V9_HARD_MIN_EXPECTED_R = 0.10
V9_HARD_MIN_PROB_EDGE = 0.00
V9_LUNA_ADVISORY_ENABLED = True
V9_LUNA_ADVISORY_MIN_SCORE = 82.0
V9_LUNA_ADVISORY_MIN_EXPECTED_R = 0.10
SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY = 24
SPARTAN_GPT_MAX_POSTTRADE_API_CALLS_PER_DAY = 1
SPARTAN_GPT_PROMPT_CACHE_KEY = "v9-execution-first-scalper"
# Cost-aware spread gate: legacy 0.18 ATR remains the minimum ceiling; V9 may
# accept wider spread only when reward distance can absorb it, never >0.28 ATR.
V9_MAX_SPREAD_TO_TARGET_FRACTION = 0.16
V9_ABSOLUTE_MAX_SPREAD_ATR = 0.28
V9_MAX_FRESH_PRICE_DRIFT_ATR = 0.35
# ============================================================
# V9.1 HIGH-PRECISION LEARNING OPTIMIZER
# ============================================================
# 50/symbol/day is a safety ceiling, not a trade target. V9.1 never forces a
# trade merely to fill quota; only fresh Micro Hunter setups that survive the
# precision/expectancy/cost/risk path may execute.
V9_PRECISION_TARGET_WIN_RATE = 0.80       # calibration goal only; never a guarantee
V9_PRECISION_MEMORY_MIN_OBSERVATIONS = 4
V9_PRECISION_WEAK_WIN_RATE = 0.35
V9_PRECISION_STRONG_WIN_RATE = 0.58
V9_PRECISION_WEAK_EWMA_R = -0.08
V9_PRECISION_STRONG_EWMA_R = 0.10
V9_PRECISION_HARD_HOLD_MIN_OBSERVATIONS = 10
V9_PRECISION_HARD_HOLD_WIN_RATE = 0.25
V9_PRECISION_HARD_HOLD_EWMA_R = -0.20
V9_RECOVERY_MODE_ENABLED = True
V9_RECOVERY_MIN_MICRO_SCORE = 86.0
V9_RECOVERY_MIN_EXPECTED_R = 0.45
V9_RECOVERY_MIN_PROB_EDGE = 0.05
V9_RECOVERY_MAX_RISK_MULTIPLIER = 0.55
# Recovery mode is deliberately NOT martingale: after losses it never increases
# size above the normal risk grade. It seeks a fresh independent setup and caps
# risk until evidence improves.
V9_MARTINGALE_ENABLED = False

# Dynamic profit protection. Strong A/A+ runners keep room; weaker or recently
# poor playbooks protect open profit earlier to improve close quality.
V9_PRECISION_EXIT_ENABLED = True
V9_A_PLUS_BE_AT_R = 0.65
V9_A_BE_AT_R = 0.55
V9_B_BE_AT_R = 0.45
V9_WEAK_MEMORY_BE_AT_R = 0.35
V9_B_TRAIL_START_R = 0.80
V9_WEAK_MEMORY_TRAIL_START_R = 0.65
V9_B_TRAIL_DISTANCE_R = 0.55
V9_WEAK_MEMORY_TRAIL_DISTANCE_R = 0.45



# ============================================================
# V9.2 ADAPTIVE SCALPING INTELLIGENCE — PROFIT-QUALITY OVERLAY
# ============================================================
# 100/symbol/day is a safety capacity, never a forced quota. V9.2 keeps the
# working V9 execution-first path and adds bounded context learning around it.
V92_ADAPTIVE_SCALPING_ENABLED = True
V92_CONTEXT_EWMA_ALPHA = 0.25
V92_CONTEXT_FULL_WEIGHT_N = 20
V92_MIN_ADAPTIVE_EXECUTE_FLOOR = 59.0
V92_MAX_ADAPTIVE_EXECUTE_FLOOR = 69.0

# New playbook/context cells start tiny and earn normal allocation from real
# broker outcomes. This is exploration without martingale.
V92_NEW_PLAYBOOK_FIRST5_RISK_CAP = 0.25
V92_NEW_PLAYBOOK_6_10_RISK_CAP = 0.50
V92_NEW_PLAYBOOK_11_20_RISK_CAP = 0.75

# Dynamic ATR/MFE/MAE exit intelligence and real partial scale-out. If the
# broker minimum lot makes partial close impossible, the manager locks profit
# with a tighter stop rather than inventing extra exposure.
V92_DYNAMIC_EXIT_ENABLED = True
V92_PARTIAL_EXIT_ENABLED = True
V92_PARTIAL_MIN_PROFIT_R = 0.30
V92_ADVERSE_FLOW_CUT_ENABLED = True
V92_ADVERSE_FLOW_CUT_AT_R = -0.48
V92_ADVERSE_FLOW_COMBINED_THRESHOLD = -0.58
V92_ADVERSE_FLOW_MIN_SECONDS_OPEN = 25

# Champion/challenger: current plan trades; a bounded alternative is evaluated
# from broker MFE/MAE and can be promoted only after enough evidence.
V92_CHALLENGER_ENABLED = True
V92_CHALLENGER_MIN_SAMPLES = 12
V92_CHALLENGER_PROMOTION_MARGIN_R = 0.12
V92_CHALLENGER_TAKE_MULTIPLIER = 1.10
V92_CHALLENGER_STOP_MULTIPLIER = 0.96

# Rejected-trade counterfactual learning. Virtual trials place no broker order.
V92_REJECTED_TRADE_LEARNING_ENABLED = True
V92_REJECTED_TRIAL_TTL_MINUTES = 12
V92_REJECTED_ANALYSIS_WINDOW = 40

# Microstructure/MTF/volume-profile/session evidence remain SCORE/RISK inputs,
# not a return to the old serial-veto architecture.
V92_MICROSTRUCTURE_ENABLED = True
V92_MTF_ALIGNMENT_ENABLED = True
V92_VOLUME_PROFILE_ENABLED = True
V92_EXHAUSTION_ENABLED = True
V92_SESSION_ADAPTATION_ENABLED = True

# 80% is a dashboard aspiration only, not an execution promise.
V92_TARGET_WIN_RATE = 0.80

# V9.2.1 loss-freeze scope repair
V921_SYMBOL_LOCAL_LOSS_FREEZE = True
V921_OLD_LOSSES_LEARNING_ONLY_FOR_FREEZE = True

# ============================================================
# V10 SUPERHUMAN SCALP OS — FAST EXPERT ROUTER
# ============================================================
# The V10 fast brain broadens alpha coverage with eight intraday archetypes.
# It runs locally and does not wait for Luna. Luna remains a selective meta-brain
# for high-quality candidates through the existing V9 advisory path.
V10_SUPERHUMAN_SCALPER_ENABLED = True
V10_EXPERT_TRIGGER_MIN_SCORE = 72.0
V10_OPPOSITE_OVERRIDE_MARGIN = 4.0
V10_EXPERT_ARCHETYPES = (
    "trend_pullback",
    "momentum_breakout",
    "squeeze_release",
    "liquidity_sweep_reclaim",
    "failed_breakout_reversal",
    "vwap_mean_reversion",
    "range_edge_rotation",
    "exhaustion_snapback",
)
# Existing V9.2.1 capacity/safety remain authoritative:
# 100 trades/symbol/day safety ceiling, symbol-local 5-loss freeze,
# portfolio daily-loss/drawdown hard stops, no martingale.

# V11 canonical DEMO research/execution. Raw expert scores are not win rates.
# Existing hard loss limits, magic, symbols, account lock and learning stay intact.
V11_ENABLED = True
V11_POLL_SECONDS = 0.5                 # target cadence, NOT broker fill guarantee
V11_BAR_REFRESH_SECONDS = 2.0          # closed-bar feature cache; intrabar triggers on polls
V11_FEATURE_BARS = 360
V11_MAX_BAR_AGE_SECONDS = 120
V11_CANDIDATE_TTL_SECONDS = 20          # stale blocked candidates are never chased
V11_MAX_TICK_CATCHUP_SECONDS = 120
V11_UNKNOWN_COMMISSION_RESERVE_R = 0.03 # replace only with measured broker costs
V11_MIN_SLIPPAGE_RESERVE_R = 0.03
V11_SLIPPAGE_ROUND_TRIP_MULTIPLIER = 2.0 # P90 entry slippage converted by actual stop ATR
V11_MAX_COST_TO_TARGET = 0.35
V11_NEGATIVE_EVIDENCE_MIN_EVENTS = 12
V11_NEGATIVE_EVIDENCE_R = -0.15
V11_ESTABLISHED_MIN_EVENTS = 24
V11_ESTABLISHED_MIN_EFFECTIVE_N = 8.0
V11_ESTABLISHED_MIN_BROKER_TRADES = 5
V11_MIN_EXPECTED_NET_R = 0.05
V11_PROBES_PER_SYMBOL_DAY = 8           # exploration ceiling, never a trade quota
V11_PROBES_TOTAL_DAY = 24
V11_PROBE_RISK_MULTIPLIER = 0.25
V11_PROBE_HARD_RISK_PCT = 0.001        # max 0.10% actual planned SL risk on cold-start probes
V11_EVIDENCE_RISK_MULTIPLIER = 0.50
V11_POLICY_MIN_PAIRS = 60
V11_POLICY_MIN_DAYS = 3
V11_MAX_CANDIDATES_PER_POLL = 8        # try next local setup after candidate-specific rejection
V11_SUPERLEARNER_VETO_PROBABILITY = 0.35 # active broker-net model; strong conflicts only
V11_SUPERLEARNER_DERISK_BELOW = 0.47
V11_SUPERLEARNER_DERISK_MULTIPLIER = 0.70
V11_ASYNC_LUNA_ENABLED = True          # reviewed HOLD/CONFIRM affects that candidate
V11_LUNA_MAX_WAIT_SECONDS = 14.0       # hot execution loop never blocks on the network
V11_LUNA_QUEUE_SIZE = 6
V11_LUNA_WORKERS = 2
