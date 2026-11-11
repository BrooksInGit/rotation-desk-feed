# Stockbee swing setups out of sample, 2026-11-11

Script: `swing_oos.py`, results `swing_oos/summary.json`. Jan 2024 to Nov 10 2026 (718 sessions), 5,673 active US
stocks, daily split-adjusted SIP bars, eligible = close >= $5, volume >= 100k, 20-day avg dollar volume >= $10M.
Rules follow tradermonty/claude-trading-skills (stockbee-momentum-burst-screener, stockbee-episodic-pivot-analyzer).
Stop = trigger-day low; costs 0.05% each way (stress 0.25%). Baseline = all eligible stocks over the same window.

| setup | entry, hold | trades | win | avg R | avg % | baseline % | profit factor | 2024 / 2025 / 2026 avg R |
|---|---|---|---|---|---|---|---|---|
| Momentum burst, raw | next open, 5d | 62,421 | 37% | -0.03 | -0.24 | +0.07 | 0.93 | -0.04 / -0.02 / -0.02 |
| Momentum burst, quality filters | at close, 5d | 12,246 | 45% | +0.03 | +0.14 | +0.18 | 1.07 | +0.02 / +0.05 / +0.01 |
| Momentum burst, quality filters | next open, 5d | 12,051 | 42% | -0.01 | -0.02 | +0.07 | 0.99 | -0.04 / +0.02 / -0.02 |
| Episodic pivot, gap >= 4% | next open, 10d | 2,283 | 39% | -0.01 | -1.55 | +0.15 | 0.76 | +0.16 / -0.12 / -0.09 |
| Episodic pivot, gap >= 8% | next open, 10d | 1,379 | 36% | -0.09 | -2.56 | +0.16 | 0.66 | +0.05 / -0.15 / -0.13 |

Taking only the 2 strongest signals per day (by volume ratio) does not rescue either: best is quality momentum burst
at +0.07R with profit factor 0.94. At 0.25% costs every variant is negative.

Verdict: no edge. The best variant (quality momentum burst bought at the close) earns less than simply holding the
average eligible stock for the same days. Episodic pivots without a catalyst check underperform the market by
1.5-3% per trade. Survivorship bias (delisted names missing) flatters these long results, so the truth is likely worse.
Not adopted. Limit: the episodic pivot test has no catalyst filter (the skill scores catalyst quality by hand).
