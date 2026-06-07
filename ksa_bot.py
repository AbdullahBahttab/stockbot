"""
KSA Trading Bot — Scans exactly like Claude does manually
────────────────────────────────────────────────────────
HOW IT WORKS:
  1. Runs all 8 Finviz screener links
  2. Cross-matches stocks (more screeners = stronger signal)
  3. Checks 52W High/Price ratio — skips reverse-split stocks
  4. Checks StockAnalysis for news/catalyst
  5. Sends alert with Entry / Stop / Target

SCHEDULE (KSA time):
  11:00 AM  → Pre-Market scan
   4:30 PM  → Market Open scan
  Every 10 min during market hours → live scan

COMMANDS:
  /scan         → run scan now
  /check AAPL   → check one ticker
  /ping         → bot alive?
  /help         → show help

SETUP:
  pip install requests beautifulsoup4 schedule pytz yfinance
  Set BOT_TOKEN and ADMIN_ID below, then: python ksa_bot.py
"""

import os, sys, re, json, time, logging, threading
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
import yfinance as yf
import schedule
import pytz

# ══════════════════════════════════════════════════════════════
#  CONFIG — edit these
# ══════════════════════════════════════════════════════════════
BOT_TOKEN  = "8916814687:AAEWtTQuO10hTp7yZHqsMzDlCOlhibo2nhQ"
ADMIN_ID   = "179463282"      # your Telegram numeric ID
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
ALERTED_FILE = os.path.join(BASE_DIR, "ksa_alerted.json")

ALERT_COOLDOWN_H = 4          # don't re-alert same stock within 4 hours
MIN_VOLUME       = 100_000    # skip stocks under 100K volume
MIN_CHANGE_PCT   = 5.0        # skip stocks moving less than 5%
MAX_52W_RATIO    = 10.0       # 52W_high / price > 10 = likely reverse split → skip
MAX_PRICE        = 20.0       # skip stocks over $20

KSA     = pytz.timezone("Asia/Riyadh")
EASTERN = pytz.timezone("US/Eastern")

# ══════════════════════════════════════════════════════════════
#  KNOWN BAD STOCKS — always skip these (confirmed reverse splits)
# ══════════════════════════════════════════════════════════════
BLACKLIST = {
    "ADTX","JAGX","LRHC","PHGE","ARTL","ONCO","WCT","CDT","EZRA",
    "ISPC","KITT","FGL","CHR","LGHL","UZX","WGRX","LNKS","NTCL",
    "GNTA","ORIS","IFBD","FOFO","HKIT","SDOT","OLOX","CMND",
    "NCT","SPRC","MASK","IOTR","YYGH","GMM","GNS","SNYR","BRTX",
    "SNGX","ASTC","CGTL","NTCL","VMAR","NUWE","SUGP","LIQT",
    "ADTX","TVGN","NAKA","ASBP","SCAG",
}

# ══════════════════════════════════════════════════════════════
#  FINVIZ SCREENER LINKS (all 8 from strategy)
# ══════════════════════════════════════════════════════════════
SCREENERS = [
    # 1 — Pre-market broad
    "https://finviz.com/screener.ashx?v=111&f=cap_microunder,sh_relvol_o2,sh_price_u20,ta_rsi_nob60,sh_vol_o1000&o=-relativevolume",
    # 2 — Pre-market float <10M
    "https://finviz.com/screener.ashx?v=111&f=cap_microunder,sh_float_u10,sh_relvol_o2,sh_price_u20,ta_rsi_nob60,sh_vol_o10&o=-relativevolume",
    # 3 — Pre-market float <5M aggressive
    "https://finviz.com/screener.ashx?v=111&f=cap_microunder,sh_float_u5,sh_price_u10,ta_rsi_os40,sh_relvol_o1&o=-relativevolume",
    # 4 — Pre-market float <15M extra
    "https://finviz.com/screener.ashx?v=111&f=cap_microunder,sh_price_u10,sh_float_u15,ta_rsi_os40,sh_relvol_o1&o=-relativevolume",
    # 5 — Market open high activity
    "https://finviz.com/screener.ashx?v=111&f=cap_microunder,sh_relvol_o3,sh_price_u20,ta_rsi_nob60,sh_vol_o3000&o=-relativevolume",
    # 6 — Market open early entry
    "https://finviz.com/screener.ashx?v=111&f=cap_microunder,sh_float_u10,sh_relvol_o2,sh_price_u10,ta_rsi_nob60,sh_vol_o500&o=-relativevolume",
    # 7 — Market open live movers
    "https://finviz.com/screener.ashx?v=111&f=cap_microunder,sh_relvol_o3,sh_price_u20,ta_rsi_nob60,sh_vol_o3000&o=-relativevolume",
    # 8 — High relative volume (catches everything)
    "https://finviz.com/screener.ashx?v=111&f=cap_microunder,sh_relvol_o5,sh_price_u20,sh_vol_o100000&o=-relativevolume",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ksa_bot")


# ══════════════════════════════════════════════════════════════
#  TELEGRAM HELPERS
# ══════════════════════════════════════════════════════════════
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_send(chat_id: str, text: str):
    try:
        requests.post(
            f"{TG_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        log.error(f"tg_send: {e}")

def tg_get_updates(offset=None):
    try:
        params = {"timeout": 20, "limit": 10}
        if offset:
            params["offset"] = offset
        r = requests.get(f"{TG_BASE}/getUpdates", params=params, timeout=25)
        return r.json().get("result", [])
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════
#  ALERTED CACHE — avoid duplicate alerts
# ══════════════════════════════════════════════════════════════
def load_alerted():
    if os.path.exists(ALERTED_FILE):
        try:
            return json.load(open(ALERTED_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_alerted(data):
    json.dump(data, open(ALERTED_FILE, "w", encoding="utf-8"), indent=2)

def already_alerted(ticker: str) -> bool:
    data = load_alerted()
    ts = data.get(ticker)
    if not ts:
        return False
    alerted_at = datetime.fromisoformat(ts)
    return datetime.now() - alerted_at < timedelta(hours=ALERT_COOLDOWN_H)

def mark_alerted(ticker: str):
    data = load_alerted()
    data[ticker] = datetime.now().isoformat()
    save_alerted(data)


# ══════════════════════════════════════════════════════════════
#  FINVIZ SCRAPER
# ══════════════════════════════════════════════════════════════
def fetch_finviz_screener(url: str) -> list[str]:
    """Returns list of tickers from one Finviz screener URL."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        tickers = []
        # Finviz ticker cells have class "screener-link-primary"
        for tag in soup.select("a.screener-link-primary"):
            t = tag.text.strip()
            if t:
                tickers.append(t)
        return tickers
    except Exception as e:
        log.warning(f"Finviz fetch failed: {e}")
        return []


def cross_match_screeners() -> dict[str, int]:
    """
    Run all 8 screeners, return dict of {ticker: count_of_screeners}.
    Stocks in more screeners = stronger signal.
    """
    counts: dict[str, int] = {}
    for i, url in enumerate(SCREENERS, 1):
        tickers = fetch_finviz_screener(url)
        log.info(f"Screener {i}: {len(tickers)} tickers")
        for t in tickers:
            counts[t] = counts.get(t, 0) + 1
        time.sleep(1)  # polite delay
    return counts


# ══════════════════════════════════════════════════════════════
#  STOCKANALYSIS SCRAPER — get news/catalyst for a ticker
# ══════════════════════════════════════════════════════════════
def fetch_catalyst(ticker: str) -> str:
    """Scrape StockAnalysis news section for latest catalyst."""
    try:
        url = f"https://stockanalysis.com/stocks/{ticker.lower()}/"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        # Look for news items
        news_items = []
        for tag in soup.select("div[class*='news'] a, li[class*='news'] a, a[href*='/news/']"):
            txt = tag.get_text(strip=True)
            if len(txt) > 20:
                news_items.append(txt)
        if news_items:
            return news_items[0][:120]

        # Fallback: look for any paragraph with keywords
        body = soup.get_text(" ", strip=True).lower()
        for kw in ["fda", "contract", "partnership", "acquisition", "approved",
                   "dod", "air force", "clinical", "earnings", "buyback"]:
            if kw in body:
                # Find sentence containing keyword
                for sent in body.split("."):
                    if kw in sent and len(sent) > 20:
                        return sent.strip()[:120].title()
        return "No catalyst found — check manually"
    except Exception:
        return "Catalyst check failed — check manually"


# ══════════════════════════════════════════════════════════════
#  YFINANCE DATA — price, 52W range, volume, float
# ══════════════════════════════════════════════════════════════
def fetch_stock_data(ticker: str) -> dict | None:
    """
    Returns dict with: price, change_pct, volume, float_shares,
                       week52_high, week52_low, day_high, day_low
    Returns None if data unavailable.
    """
    try:
        tk = yf.Ticker(ticker)
        info = tk.info

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price or price <= 0:
            return None

        week52_high  = info.get("fiftyTwoWeekHigh", 0)
        week52_low   = info.get("fiftyTwoWeekLow", 0)
        volume       = info.get("volume") or info.get("regularMarketVolume", 0)
        float_shares = info.get("floatShares", 0)
        prev_close   = info.get("previousClose") or info.get("regularMarketPreviousClose", price)
        day_high     = info.get("dayHigh") or info.get("regularMarketDayHigh", price)
        day_low      = info.get("dayLow") or info.get("regularMarketDayLow", price)
        market_cap   = info.get("marketCap", 0)

        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0

        return {
            "price":        round(price, 4),
            "change_pct":   round(change_pct, 2),
            "volume":       volume,
            "float_shares": float_shares,
            "week52_high":  week52_high,
            "week52_low":   week52_low,
            "day_high":     day_high,
            "day_low":      day_low,
            "market_cap":   market_cap,
            "prev_close":   prev_close,
        }
    except Exception as e:
        log.warning(f"{ticker} yfinance error: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  VOL5 VMA5 — average volume of last 5 x 5-min candles
# ══════════════════════════════════════════════════════════════
def fetch_vma5(ticker: str) -> float:
    """
    Downloads last 5-min candles and returns average volume of last 5.
    Replaces WeBull VOL5 VMA5. Returns 0 if unavailable.
    """
    try:
        df = yf.download(ticker, period="1d", interval="5m", progress=False)
        if df is None or df.empty or len(df) < 5:
            return 0.0
        return float(df["Volume"].iloc[-5:].mean())
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════
#  LEVEL 1 — Bid/Ask pressure (replaces WeBull Level 1)
# ══════════════════════════════════════════════════════════════
def fetch_level1(ticker: str) -> dict:
    """
    Gets bid/ask sizes from yfinance and calculates pressure ratio.
    Returns dict with ask_pct, bid_pct, signal.
    """
    try:
        info     = yf.Ticker(ticker).info
        bid_size = info.get("bidSize", 0) or 0
        ask_size = info.get("askSize", 0) or 0
        total    = bid_size + ask_size
        if total == 0:
            return {"ask_pct": 50, "bid_pct": 50, "signal": "⚪ No data"}
        ask_pct = round(ask_size / total * 100, 1)
        bid_pct = round(bid_size / total * 100, 1)
        if ask_pct >= 70:
            signal = f"🔴 SELLERS ({ask_pct}% Ask) — skip"
        elif bid_pct >= 50:
            signal = f"🟢 BUYERS ({bid_pct}% Bid) — OK"
        else:
            signal = f"🟡 Neutral (Ask {ask_pct}%)"
        return {"ask_pct": ask_pct, "bid_pct": bid_pct, "signal": signal}
    except Exception:
        return {"ask_pct": 50, "bid_pct": 50, "signal": "⚪ No data"}


# ══════════════════════════════════════════════════════════════
#  NEWS — yfinance + Finviz news (replaces keyword matching)
# ══════════════════════════════════════════════════════════════
def fetch_news(ticker: str) -> tuple[str, str]:
    """
    Returns (headline, quality) where quality is STRONG / WEAK / UNKNOWN.
    Sources (in order): Globe Newswire → yfinance → Finviz news tab.
    """
    headline = ""

    # 1️⃣ Globe Newswire — best source for FDA, DoD contracts, partnerships
    try:
        url  = f"https://www.globenewswire.com/RssFeed/company/{ticker}"
        r    = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "xml")
        item = soup.find("item")
        if item and item.find("title"):
            headline = item.find("title").get_text(strip=True)
    except Exception:
        pass

    # 2️⃣ yfinance news
    if not headline:
        try:
            news = yf.Ticker(ticker).news
            if news:
                headline = news[0].get("title", "")
        except Exception:
            pass

    # 3️⃣ Finviz news tab
    if not headline:
        try:
            url  = f"https://finviz.com/quote.ashx?t={ticker}"
            r    = requests.get(url, headers=HEADERS, timeout=10)
            soup = BeautifulSoup(r.text, "html.parser")
            row  = soup.select_one("table.fullview-news-outer a")
            if row:
                headline = row.get_text(strip=True)
        except Exception:
            pass

    if not headline:
        return "No news found", "UNKNOWN"

    hl_lower = headline.lower()

    # Strong catalyst keywords
    strong = [
        "fda", "approved", "approval", "contract", "dod", "air force", "navy",
        "army", "pentagon", "sbir", "phase 2", "phase 3", "clinical trial",
        "acquisition", "merger", "buyout", "partnership", "license agreement",
        "earnings beat", "record revenue", "guidance raised", "insider buy",
        "ceo buy", "share repurchase", "buyback", "dividend",
        "ebola", "outbreak", "vaccine", "biodefense",
        "bitcoin", "btc", "crypto", "data center", "ai contract",
        "department of", "government contract", "awarded", "selected",
        "joint venture", "strategic alliance",
    ]

    # Weak/bad keywords
    weak = [
        "reverse split", "1-for-", "offering", "dilut", "warrant",
        "deficiency", "delisting", "going concern", "bankruptcy",
        "class action", "sec investigation", "restatement",
        "no news", "no catalyst", "reddit", "meme",
        "name change", "rebrands", "renames",
    ]

    for kw in weak:
        if kw in hl_lower:
            return headline[:120], "WEAK"

    for kw in strong:
        if kw in hl_lower:
            return headline[:120], "STRONG"

    return headline[:120], "UNKNOWN"


# ══════════════════════════════════════════════════════════════
#  REVERSE SPLIT DETECTOR
# ══════════════════════════════════════════════════════════════
def is_reverse_split(data: dict) -> tuple[bool, str]:
    """
    Returns (True, reason) if reverse split suspected, else (False, "").
    Rule: 52W_high / current_price > MAX_52W_RATIO
    """
    price = data["price"]
    high  = data["week52_high"]
    if price <= 0 or high <= 0:
        return False, ""
    ratio = high / price
    if ratio > MAX_52W_RATIO:
        return True, f"52W high ${high:.2f} vs ${price:.2f} ({ratio:.0f}x)"
    return False, ""


# ══════════════════════════════════════════════════════════════
#  TRADE PLAN CALCULATOR
# ══════════════════════════════════════════════════════════════
def calc_trade_plan(data: dict) -> dict:
    price    = data["price"]
    day_low  = data["day_low"]
    week52_h = data["week52_high"]

    # Stop: below day low (min 5% away from price)
    stop = max(day_low * 0.98, price * 0.85)
    stop = round(stop, 2)

    # Target 1: +20% from price
    target1 = round(price * 1.20, 2)

    # Target 2: 52W high (if meaningfully higher)
    target2 = round(week52_h, 2) if week52_h > price * 1.10 else round(price * 1.40, 2)

    risk    = price - stop
    reward  = target1 - price
    rr      = round(reward / risk, 1) if risk > 0 else 0

    return {
        "entry":   round(price, 2),
        "stop":    stop,
        "target1": target1,
        "target2": target2,
        "rr":      rr,
    }


# ══════════════════════════════════════════════════════════════
#  FORMAT ALERT MESSAGE — matches Claude's style
# ══════════════════════════════════════════════════════════════
def format_alert(ticker: str, data: dict, headline: str, quality: str,
                 screener_count: int, session: str,
                 vma5: float, level1: dict) -> str:

    plan    = calc_trade_plan(data)
    price   = data["price"]
    chg     = data["change_pct"]
    vol     = data["volume"]
    fl      = data["float_shares"]
    w52h    = data["week52_high"]
    w52l    = data["week52_low"]
    mcap    = data["market_cap"]

    chg_sign  = "+" if chg >= 0 else ""
    vol_str   = f"{vol/1_000_000:.1f}M" if vol >= 1_000_000 else f"{vol/1_000:.0f}K"
    fl_str    = f"{fl/1_000_000:.2f}M" if fl >= 1_000_000 else "Unknown"
    mcap_str  = f"${mcap/1_000_000:.1f}M" if mcap else "Unknown"
    ratio     = round(w52h / price, 1) if price > 0 else 0
    ratio_str = f"✅ Clean ({ratio}x)" if ratio <= 5 else f"⚠️ Check ({ratio}x)"

    vma5_str  = f"{vma5/1_000_000:.2f}M" if vma5 >= 1_000_000 else f"{vma5/1_000:.0f}K"
    vma5_ok   = vma5 >= 500_000
    vma5_icon = "✅" if vma5_ok else "❌"

    quality_icon = "🟢" if quality == "STRONG" else "🔴" if quality == "WEAK" else "🟡"
    crosses      = "✅✅✅" if screener_count >= 3 else "✅✅" if screener_count == 2 else "✅"
    session_icon = "🌅" if session == "PRE" else "📈" if session == "OPEN" else "🌙"

    entry_ok = vma5_ok and level1["ask_pct"] < 70
    entry_signal = "✅ ENTER NOW" if entry_ok else "⏳ WAIT — conditions not met"

    lines = [
        f"{session_icon} <b>NEW SETUP — {ticker}</b>  [{session}]",
        f"",
        f"💰 Price:     <b>${price}  ({chg_sign}{chg}%)</b>",
        f"📊 Volume:    {vol_str}",
        f"🏷️ Float:     {fl_str}",
        f"🏢 Mkt Cap:   {mcap_str}",
        f"📅 52W Range: ${w52l:.2f} – ${w52h:.2f}  {ratio_str}",
        f"🔁 Screeners: {crosses} ({screener_count}/8)",
        f"",
        f"📰 {quality_icon} Catalyst [{quality}]:",
        f"   {headline}",
        f"",
        f"📈 VOL5 VMA5: {vma5_icon} {vma5_str} {'(need 500K+)' if not vma5_ok else ''}",
        f"⚖️ Level 1:   {level1['signal']}",
        f"",
        f"📋 <b>TRADE PLAN</b>",
        f"   Entry:    <b>${plan['entry']}</b>",
        f"   Stop:     ${plan['stop']}",
        f"   Target 1: ${plan['target1']} (+20%)",
        f"   Target 2: ${plan['target2']}",
        f"   R/R:      1:{plan['rr']}",
        f"",
        f"🚦 <b>{entry_signal}</b>",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  MAIN SCAN LOGIC
# ══════════════════════════════════════════════════════════════
def run_scan(session: str = "OPEN") -> list[str]:
    """
    Full scan: run screeners → filter → check data → send alerts.
    Returns list of tickers that passed all filters.
    """
    log.info(f"Starting {session} scan...")

    # Step 1: Cross-match screeners
    counts = cross_match_screeners()
    log.info(f"Total unique tickers found: {len(counts)}")

    if not counts:
        return []

    # Sort by screener count (most cross-matched first)
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)

    passed   = []
    skipped  = []
    messages = []

    for ticker, cnt in ranked:
        # Skip blacklisted
        if ticker in BLACKLIST:
            skipped.append(f"{ticker}: blacklist")
            continue

        # Skip if recently alerted
        if already_alerted(ticker):
            skipped.append(f"{ticker}: alerted recently")
            continue

        # Fetch data
        data = fetch_stock_data(ticker)
        if not data:
            skipped.append(f"{ticker}: no data")
            continue

        price  = data["price"]
        change = data["change_pct"]
        volume = data["volume"]

        # Price filter
        if price > MAX_PRICE:
            skipped.append(f"{ticker}: price ${price} > ${MAX_PRICE}")
            continue

        # Volume filter
        if volume < MIN_VOLUME:
            skipped.append(f"{ticker}: volume {volume} < {MIN_VOLUME}")
            continue

        # Change filter
        if abs(change) < MIN_CHANGE_PCT:
            skipped.append(f"{ticker}: change {change}% < {MIN_CHANGE_PCT}%")
            continue

        # Reverse split check
        rs, reason = is_reverse_split(data)
        if rs:
            skipped.append(f"{ticker}: reverse split ({reason})")
            BLACKLIST.add(ticker)
            continue

        # Fetch news + quality
        headline, quality = fetch_news(ticker)

        # Skip if clearly bad news
        if quality == "WEAK":
            skipped.append(f"{ticker}: weak catalyst ({headline[:50]})")
            continue

        # Fetch VOL5 VMA5
        vma5 = fetch_vma5(ticker)

        # Fetch Level 1
        level1 = fetch_level1(ticker)

        # Skip if sellers dominating (Ask% >= 70%)
        if level1["ask_pct"] >= 70:
            skipped.append(f"{ticker}: sellers dominating (Ask {level1['ask_pct']}%)")
            continue

        time.sleep(0.3)

        # Format and collect alert
        msg = format_alert(ticker, data, headline, quality, cnt, session, vma5, level1)
        messages.append((ticker, msg))
        passed.append(ticker)

    # Send alerts only — no status messages
    for ticker, msg in messages[:5]:
        tg_send(ADMIN_ID, msg)
        mark_alerted(ticker)
        time.sleep(1)

    log.info(f"Scan done. Passed: {len(passed)}, Skipped: {len(skipped)}")
    return passed


def check_single(ticker: str):
    """Check one specific ticker and send result."""
    ticker = ticker.upper().strip()
    log.info(f"Checking {ticker}...")

    if ticker in BLACKLIST:
        tg_send(ADMIN_ID, f"❌ {ticker} is blacklisted (known reverse split). Skip.")
        return

    data = fetch_stock_data(ticker)
    if not data:
        tg_send(ADMIN_ID, f"❌ Could not fetch data for {ticker}. Check the ticker.")
        return

    rs, reason = is_reverse_split(data)
    if rs:
        tg_send(ADMIN_ID, f"⚠️ {ticker} — POSSIBLE REVERSE SPLIT\n{reason}\n\nAvoid entering.")
        return

    headline, quality = fetch_news(ticker)
    vma5   = fetch_vma5(ticker)
    level1 = fetch_level1(ticker)

    msg = format_alert(ticker, data, headline, quality, 1, "CHECK", vma5, level1)
    tg_send(ADMIN_ID, msg)


# ══════════════════════════════════════════════════════════════
#  SESSION DETECTOR
# ══════════════════════════════════════════════════════════════
def current_session() -> str:
    now_et = datetime.now(EASTERN)
    h = now_et.hour + now_et.minute / 60
    if 4 <= h < 9.5:
        return "PRE"
    elif 9.5 <= h < 16:
        return "OPEN"
    elif 16 <= h < 20:
        return "AFTER"
    return "CLOSED"


def market_is_active() -> bool:
    return current_session() in ("PRE", "OPEN", "AFTER")


# ══════════════════════════════════════════════════════════════
#  SCHEDULER — KSA times
# ══════════════════════════════════════════════════════════════
def scheduled_pre_market():
    if market_is_active():
        run_scan("PRE")

def scheduled_market_open():
    if market_is_active():
        run_scan("OPEN")

def scheduled_live():
    s = current_session()
    if s == "OPEN":
        run_scan("OPEN")

def setup_schedule():
    # Pre-market: 11:00 AM KSA = 08:00 AM EDT
    schedule.every().day.at("08:00").do(scheduled_pre_market)
    # Market open: 4:30 PM KSA = 09:30 AM EDT
    schedule.every().day.at("09:30").do(scheduled_market_open)
    # Live scans every 10 min during market hours
    schedule.every(10).minutes.do(scheduled_live)
    log.info("Scheduler set: Pre-Market 08:00 EDT, Market Open 09:30 EDT, then every 10 min")


# ══════════════════════════════════════════════════════════════
#  TELEGRAM COMMAND HANDLER
# ══════════════════════════════════════════════════════════════
def handle_commands():
    offset = None
    log.info("Listening for Telegram commands...")

    while True:
        updates = tg_get_updates(offset)
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text    = msg.get("text", "").strip()

            if not text or not chat_id:
                continue

            # Only respond to admin
            if chat_id != ADMIN_ID:
                tg_send(chat_id, "❌ Unauthorized.")
                continue

            cmd = text.lower().split()[0]

            if cmd == "/ping":
                session = current_session()
                now_ksa = datetime.now(KSA).strftime("%H:%M KSA")
                tg_send(chat_id, f"✅ Bot alive\n🕐 {now_ksa}\n📊 Session: {session}")

            elif cmd == "/scan":
                session = current_session()
                threading.Thread(target=run_scan, args=(session,), daemon=True).start()

            elif cmd == "/check":
                parts = text.split()
                if len(parts) >= 2:
                    threading.Thread(
                        target=check_single, args=(parts[1],), daemon=True
                    ).start()
                else:
                    tg_send(chat_id, "Usage: /check TICKER\nExample: /check NCEL")

            elif cmd == "/blacklist":
                bl = sorted(BLACKLIST)
                tg_send(chat_id, "🚫 Blacklist:\n" + ", ".join(bl))

            elif cmd == "/session":
                session = current_session()
                now_et  = datetime.now(EASTERN).strftime("%H:%M ET")
                now_ksa = datetime.now(KSA).strftime("%H:%M KSA")
                tg_send(chat_id,
                    f"🕐 {now_ksa} / {now_et}\n"
                    f"📊 Session: {session}\n"
                    f"{'✅ Market active' if market_is_active() else '❌ Market closed'}"
                )

            elif cmd == "/help":
                tg_send(chat_id,
                    "📖 <b>Commands:</b>\n\n"
                    "/scan — run full scan now\n"
                    "/check TICKER — check one stock\n"
                    "/ping — bot status\n"
                    "/session — current market session\n"
                    "/blacklist — show avoided stocks\n\n"
                    "🔔 Auto scans:\n"
                    "• 11:00 AM KSA → Pre-Market\n"
                    "• 4:30 PM KSA → Market Open\n"
                    "• Every 10 min during market hours"
                )

        time.sleep(2)


# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════
def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    log.info("KSA Trading Bot starting...")
    tg_send(ADMIN_ID,
        "🤖 <b>KSA Trading Bot started</b>\n\n"
        "Auto scans:\n"
        "• 11:00 AM KSA → Pre-Market\n"
        "• 4:30 PM KSA → Market Open\n"
        "• Every 10 min during market hours\n\n"
        "Type /help for commands"
    )

    setup_schedule()

    # Run scheduler in background thread
    threading.Thread(target=run_scheduler, daemon=True).start()

    # Command handler runs in main thread
    handle_commands()
