V6.9.4 EXPECTANCY -> LUNA BRIDGE HOTFIX (DEMO ONLY)

WHY THIS EXISTS
The fresh pipeline diagnostic exposed a real dead-zone:
  XAU scalp P=0.367, threshold=0.400, Expected-R=+0.578R, R:R=3.30
was rejected before Luna. The previous borderline lane had a fixed minimum
probability of 0.44, so a soft reject below a 0.40 threshold could NEVER reach
Luna even when its expectancy was strongly positive.

WHAT CHANGES
- Borderline Luna eligibility is now dynamic:
    probability >= max(absolute floor, break-even + margin, threshold - max gap)
- Candidate must still have meaningful positive Expected-R.
- Hard Spartan, DOM, random/noisy/drift, Bayesian hard vetoes stay non-overridable.
- Edge Recovery quarantine still runs BEFORE any paid Luna call.
- Luna still needs >=0.82 confidence to rescue a borderline candidate.
- Rescue trades stay capped at 2/day and run at 0.30x risk multiplier.
- No extra polling / no chat history / no repeated paid calls are added.

CURRENT DEFAULTS
- absolute probability floor: 0.30
- max probability gap: 0.08
- break-even safety margin: +0.05
- minimum expected-R: +0.15R
- Luna rescue min confidence: 0.82
- rescue max broker-DEMO trades/day: 2
- rescue risk multiplier: 0.30x

INSTALL
1. Stop the 5 trading windows with 09_STOP_ALL_WINDOWS.bat.
2. Run 15_BACKUP_LEARNING.bat.
3. Copy ALL files in this TM_V6_SCALP_INTELLIGENCE_DEMO folder into your existing
   TM_V6_SCALP_INTELLIGENCE_DEMO folder and choose Replace.
4. Do NOT delete .env, trading_machine.db, data, reports or backups.
5. Run 10_OFFLINE_SELF_TEST.bat. It must say OFFLINE SELF TEST PASSED.
6. Run 31_EXPECTANCY_LUNA_BRIDGE_STATUS.bat.
7. Start 00_RUN_ME_DEMO.bat.

This is not a permission to force trades. It only routes a narrow class of
high-R:R, positive-expectancy, near-threshold soft rejects to Luna for a final
second opinion. Deterministic safety gates remain in control.
