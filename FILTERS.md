# StockBot — Filters & Indicators Reference

Complete reference of every indicator and filter the bot uses. Values reflect the
current live config. Set the EMAs on a **daily** Webull chart to match the EMA strategy.

**Active strategies:** 🎯 GAP · 📈 EMA  ·  **benched:** 🥇🥈 A/B (`/scan` runs it manually)
and 🚀 ORB (disabled 2026-06-18 — 0 alerts all week even after loosening vol to 2.5×;
code kept, re-enable by uncommenting its schedule line).

---

## 📊 Indicators (computed from Webull candle data)

| Indicator | What it measures |
|---|---|
| **RSI (14)** | Momentum strength (0–100) |
| **MFI (14)** | Money Flow Index — volume-weighted RSI |
| **OBV** (↑ / ↓ / →) | On-Balance Volume — money flowing in vs out |
| **VWAP** | Volume-weighted average price (over/under-extension) |
| **EMA** | 9 (inside "ignition"); **100 & 200** (EMA strategy, daily) |
| **Ignition** | Composite: VWAP reclaim + volume surge (last 15m vs prior 30m) + acceleration + rising 9-EMA |
| **RelVol** | Current volume ÷ average volume |
| **Change %** | Price vs previous close |
| **Float / Market cap** | Share float (millions) / market cap (millions) |
| **Catalyst score** | News scored by Claude AI + keyword rules |

---

## 🛡️ Universal safety floors — ALL strategies (`passes_safety_floors`)

Change these in **one place** and it applies to A/B, ORB, GAP, and EMA.

- **Price:** $1.50 – $65  (`MIN_PRICE` / `MAX_PRICE`)
- **Liquidity:** dollar-volume ≥ session floor, and volume must be present
  - PRE $300k · **OPEN $2,000,000** · AFTER $500k  (`min_dollar_vol`)
- **Float band:** **2M – 100M shares**  (`MIN_FLOAT_M` / `MAX_FLOAT_M`) — rejects nano-float pumps *and* heavy large-caps; now universal (was A/B-only)
- **Over-extension reject:** change must be **≤ 40%**  (`MAX_CHANGE_PCT`)
- **VWAP extension:** **≤ 1.45×** (`VWAP_LIMIT`; 1.25× parabolic) — shared by A/B + ORB

> These three floors are what block the disaster patterns: illiquid (ASBP),
> nano-float pumps (SDOT), and parabolic crashes (SLBT).

---

## 🥇🥈 A/B — main scan (BENCHED, rules preserved)

Per-session base filters (`FILTERS`):

| Session | min change | min volume | min $-vol | max float | max RSI | max mcap |
|---|---|---|---|---|---|---|
| PRE | 9% | 10k | $300k | 100M | 78 | 300M |
| OPEN | 10% | 500k | $2M | 100M | 78 | 300M |
| AFTER | 9% | 50k | $500k | 100M | 78 | 300M |

Quality rules (`passes_filters`):
- Change **≤ 40%** (max) — not over-extended
- Volume ≥ session floor (bypassed only if RelVol > 15)
- **RSI 52 – 78** (reject < 52 fading, > 78 parabolic)
- **MFI < 85**
- **VWAP ≤ 1.45×** (`VWAP_LIMIT`; 1.25× `VWAP_LIMIT_PARABOLIC` for the daily-RSI parabolic case)
- **RelVol ≥ 2.8×**
- **Float 2M – 100M** (now enforced by the universal floors above, not just here)
- Market cap: reject nano-cap < $1M; reject no-float + mcap > 100M
- **Day-range position ≤ 75%** (don't buy near the day high)
- **"Already dumped" reject:** price not > 25% below the day high
- **Inflow required:** OBV ↑ **and** volume surge (ignition vol-surge ≥ 2 OR RelVol ≥ 3)
- Pump-and-dump guards: no-catalyst-on-big-move, micro-float pump, near-peak + weak catalyst, MFI overbought near high, OBV distribution
- **Grade:** 6-dimension score (momentum/RSI, float, volume, catalyst, day-range position, ignition) → **A ≥ 8, B ≥ 5**, else C (C never alerts)

---

## 🚀 ORB — Opening Range Breakout

- Window: **9:45 – 11:00 ET only** · uses **1-minute** bars
- Opening range = high/low of the first **12 minutes** (`ORB_RANGE_MIN`)
- **Breakout:** latest bar closes **above the OR high** (previous bar did not)
- **Volume surge ≥ 2.5×** the opening-range average (`ORB_MIN_RVOL`)
- **VWAP ≤ 1.45×** (`ORB_VWAP_LIMIT` = shared `VWAP_LIMIT`)
- \+ universal safety floors (incl. the **2M–100M float band**)

---

## 🎯 GAP — Gap-up-on-news → pullback to support

- Prior-day "gap candle" ran **15% – 45%** (`GAP_MIN_PCT` / `GAP_MAX_PCT`)
- **Support** = that day's low · **Target** = that day's high
- **Pullback:** price now within **10%** above support (`GAP_ENTRY_ZONE`), still above it
- Target must be **≥ 15%** above current price (`GAP_MIN_UPSIDE`)
- **Catalyst required** (positive news)
- \+ universal safety floors
- 🎯 **Short SWING / bounce play** — stop below support, target the **prior peak**,
  **hold hours** for the bounce (not a quick scalp; no scale-out).

---

## 📈 EMA — 100-EMA breakout (daily)

- **EMA 100 & EMA 200, on the daily timeframe** (`EMA_FAST` / `EMA_SLOW`)
- **Entry:** price **crosses above the 100-EMA** (prev day below, today above)
- **Target:** the **200-EMA** (must be above price = room to run)
- **Stop:** just below the 100-EMA · **Target:** the 200-EMA
- **RelVol ≥ 3** + **catalyst** required
- \+ universal safety floors
- ⏳ **This is a multi-day SWING** — it plays out over *days*, not minutes. No scalp
  scale-out; hold toward the 200-EMA. It is **excluded from the +5%/30-min scoring**
  (that scalp metric can't judge a swing) — **judge EMA manually over days.**

**To replicate in Webull:** open the **Daily** chart, add **EMA 100** and **EMA 200**,
and watch for a volume-backed break above the 100 toward the 200.

---

## 🎯 Alert outcome tracking (dashboard win-rate)

Each alert is scored on a window that matches the strategy's hold time:
- **PASS** = hit **+5%** (T1) or **+10%** (T2) within the window (`ALERT_T1_PCT` / `ALERT_T2_PCT`)
- **FAIL** = hit the **−7%** stop (`ALERT_STOP_PCT`), or no +5% by the window's end
- **Window is per-strategy:**
  - **A/B & ORB (scalps):** **30 minutes** (`ALERT_OPEN_MIN`)
  - **GAP (bounce held for hours):** **~6 hours / the session** (`GAP_OPEN_MIN=360`) — it's a bounce toward the prior peak, so the 30-min scalp clock was wrong and falsely failed it.
- **EMA is excluded** — it's a multi-day swing, so an intraday metric can't score it; judge it manually over days.

> **Market holidays:** the bot now skips scanning/alerting on US market holidays
> (`MARKET_HOLIDAYS`, e.g. Juneteenth) — before, it only knew about weekends and
> would alert on stale data. Update the holiday set each year.

> The scale-out plan below applies to the **scalp** strategies (A/B, ORB).
> **GAP** uses bounce exits: stop below support, target the prior peak, held hours.
> **EMA** uses swing exits: stop below the 100-EMA, target the 200-EMA, held over days.

## 🎯 Suggested exit plan (shown on every alert)

- **T1:** +1.7% → take 50%
- **T2:** +2.5% → take 30%
- Trail 0.8% after T1 · exit in 27 min if T1 not hit
- (The bot only *alerts* — you execute the scale-out and the stop manually.)

---

*This file documents the config in `main.py`. When you change a value there,
update it here too so they don't drift.*
