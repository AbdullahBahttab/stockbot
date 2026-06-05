#!/usr/bin/env python3
"""
main.py — StockBot, single-file deployment.

Contains the SQLite layer + the Telegram scanner bot + the web dashboard in
ONE file. The dashboard runs in a background thread (binds $PORT); the bot
runs in the foreground.

Run anywhere:   python main.py
Railway:        Procfile -> `web: python main.py`

Config via env vars: BOT_TOKEN, ANTHROPIC_API_KEY, DB_PATH (optional), PORT.

NOTE: this file is the single source of truth. The original db.py /
stock_scanner.py / dashboard.py were merged here and moved to archive/.
"""
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "stockbot.db"))



# ========================================================================
# DATABASE  (SQLite)
# ========================================================================

"""
db.py — SQLite layer for Stock Scanner Bot (Linux)
Drop this file next to stock_scanner.py on the server to replace the SQL Server version.
"""
import sqlite3
import logging
import os

log = logging.getLogger("scanner")

DB_OK    = False


def get_conn():
    """Open a fresh SQLite connection."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        log.debug(f"[DB] connect failed: {e}")
        return None


def setup_db():
    """Create all tables if they don't exist. Safe to call on every startup."""
    conn = get_conn()
    if not conn:
        return
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id   INTEGER PRIMARY KEY,
                name      TEXT,
                username  TEXT,
                is_active INTEGER DEFAULT 1,
                is_admin  INTEGER DEFAULT 0,
                pin       TEXT    DEFAULT '1234',
                joined_at DATETIME DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS portfolio (
                chat_id     INTEGER,
                symbol      TEXT,
                entry_price REAL,
                stop_price  REAL,
                t1_price    REAL,
                t2_price    REAL,
                t1_hit      INTEGER  DEFAULT 0,
                t2_hit      INTEGER  DEFAULT 0,
                rsi_warned  INTEGER  DEFAULT 0,
                vol_warned  INTEGER  DEFAULT 0,
                exit_warned INTEGER  DEFAULT 0,
                qty         INTEGER,
                added_at    DATETIME DEFAULT (datetime('now')),
                PRIMARY KEY (chat_id, symbol)
            );

            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER,
                symbol      TEXT,
                entry_price REAL,
                exit_price  REAL,
                qty         INTEGER,
                pnl_dollar  REAL,
                pnl_pct     REAL,
                result      TEXT,
                trade_date  TEXT     DEFAULT (date('now')),
                closed_at   DATETIME DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id              INTEGER  PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT,
                alert_price     REAL,
                grade           TEXT,
                change_pct      REAL,
                float_m         REAL,
                rsi             REAL,
                volume          INTEGER,
                rel_vol         REAL,
                mcap_m          REAL,
                session         TEXT,
                alerted_at      DATETIME DEFAULT (datetime('now')),
                outcome         TEXT,
                close_price     REAL,
                pct_after_alert REAL
            );

            CREATE TABLE IF NOT EXISTS scan_log (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT,
                price       REAL,
                change_pct  REAL,
                grade       TEXT,
                passed      INTEGER,
                skip_reason TEXT,
                session     TEXT,
                scanned_at  DATETIME DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
    except Exception as e:
        log.error(f"[DB] setup_db: {e}")
    finally:
        conn.close()


def test_connection() -> bool:
    """Call once at startup. Creates tables and sets DB_OK flag."""
    global DB_OK
    setup_db()
    conn = get_conn()
    if conn:
        conn.close()
        DB_OK = True
        log.info(f"[DB] SQLite connected OK  ({DB_PATH})")
    else:
        DB_OK = False
        log.warning("[DB] SQLite not available — running with JSON files only")
    return DB_OK


# ─── Users ────────────────────────────────────────────────────────────────────

def db_load_users() -> dict:
    conn = get_conn()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute("SELECT chat_id, name, username, is_active, is_admin, joined_at FROM users")
        result = {}
        for row in cur.fetchall():
            uid = str(row["chat_id"])
            result[uid] = {
                "name":     row["name"]     or uid,
                "username": row["username"] or "",
                "active":   bool(row["is_active"]),
                "is_admin": bool(row["is_admin"]),
                "added":    str(row["joined_at"])[:10] if row["joined_at"] else "",
            }
        return result
    except Exception as e:
        log.error(f"[DB] load_users: {e}")
        return {}
    finally:
        conn.close()


def db_upsert_user(chat_id: str, name: str, username: str,
                   active: bool, is_admin: bool):
    conn = get_conn()
    if not conn:
        return
    try:
        cid = int(chat_id)
        conn.execute("""
            INSERT INTO users (chat_id, name, username, is_active, is_admin)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                name=excluded.name, username=excluded.username,
                is_active=excluded.is_active, is_admin=excluded.is_admin
        """, (cid, name, username, 1 if active else 0, 1 if is_admin else 0))
        conn.commit()
    except Exception as e:
        log.error(f"[DB] upsert_user {chat_id}: {e}")
    finally:
        conn.close()


def db_set_pin(chat_id: str, pin: str):
    conn = get_conn()
    if not conn:
        return
    try:
        conn.execute("UPDATE users SET pin=? WHERE chat_id=?",
                     (str(pin)[:10], int(chat_id)))
        conn.commit()
    except Exception as e:
        log.error(f"[DB] set_pin {chat_id}: {e}")
    finally:
        conn.close()


def db_sync_users(users_dict: dict):
    for uid, u in users_dict.items():
        db_upsert_user(uid, u.get("name", uid), u.get("username", ""),
                       u.get("active", True), u.get("is_admin", False))


# ─── Alerted / Cooldown ───────────────────────────────────────────────────────

def db_load_alerted(cooldown_seconds: int) -> dict:
    conn = get_conn()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT a.symbol, CAST(strftime('%s', a.alerted_at) AS INTEGER) AS ts
            FROM alerts AS a
            INNER JOIN (
                SELECT symbol, MAX(alerted_at) AS last_alert
                FROM   alerts
                GROUP  BY symbol
            ) mx ON a.symbol = mx.symbol AND a.alerted_at = mx.last_alert
            WHERE (strftime('%s','now') - strftime('%s', a.alerted_at)) < ?
        """, (int(cooldown_seconds),))
        return {row["symbol"]: float(row["ts"]) for row in cur.fetchall()}
    except Exception as e:
        log.error(f"[DB] load_alerted: {e}")
        return {}
    finally:
        conn.close()


def db_log_alert(symbol: str, price: float, grade: str,
                 change_pct: float, float_m, rsi, volume,
                 rel_vol, mcap_m, session: str):
    conn = get_conn()
    if not conn:
        return
    try:
        conn.execute("""
            INSERT INTO alerts
                (symbol, alert_price, grade, change_pct, float_m, rsi,
                 volume, rel_vol, mcap_m, session)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, price, grade, change_pct,
            float(float_m) if float_m is not None else None,
            float(rsi)     if rsi     is not None else None,
            int(volume)    if volume  is not None else None,
            float(rel_vol) if rel_vol is not None else None,
            float(mcap_m)  if mcap_m  is not None else None,
            session,
        ))
        conn.commit()
    except Exception as e:
        log.error(f"[DB] log_alert {symbol}: {e}")
    finally:
        conn.close()


def db_get_recent_alerts(hours: int = 24) -> list:
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT symbol, alert_price, grade, change_pct,
                   float_m, rsi, session, alerted_at
            FROM   alerts
            WHERE  alerted_at >= datetime('now', ? || ' hours')
            ORDER  BY alerted_at DESC
        """, (f"-{hours}",))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"[DB] get_recent_alerts: {e}")
        return []
    finally:
        conn.close()


def db_get_todays_alerts() -> list:
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, symbol, alert_price, grade, change_pct, session, alerted_at
            FROM   alerts
            WHERE  DATE(alerted_at) = DATE('now')
              AND  outcome IS NULL
            ORDER  BY alerted_at ASC
        """)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"[DB] get_todays_alerts: {e}")
        return []
    finally:
        conn.close()


def db_update_alert_outcome(alert_id: int, close_price: float,
                            pct_after: float, outcome: str):
    conn = get_conn()
    if not conn:
        return
    try:
        conn.execute("""
            UPDATE alerts
            SET close_price=?, pct_after_alert=?, outcome=?
            WHERE id=?
        """, (round(close_price, 4), round(pct_after, 4), outcome, alert_id))
        conn.commit()
    except Exception as e:
        log.error(f"[DB] update_alert_outcome {alert_id}: {e}")
    finally:
        conn.close()


# ─── Portfolio ────────────────────────────────────────────────────────────────

def db_load_portfolio() -> dict:
    conn = get_conn()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT chat_id, symbol,
                   entry_price, stop_price, t1_price, t2_price,
                   t1_hit, t2_hit, rsi_warned, vol_warned, exit_warned, qty
            FROM portfolio
        """)
        port = {}
        for row in cur.fetchall():
            uid = str(row["chat_id"])
            port.setdefault(uid, {})[row["symbol"]] = {
                "entry":       float(row["entry_price"]),
                "stop":        float(row["stop_price"])  if row["stop_price"]  is not None else None,
                "t1":          float(row["t1_price"])    if row["t1_price"]    is not None else None,
                "t2":          float(row["t2_price"])    if row["t2_price"]    is not None else None,
                "t1_hit":      bool(row["t1_hit"]),
                "t2_hit":      bool(row["t2_hit"]),
                "rsi_warned":  bool(row["rsi_warned"]),
                "vol_warned":  bool(row["vol_warned"]),
                "exit_warned": bool(row["exit_warned"]),
                "qty":         int(row["qty"]) if row["qty"] is not None else None,
            }
        return port
    except Exception as e:
        log.error(f"[DB] load_portfolio: {e}")
        return {}
    finally:
        conn.close()


def db_save_position(chat_id: str, symbol: str, pos: dict):
    conn = get_conn()
    if not conn:
        return
    try:
        conn.execute("""
            INSERT INTO portfolio
                (chat_id, symbol, entry_price, stop_price, t1_price, t2_price,
                 t1_hit, t2_hit, rsi_warned, vol_warned, exit_warned, qty)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, symbol) DO UPDATE SET
                entry_price=excluded.entry_price, stop_price=excluded.stop_price,
                t1_price=excluded.t1_price,       t2_price=excluded.t2_price,
                t1_hit=excluded.t1_hit,           t2_hit=excluded.t2_hit,
                rsi_warned=excluded.rsi_warned,   vol_warned=excluded.vol_warned,
                exit_warned=excluded.exit_warned, qty=excluded.qty
        """, (
            int(chat_id), symbol,
            pos.get("entry"), pos.get("stop"), pos.get("t1"), pos.get("t2"),
            1 if pos.get("t1_hit")      else 0,
            1 if pos.get("t2_hit")      else 0,
            1 if pos.get("rsi_warned")  else 0,
            1 if pos.get("vol_warned")  else 0,
            1 if pos.get("exit_warned") else 0,
            pos.get("qty"),
        ))
        conn.commit()
    except Exception as e:
        log.error(f"[DB] save_position {chat_id}/{symbol}: {e}")
    finally:
        conn.close()


def db_remove_position(chat_id: str, symbol: str):
    conn = get_conn()
    if not conn:
        return
    try:
        conn.execute("DELETE FROM portfolio WHERE chat_id=? AND symbol=?",
                     (int(chat_id), symbol))
        conn.commit()
    except Exception as e:
        log.error(f"[DB] remove_position {chat_id}/{symbol}: {e}")
    finally:
        conn.close()


# ─── Trades (closed) ─────────────────────────────────────────────────────────

def db_log_trade(chat_id: str, symbol: str,
                 entry: float, exit_price: float = None, qty: int = None):
    conn = get_conn()
    if not conn:
        return
    try:
        pnl_d = pnl_p = result = None
        if exit_price is not None and entry:
            qty_val = qty or 0
            pnl_d   = round((exit_price - entry) * qty_val, 2) if qty_val else None
            pnl_p   = round((exit_price - entry) / entry * 100, 4)
            result  = "WIN" if pnl_p > 0 else "LOSS"
        conn.execute("""
            INSERT INTO trades
                (chat_id, symbol, entry_price, exit_price, qty,
                 pnl_dollar, pnl_pct, result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (int(chat_id), symbol, entry, exit_price, qty, pnl_d, pnl_p, result))
        conn.commit()
    except Exception as e:
        log.error(f"[DB] log_trade {chat_id}/{symbol}: {e}")
    finally:
        conn.close()


def db_update_last_trade(chat_id: str, symbol: str,
                         exit_price: float = None, entry: float = None,
                         qty: int = None) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, entry_price, exit_price, qty
            FROM   trades
            WHERE  chat_id=? AND symbol=?
            ORDER  BY closed_at DESC, id DESC
            LIMIT  1
        """, (int(chat_id), symbol))
        row = cur.fetchone()
        if not row:
            return False
        tid       = row["id"]
        new_entry = entry      if entry      is not None else (float(row["entry_price"]) if row["entry_price"] else 0.0)
        new_exit  = exit_price if exit_price is not None else (float(row["exit_price"])  if row["exit_price"]  else None)
        new_qty   = qty        if qty        is not None else (int(row["qty"])            if row["qty"]         else None)
        pnl_d = pnl_p = result = None
        if new_exit is not None and new_entry:
            qty_val = new_qty or 0
            pnl_d   = round((new_exit - new_entry) * qty_val, 2) if qty_val else None
            pnl_p   = round((new_exit - new_entry) / new_entry * 100, 4)
            result  = "WIN" if pnl_p > 0 else "LOSS"
        conn.execute("""
            UPDATE trades
            SET entry_price=?, exit_price=?, qty=?,
                pnl_dollar=?, pnl_pct=?, result=?
            WHERE id=?
        """, (new_entry, new_exit, new_qty, pnl_d, pnl_p, result, tid))
        conn.commit()
        return True
    except Exception as e:
        log.error(f"[DB] update_last_trade {chat_id}/{symbol}: {e}")
        return False
    finally:
        conn.close()


def db_get_trades(chat_id: str = None, days: int = 30) -> list:
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        if chat_id:
            cur.execute("""
                SELECT chat_id, symbol, entry_price, exit_price, qty,
                       pnl_dollar, pnl_pct, result, trade_date, closed_at
                FROM   trades
                WHERE  chat_id=?
                  AND  closed_at >= datetime('now', ? || ' days')
                ORDER  BY closed_at DESC
            """, (int(chat_id), f"-{days}"))
        else:
            cur.execute("""
                SELECT chat_id, symbol, entry_price, exit_price, qty,
                       pnl_dollar, pnl_pct, result, trade_date, closed_at
                FROM   trades
                WHERE  closed_at >= datetime('now', ? || ' days')
                ORDER  BY closed_at DESC
            """, (f"-{days}",))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"[DB] get_trades: {e}")
        return []
    finally:
        conn.close()


# ─── Scan log ─────────────────────────────────────────────────────────────────

def db_log_scan(symbol: str, price: float, change_pct: float,
                grade: str, passed: bool, skip_reason: str, session: str):
    conn = get_conn()
    if not conn:
        return
    try:
        conn.execute("""
            INSERT INTO scan_log (symbol, price, change_pct, grade, passed, skip_reason, session)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            float(price)      if price      is not None else None,
            float(change_pct) if change_pct is not None else None,
            grade or "",
            1 if passed else 0,
            skip_reason or "",
            session or "",
        ))
        conn.commit()
    except Exception as e:
        log.debug(f"[DB] log_scan {symbol}: {e}")
    finally:
        conn.close()


# ========================================================================
# SCANNER BOT
# ========================================================================

"""
Stock Scanner Bot v4 — Multi-User | Parallel Fetch
────────────────────────────────────────────────────
Sessions (US Eastern → KSA):
  Pre-Market  : 4:00 AM –  9:30 AM  →  11:00 AM –  4:30 PM KSA
  Market Open : 9:30 AM –  4:00 PM  →   4:30 PM – 11:00 PM KSA
  After-Hours : 4:00 PM –  8:00 PM  →  11:00 PM –  3:00 AM KSA

SETUP:  pip install requests beautifulsoup4 schedule pytz
"""

# ── Standard library ──────────────────────────────────────────
import sys
import os
import re
import json
import time
import uuid
import random
import platform
import logging
import threading
from datetime import datetime
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Third-party ───────────────────────────────────────────────
import requests
from bs4 import BeautifulSoup
import schedule
import pytz

# Windows UTF-8 fix
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ADMIN_ID       = "179463282"
USERS_FILE     = os.path.join(BASE_DIR, "bot_users.json")
PORTFOLIO_FILE = os.path.join(BASE_DIR, "portfolio.json")
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")
ALERTED_FILE   = os.path.join(BASE_DIR, "alerted.json")
TRACKED_FILE   = os.path.join(BASE_DIR, "tracked.json")
LOG_FILE       = os.path.join(BASE_DIR, "scanner.log")

MIN_PRICE       = 1.0
MAX_PRICE       = 20.0
SCAN_EVERY_MIN  = 2
ALERT_COOLDOWN  = 1800    # seconds before same stock can re-alert
SCAN_WORKERS    = 10      # stocks fetched in parallel
PORTFOLIO_SIZE  = 10_000  # default portfolio $ for position sizing
FLOAT_CACHE_TTL = 86_400  # float doesn't change daily — cache 24 h
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # add credits at console.anthropic.com then paste key

FILTERS = {
    "PRE":   {"min_change": 10.0, "min_volume": 10_000,  "min_dollar_vol": 300_000,   "max_float_m": 25.0, "max_rsi": 90.0, "max_mcap_m": 300.0},
    "OPEN":  {"min_change": 15.0, "min_volume": 500_000, "min_dollar_vol": 2_000_000, "max_float_m": 20.0, "max_rsi": 90.0, "max_mcap_m": 300.0},
    "AFTER": {"min_change": 10.0, "min_volume": 50_000,  "min_dollar_vol": 500_000,   "max_float_m": 20.0, "max_rsi": 85.0, "max_mcap_m": 300.0},
}

URLS = {
    "PRE":   "https://stockanalysis.com/markets/premarket/gainers/",
    "OPEN":  "https://stockanalysis.com/markets/gainers/",
    "AFTER": "https://stockanalysis.com/markets/afterhours/gainers/",
}

SESSION_LABELS = {
    "PRE":   "🌅 PRE-MARKET",
    "OPEN":  "📈 MARKET OPEN",
    "AFTER": "🌙 AFTER-HOURS",
}


# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("scanner")
logging.getLogger("httpx").setLevel(logging.WARNING)        # silence Anthropic SDK HTTP logs
logging.getLogger("anthropic").setLevel(logging.WARNING)


# ═══════════════════════════════════════════════════════════════
#  CATALYST KEYWORDS
# ═══════════════════════════════════════════════════════════════
STRONG_KEYWORDS = [
    # FDA / Regulatory
    "fda", "approved", "approval", "cleared", "510k", "de novo",
    "breakthrough designation", "breakthrough therapy", "fast track",
    "priority review", "accelerated approval", "orphan drug",
    "pdufa", "nda", "bla", "anda", "eua", "emergency use authorization",
    "ce mark", "ecac", "eu approval",
    # Clinical
    "clinical trial", "clinical data", "phase 1", "phase 2", "phase 3",
    "phase i", "phase ii", "phase iii", "positive data", "positive results",
    "efficacy", "statistically significant", "data readout", "milestone",
    "trial results", "study results", "interim data",
    # Biodefense / Outbreak
    "ebola", "mpox", "monkeypox", "hantavirus", "marburg", "outbreak",
    "biodefense", "antiviral", "vaccine", "treatment approved",
    "public health emergency", "health emergency", "cdc", "nih", "barda",
    "bioterrorism", "pandemic preparedness", "drc", "congo",
    # WHO / Global health
    "who", "world health organization", "emergency declaration",
    # Defense contracts
    "department of defense", "dod", "air force", "marine corps", "navy",
    "army", "pentagon", "darpa", "fema", "hhs", "veterans affairs",
    "sbir", "sttr", "afwerx", "nasa", "dhs", "homeland security",
    "contract awarded", "wins contract", "secures contract", "awarded contract",
    "purchase order", "task order", "sole source", "government contract",
    "federal contract", "defense contract", "military contract",
    # Grants
    "grant awarded", "grant funding", "receives grant", "non-dilutive",
    # Insider buying
    "insider buy", "ceo buys", "cfo buys", "coo buys", "director buys",
    "insider purchase", "open market purchase", "10b5-1",
    "executive buys", "officer buys", "chairman buys",
    # M&A
    "acquisition", "acquires", "buyout", "merger", "takeover",
    "going private", "strategic alternatives", "tender offer",
    "letter of intent", "loi", "definitive agreement",
    # Deals / Partnerships
    "partnership", "collaboration agreement", "license agreement",
    "licensing agreement", "joint venture", "strategic alliance",
    "memorandum of understanding", "mou", "distribution agreement",
    "exclusive agreement", "preferred supplier", "supply agreement",
    # Earnings
    "earnings beat", "beat estimates", "beat expectations",
    "record revenue", "record sales", "record earnings",
    "revenue growth", "profitable", "guidance raised", "raised outlook",
    "above consensus",
    # Analyst upgrades
    "analyst upgrade", "price target raised", "target raised",
    "initiated coverage", "buy rating", "strong buy", "outperform", "overweight",
    "bofa", "bank of america", "goldman sachs", "jpmorgan",
    "morgan stanley", "ubs", "citi", "raymond james", "needham",
    # Crypto treasury
    "bitcoin", "btc", "ethereum", "eth", "hyperliquid", "hype",
    "crypto holdings", "digital assets", "cryptocurrency reserve",
    "treasury reserve", "crypto treasury", "sui", "solana",
    # Solar / Clean energy
    "solar contract", "solar project", "power purchase agreement",
    "ppa", "renewable energy contract", "energy storage", "large scale solar",
    # AI / Tech
    "ai partnership", "gpu", "artificial intelligence contract",
    "google cloud", "microsoft azure", "aws contract",
    # Buyback / Dividend
    "share repurchase", "stock buyback", "buyback program",
    "special dividend", "dividend increase",
    # Compliance
    "regained compliance", "compliance achieved",
]

WEAK_KEYWORDS = [
    # No catalyst
    "no news", "no clear catalyst", "no new news", "no known catalyst",
    "retail momentum", "no reason", "unexplained surge", "sympathy play",
    # Dilution / offerings (most important P&D signal)
    "direct offering", "registered direct", "at-the-market", "atm offering",
    "shelf registration", "424b5", "424b3", "prospectus supplement",
    "underwritten offering", "follow-on offering", "secondary offering",
    "warrant exercise", "convertible note", "pipe offering", "dilutive",
    "priced offering", "million shares", "million share offering",
    "public offering", "concurrent offering", "best efforts offering",
    "nasdaq listing", "uplisting", "uplist to nasdaq",  # often dilution-heavy
    # Reverse splits (usually desperate companies)
    "reverse split", "reverse stock split", "1-for-", "10-for-1",
    "20-for-1", "15-for-1", "25-for-1", "30-for-1",
    # Management departures
    "ceo resign", "cfo resign", "coo resign", "stepping down",
    "ceo departs", "cfo departs", "chief executive resign",
    "interim ceo", "interim cfo", "replace ceo",
    # FDA bad news
    "fda rejection", "fda refused", "complete response letter", "crl",
    "warning letter", "clinical hold", "voluntary recall", "mandatory recall",
    "refuse to file", "not approvable", "fda concern",
    # Legal / regulatory trouble
    "class action", "lawsuit filed", "sec investigation", "sec charges",
    "going concern", "bankruptcy", "chapter 11", "chapter 7",
    "default notice", "debt default", "forbearance agreement",
    "restatement", "material weakness", "internal investigation",
    # Listing problems
    "nasdaq deficiency", "nyse deficiency", "minimum bid",
    "delisting notice", "delisting warning", "transfer to otc",
    "below listing standards", "regain compliance deadline",
    # Social media / meme pumps
    "reddit", "wallstreetbets", "wsb", "social media",
    "tiktok", "meme stock", "gamma squeeze", "retail buying frenzy",
    "short squeeze candidate", "trending on",
    # Vague corporate fluff (no real catalyst)
    "renames to", "rebrands", "name change", "ticker change",
    "corporate update", "conference presentation", "investor day",
    "strategic review", "exploring strategic alternatives",
    "letter of intent signed",  # LOI without definitive agreement = weak
    "non-binding", "memorandum of understanding signed",  # MOU alone = weak
    "pilot program", "proof of concept",
    "appoints new", "names new ceo", "board member appointed",
    # Crypto hype without substance
    "metaverse", "nft", "web3 pivot", "blockchain pivot",
    "pivots to ai", "rebrands as ai", "ai company now",
    # Technical / chart-based (no fundamental reason)
    "breakout", "technical breakout", "chart pattern",
    "52-week high", "all-time high", "momentum",
]


# ═══════════════════════════════════════════════════════════════
#  TIMEZONE & HTTP SESSION
# ═══════════════════════════════════════════════════════════════
EASTERN = pytz.timezone("US/Eastern")
KSA_TZ  = pytz.timezone("Asia/Riyadh")

# Sector peers — when one stock in a group alerts, scan the others
SECTOR_PEERS: dict[str, list[str]] = {
    "GOVX":  ["SNGX", "NNVC", "CODX", "BVNRY"],
    "SNGX":  ["GOVX", "NNVC", "CODX", "BVNRY"],
    "NNVC":  ["GOVX", "SNGX", "CODX", "BVNRY"],
    "CODX":  ["GOVX", "SNGX", "NNVC"],
    "VTIX":  ["MNTS", "RCAT", "KTOS"],
    "MNTS":  ["VTIX", "RCAT", "KTOS"],
    "EDIT":  ["CRSP", "NTLA", "BEAM"],
    "CRSP":  ["EDIT", "NTLA", "BEAM"],
    "LGHL":  ["MSTR", "SMLR", "BTBT"],
    "MSTR":  ["LGHL", "SMLR", "BTBT"],
    "VCIG":  ["NVDA", "SMCI", "AEVA"],
}

_http = requests.Session()
_http.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})

# Webull-specific session with browser-like headers to avoid blocking
_wb_http = requests.Session()
_wb_http.headers.update({
    "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "did":         uuid.uuid4().hex,          # random device ID per run
    "tz":          "America/New_York",
    "version":     "3.40.6",
    "platform":    "pc",
    "os":          "web",
    "osv":         "web",
    "Referer":     "https://app.webull.com/",
    "Origin":      "https://app.webull.com",
    "Accept":      "application/json",
})

# Max 3 simultaneous Webull requests to avoid rate-limiting
_wb_semaphore = threading.Semaphore(3)

# ── Claude AI ─────────────────────────────────────────────────
_anthropic_mod       = None    # lazy import
_claude_client       = None
_claude_cache: dict  = {}      # hash(news) → (score, label)
_claude_err_count    = 0       # consecutive API failures
_claude_pause_until  = 0.0     # circuit breaker: stop trying until this timestamp
_claude_active       = False   # True after first successful API call
CLAUDE_PAUSE_SECONDS = 300     # 5 min cooldown after 3 consecutive failures


def _get_claude():
    """
    Returns the Claude client, or None if:
      - no API key set
      - SDK not installed
      - circuit breaker open (too many recent failures)
    """
    global _anthropic_mod, _claude_client
    if time.time() < _claude_pause_until:
        return None  # circuit open — in cooldown
    if _claude_client is not None:
        return _claude_client
    if not ANTHROPIC_API_KEY:
        return None
    if _anthropic_mod is None:
        try:
            import anthropic as _mod
            _anthropic_mod = _mod
        except ImportError:
            log.warning("[Claude] anthropic not installed — run: pip install anthropic")
            return None
    try:
        _claude_client = _anthropic_mod.Anthropic(api_key=ANTHROPIC_API_KEY)
        log.info("[Claude] API ready  (model: claude-haiku-4-5)")
    except Exception as e:
        log.warning(f"[Claude] init failed: {e}")
    return _claude_client


def _claude_ok():
    """Call after a successful Claude API response."""
    global _claude_err_count, _claude_active
    _claude_err_count = 0
    _claude_active = True


def _claude_fail(context: str = ""):
    """Call after a failed Claude API response. Opens circuit after 3 errors."""
    global _claude_err_count, _claude_pause_until, _claude_active
    _claude_err_count += 1
    if _claude_err_count >= 3:
        _claude_pause_until = time.time() + CLAUDE_PAUSE_SECONDS
        _claude_err_count   = 0
        _claude_active      = False
        log.warning(f"[Claude] 3 consecutive errors ({context}) — pausing {CLAUDE_PAUSE_SECONDS//60} min, using keyword logic")


def test_claude() -> bool:
    """Test the Claude API key at startup. Returns True if working."""
    global _claude_active
    client = _get_claude()
    if not client:
        return False
    try:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            timeout=8.0,
            messages=[{"role": "user", "content": "reply: ok"}],
        )
        if r.content:
            _claude_ok()
            log.info("[Claude] API test passed ✓  (claude-haiku-4-5)")
            return True
    except Exception as e:
        _claude_fail("startup test")
        log.warning(f"[Claude] API test failed: {e}")
    return False


# ═══════════════════════════════════════════════════════════════
#  SHARED STATE
# ═══════════════════════════════════════════════════════════════
_lock           = threading.Lock()
alerted         = {}               # symbol → unix timestamp
watchlist_log   = deque(maxlen=50)
portfolio       = {}               # user_id → {symbol → position}
_last_removed   = {}               # uid → {sym, pos} — for UNDO
_last_upd_id    = [0]
users           = {}               # user_id (str) → {name, username, active, ...}
_float_cache    = {}               # symbol → (float_m, timestamp)
tracked_symbols = set()            # user-added symbols always scanned
_market_context = {"spy_chg": 0.0} # SPY % change updated each scan


# ═══════════════════════════════════════════════════════════════
#  USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def load_users():
    global users
    # Try SQL Server first, fall back to JSON
    if DB_OK:
        db_users = db_load_users()
        if db_users:
            users = db_users
            log.info(f"[DB] Users loaded from SQL Server: {len(users)}")
    if not users and os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                users = json.load(f)
        except Exception:
            users = {}
    if ADMIN_ID not in users:
        users[ADMIN_ID] = {
            "name": "Admin", "username": "A_adnan15",
            "active": True, "is_admin": True,
            "added": datetime.now().strftime("%Y-%m-%d"),
        }
        save_users()

def save_users():
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Save users error: {e}")
    if DB_OK:
        db_sync_users(users)

def load_alerted():
    global alerted
    # Try SQL Server first
    if DB_OK:
        db_alerted = db_load_alerted(ALERT_COOLDOWN)
        if db_alerted:
            alerted = db_alerted
            log.info(f"[DB] Alerted restored from SQL Server: {len(alerted)} symbol(s) in cooldown")
            return
    # Fall back to JSON file
    if os.path.exists(ALERTED_FILE):
        try:
            with open(ALERTED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cutoff = time.time() - 86_400
            alerted = {k: v for k, v in data.items() if v > cutoff}
            if alerted:
                log.info(f"Alerted restored: {len(alerted)} symbol(s) still in cooldown")
        except Exception:
            pass

def save_alerted():
    try:
        with _lock:
            snap = dict(alerted)
        with open(ALERTED_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Save alerted error: {e}")

def load_tracked():
    global tracked_symbols
    if os.path.exists(TRACKED_FILE):
        try:
            with open(TRACKED_FILE, "r", encoding="utf-8") as f:
                tracked_symbols = set(json.load(f))
            if tracked_symbols:
                log.info(f"Tracked symbols: {', '.join(sorted(tracked_symbols))}")
        except Exception:
            pass

def save_tracked():
    try:
        with open(TRACKED_FILE, "w", encoding="utf-8") as f:
            json.dump(list(tracked_symbols), f, ensure_ascii=False)
    except Exception as e:
        log.error(f"Save tracked error: {e}")

def load_watchlist():
    global watchlist_log
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            watchlist_log = deque(data, maxlen=50)
        except Exception:
            pass

def save_watchlist():
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(list(watchlist_log), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Save watchlist error: {e}")

def load_portfolio():
    global portfolio
    # Try SQL Server first
    if DB_OK:
        db_port = db_load_portfolio()
        if db_port:
            portfolio = db_port
            count = sum(len(p) for p in portfolio.values())
            log.info(f"[DB] Portfolio loaded from SQL Server: {count} position(s)")
            return
    # Fall back to JSON
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                portfolio = json.load(f)
            count = sum(len(p) for p in portfolio.values())
            if count:
                log.info(f"Portfolio restored: {count} position(s)")
        except Exception as e:
            log.error(f"Load portfolio error: {e}")
            portfolio = {}

def save_portfolio():
    try:
        with _lock:
            snap = {uid: dict(pos) for uid, pos in portfolio.items() if pos}
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        # Sync each position to SQL Server
        if DB_OK:
            for uid, positions in snap.items():
                for sym, pos in positions.items():
                    db_save_position(uid, sym, pos)
    except Exception as e:
        log.error(f"Save portfolio error: {e}")

def is_admin(uid: str) -> bool:
    return str(uid) == ADMIN_ID

def is_allowed(uid: str) -> bool:
    uid = str(uid)
    return uid in users and users[uid].get("active", False)

def add_user(uid: str, name: str = "", username: str = "") -> bool:
    uid = str(uid)
    if uid in users:
        users[uid]["active"] = True
        save_users()
        return False
    users[uid] = {
        "name": name or uid, "username": username,
        "active": True, "is_admin": False,
        "added": datetime.now().strftime("%Y-%m-%d"),
    }
    save_users()
    return True

def remove_user(uid: str) -> bool:
    uid = str(uid)
    if uid == ADMIN_ID:
        return False
    if uid in users:
        users[uid]["active"] = False
        save_users()
        return True
    return False

def get_active_users() -> list[str]:
    return [uid for uid, u in users.items() if u.get("active", False)]


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════

def send_to(uid: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": str(uid), "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.warning(f"send_to({uid}) failed: {e}")
        return False

def broadcast(text: str):
    for uid in get_active_users():
        send_to(uid, text)
        time.sleep(0.3)

def get_updates(offset: int) -> list:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10)
        if r.status_code == 200:
            return r.json().get("result", [])
    except Exception:
        pass
    return []


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def parse_num(s) -> float:
    s = str(s).strip().upper().replace(",", "").replace("$", "").replace("%", "").replace("+", "")
    if not s or s in ("-", "N/A", ""):
        return 0.0
    mult = 1
    if   s.endswith("B"): mult, s = 1_000_000_000, s[:-1]
    elif s.endswith("M"): mult, s = 1_000_000,     s[:-1]
    elif s.endswith("K"): mult, s = 1_000,          s[:-1]
    try:    return float(s) * mult
    except: return 0.0

def get_session() -> str | None:
    now = datetime.now(EASTERN)
    if now.weekday() >= 5:
        return None
    m = now.hour * 60 + now.minute
    if   4*60    <= m < 9*60+30: return "PRE"
    elif 9*60+30 <= m < 16*60:   return "OPEN"
    elif 16*60   <= m <= 20*60:  return "AFTER"
    return None


# ═══════════════════════════════════════════════════════════════
#  DATA FETCHING
# ═══════════════════════════════════════════════════════════════

def fetch_gainers(session: str) -> list[dict]:
    try:
        r    = _http.get(URLS[session], timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        tbl  = soup.find("table")
        if not tbl:
            return []
        stocks = []
        for row in tbl.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) < 5:
                continue
            try:
                sym    = cols[1].get_text(strip=True)
                change = float(cols[3].get_text(strip=True).replace("%", "").replace("+", ""))
                price  = float(cols[4].get_text(strip=True).replace("$", "").replace(",", ""))
                volume = parse_num(cols[5].get_text(strip=True)) if len(cols) > 5 else 0
                stocks.append({"symbol": sym, "change": change, "price": price, "volume": volume})
            except Exception:
                continue
        return stocks
    except Exception as e:
        log.error(f"Gainers fetch error: {e}")
        return []


def calc_rsi(closes: list, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g  = sum(gains[-period:])  / period
    avg_l  = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)


def calc_mfi(highs: list, lows: list, closes: list, vols: list,
             period: int = 14) -> float | None:
    """Money Flow Index — volume-weighted RSI on typical price."""
    n = min(len(highs), len(lows), len(closes), len(vols))
    if n < period + 1:
        return None
    tp   = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    pos_mf = neg_mf = 0.0
    for i in range(n - period, n):
        mf = tp[i] * vols[i]
        if tp[i] > tp[i - 1]:
            pos_mf += mf
        elif tp[i] < tp[i - 1]:
            neg_mf += mf
    if neg_mf == 0:
        return 100.0
    return round(100 - 100 / (1 + pos_mf / neg_mf), 1)


def calc_obv_trend(closes: list, vols: list) -> str:
    """
    Returns direction of OBV over available bars.
    '↑' = money flowing in, '↓' = flowing out, '→' = neutral.
    """
    n = min(len(closes), len(vols))
    if n < 6:
        return "→"
    obv = [0.0]
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + vols[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - vols[i])
        else:
            obv.append(obv[-1])
    # Compare first half vs second half average
    mid  = n // 2
    avg1 = sum(obv[:mid]) / mid
    avg2 = sum(obv[mid:]) / (n - mid)
    diff = (avg2 - avg1) / (abs(avg1) + 1)
    if diff > 0.05:
        return "↑"
    if diff < -0.05:
        return "↓"
    return "→"


def _fetch_yahoo_news(symbol: str) -> str | None:
    """
    Fetch latest news headline from Yahoo Finance search API.
    Aggregates Globe Newswire, SEC, Reuters, AP, and others.
    Returns title string or None.
    """
    try:
        r = _http.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={
                "q": symbol,
                "newsCount": 3,
                "enableFuzzyQuery": "false",
                "region": "US",
                "lang": "en-US",
            },
            headers={"Accept": "application/json"},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        items = r.json().get("news", [])
        if not items:
            return None
        # Pick the most recent article whose title mentions the ticker or
        # just return the first one (already sorted by recency)
        for item in items:
            title = item.get("title", "")
            if title:
                publisher = item.get("publisher", "")
                suffix = f" [{publisher}]" if publisher else ""
                return (title + suffix)[:120]
    except Exception:
        pass
    return None


_wb_id_cache: dict = {}

def fetch_webull_id(symbol: str) -> str | None:
    if symbol in _wb_id_cache:
        return _wb_id_cache[symbol]
    try:
        s = requests.Session()
        s.headers.update(_wb_http.headers)
        r = s.get(
            "https://quotes-gw.webullfintech.com/api/search/pc/tickers",
            params={"keyword": symbol, "pageIndex": 1, "pageSize": 5},
            timeout=8,
        )
        if r.status_code == 200:
            body = r.json()
            # Response can be a list directly OR {"data": {"items": [...]}}
            if isinstance(body, list):
                items = body
            elif isinstance(body, dict):
                d = body.get("data", {})
                items = d if isinstance(d, list) else d.get("items", [])
            else:
                items = []
            if not items:
                log.debug(f"  Webull ID not found for {symbol}")
                return None
            # Find exact symbol match first
            for item in items:
                if item.get("symbol", "").upper() == symbol.upper():
                    tid = str(item.get("tickerId", ""))
                    if tid:
                        _wb_id_cache[symbol] = tid
                        return tid
            # Fallback to first result
            tid = str(items[0].get("tickerId", ""))
            if tid:
                _wb_id_cache[symbol] = tid
                return tid
    except Exception as e:
        log.debug(f"  Webull ID error {symbol}: {e}")
    return None

def fetch_webull(symbol: str) -> dict | None:
    tid = fetch_webull_id(symbol)
    if not tid:
        return None

    with _wb_semaphore:  # max 3 concurrent Webull calls
        try:
            def sf(v):
                try: return float(v) if v else None
                except: return None

            def _wb_get(url, params):
                # Each call gets its own session — thread-safe
                s = requests.Session()
                s.headers.update(_wb_http.headers)
                time.sleep(random.uniform(0.05, 0.2))
                return s.get(url, params=params, timeout=8)

            def _get_quote():
                return _wb_get(
                    "https://quotes-gw.webullfintech.com/api/bgw/quote/realtime",
                    {"ids": tid, "includeSecu": 1, "delay": 0, "more": 1},
                )

            def _get_intraday():
                # 30 bars of m5 = 150 min of data → intraday RSI + VWAP
                return _wb_get(
                    "https://quotes-gw.webullfintech.com/api/quote/charts/query",
                    {"tickerIds": tid, "type": "m5", "count": 30},
                )

            def _get_candles_daily():
                # Daily candles — fallback RSI when intraday has <15 bars
                return _wb_get(
                    "https://quotes-gw.webullfintech.com/api/quote/charts/query",
                    {"tickerIds": tid, "type": "d1", "count": 60},
                )

            def _get_fundamentals():
                return _wb_get(
                    "https://quotes-gw.webullfintech.com/api/bgw/stock/stock-analysis/fundamental",
                    {"tickerId": tid},
                )

            def _get_news():
                r = _wb_get(
                    "https://quotes-gw.webullfintech.com/api/information/news/ticker",
                    {"tickerIds": tid, "pageSize": 3, "currentNewsId": 0},
                )
                if r.status_code == 200:
                    return r
                # Fallback to old endpoint
                return _wb_get(
                    "https://quotes-gw.webullfintech.com/api/bgw/news/list",
                    {"tickerId": tid, "pageSize": 3},
                )

            # Fetch all 5 in parallel
            with ThreadPoolExecutor(max_workers=5) as pool:
                qf  = pool.submit(_get_quote)
                inf = pool.submit(_get_intraday)
                df  = pool.submit(_get_candles_daily)
                ff  = pool.submit(_get_fundamentals)
                nf  = pool.submit(_get_news)
                qr  = qf.result()
                inr = inf.result()
                dr  = df.result()
                fr  = ff.result()
                nr  = nf.result()

            # ── Quote ─────────────────────────────────────────
            if qr.status_code != 200:
                return None
            raw = qr.json()
            q   = raw[0] if isinstance(raw, list) and raw else raw
            if not q:
                return None

            session = get_session()
            if session == "PRE":
                price = sf(q.get("pPrice")) or sf(q.get("close"))
                high  = sf(q.get("pHigh"))  or sf(q.get("high"))
                low   = sf(q.get("pLow"))   or sf(q.get("low"))
                vol   = sf(q.get("pVolume")) or sf(q.get("volume"))
            elif session == "AFTER":
                price    = sf(q.get("pPrice")) or sf(q.get("close"))
                ah_high  = sf(q.get("pHigh"))
                reg_high = sf(q.get("high"))
                high     = max(h for h in [ah_high, reg_high] if h) if (ah_high or reg_high) else None
                low      = sf(q.get("pLow"))   or sf(q.get("low"))
                vol      = sf(q.get("pVolume")) or sf(q.get("volume"))
            else:
                price   = sf(q.get("close"))
                high    = sf(q.get("high"))
                low     = sf(q.get("low"))
                vol     = sf(q.get("volume"))

            avg_vol = sf(q.get("avgVol10D") or q.get("avgVol3M") or q.get("avgVol"))
            rel_vol = round(vol / avg_vol, 2) if vol and avg_vol else None
            mcap_m  = sf(q.get("marketValue"))
            mcap_m  = round(mcap_m / 1e6, 2) if mcap_m else None

            # ── Intraday RSI + VWAP from m5 candles ──────────
            # m5 candles: "timestamp,open,close,high,low,preClose,volume,vwap"
            # index 2 = close, index 7 = vwap
            # Webull returns newest-first → reverse before RSI
            rsi      = None
            rsi_type = "intraday"
            vwap     = None

            def _parse_candle_bars(raw_resp):
                try:
                    body = raw_resp.json()
                    if isinstance(body, list) and body:
                        return body[0].get("data", []) if isinstance(body[0], dict) else []
                    return body.get("data", []) if isinstance(body, dict) else []
                except Exception:
                    return []

            def _extract_closes(bars):
                closes = []
                for c in bars:
                    if isinstance(c, str):
                        p = c.split(",")
                        if len(p) > 2:
                            try: closes.append(float(p[2]))
                            except: pass
                    elif isinstance(c, (list, tuple)) and len(c) > 2:
                        try: closes.append(float(c[2]))
                        except: pass
                return closes

            def _extract_ohlcv(bars):
                """Return (opens, highs, lows, closes, volumes) — oldest-first."""
                O, H, L, C, V = [], [], [], [], []
                for c in bars:
                    p = c.split(",") if isinstance(c, str) else c
                    if len(p) >= 7:
                        try:
                            O.append(float(p[1])); H.append(float(p[3]))
                            L.append(float(p[4])); C.append(float(p[2]))
                            V.append(float(p[6]))
                        except Exception:
                            pass
                O.reverse(); H.reverse(); L.reverse(); C.reverse(); V.reverse()
                return O, H, L, C, V

            mfi       = None
            obv_trend = "→"

            if inr.status_code == 200:
                intraday_bars = _parse_candle_bars(inr)
                if intraday_bars:
                    # VWAP from the newest bar (index 0)
                    try:
                        p0 = intraday_bars[0].split(",") if isinstance(intraday_bars[0], str) else []
                        if len(p0) > 7 and p0[7] not in ("null", "", "0"):
                            vwap = float(p0[7])
                    except Exception:
                        pass
                    # Intraday RSI — need 15+ bars for 14-period RSI
                    closes_m5 = _extract_closes(intraday_bars)
                    closes_m5.reverse()   # oldest-first for RSI
                    if len(closes_m5) >= 15:
                        rsi = calc_rsi(closes_m5)
                        rsi_type = "intraday"
                    # MFI + OBV from intraday OHLCV
                    _, H5, L5, C5, V5 = _extract_ohlcv(intraday_bars)
                    if len(C5) >= 15:
                        mfi       = calc_mfi(H5, L5, C5, V5)
                        obv_trend = calc_obv_trend(C5, V5)

            # Daily RSI fallback — when pre-market has <15 m5 bars
            if rsi is None and dr.status_code == 200:
                daily_bars = _parse_candle_bars(dr)
                closes_d1  = _extract_closes(daily_bars)
                closes_d1.reverse()
                if closes_d1:
                    rsi      = calc_rsi(closes_d1)
                    rsi_type = "daily"
                # MFI from daily candles when intraday had no data
                if mfi is None:
                    _, Hd, Ld, Cd, Vd = _extract_ohlcv(daily_bars)
                    if len(Cd) >= 15:
                        mfi       = calc_mfi(Hd, Ld, Cd, Vd)
                        obv_trend = calc_obv_trend(Cd, Vd)

            # ── Float ─────────────────────────────────────────
            float_m          = None
            float_estimated  = False
            raw_float = sf(q.get("floatShares"))
            if raw_float and 0 < raw_float < 500_000_000:
                float_m = round(raw_float / 1e6, 2)
            # Fallback: outstandingShares as proxy (capped at 500M)
            if not float_m:
                raw_out = sf(q.get("outstandingShares"))
                if raw_out and 0 < raw_out < 500_000_000:
                    float_m         = round(raw_out / 1e6, 2)
                    float_estimated = True

            # Also try fundamentals endpoint if quote didn't have float
            if not float_m and fr.status_code == 200:
                try:
                    fd = fr.json()
                    if isinstance(fd, list) and fd:
                        fd = fd[0]
                    raw_f2 = (
                        fd.get("floatShares") or
                        fd.get("sharesFloat") or
                        fd.get("float")
                    )
                    if raw_f2:
                        float_m = round(float(raw_f2) / 1e6, 2)
                except Exception:
                    pass

            # ── News ──────────────────────────────────────────
            news = None
            if nr.status_code == 200:
                try:
                    body = nr.json()
                    # New endpoint: {"data": [{...},...]} or list directly
                    if isinstance(body, list):
                        items = body
                    else:
                        items = body.get("data", [])
                    if isinstance(items, dict):
                        items = items.get("list", [])
                    if items:
                        n = items[0]
                        news = (n.get("title") or n.get("newsTitle") or
                                n.get("summary", ""))[:120]
                except Exception:
                    pass

            # ── Yahoo Finance news fallback ───────────────────
            if not news:
                news = _fetch_yahoo_news(symbol)

            if not price:
                return None

            # Cache float if found
            if float_m:
                _float_cache[symbol] = (float_m, time.time())

            change_pct = round(sf(q.get("changeRatio")) * 100, 2) if q.get("changeRatio") else None

            return {
                "price": price, "high": high, "low": low,
                "volume": vol, "rel_vol": rel_vol, "rsi": rsi,
                "mcap_m": mcap_m, "float_m": float_m, "news": news,
                "vwap": vwap, "rsi_type": rsi_type,
                "float_estimated": float_estimated,
                "change_pct": change_pct,
                "mfi": mfi, "obv_trend": obv_trend,
            }
        except Exception:
            return None


def fetch_yahoo(symbol: str) -> dict | None:
    try:
        # Daily candles for RSI (30 days → reliable 14-period RSI)
        r = _http.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "1d", "range": "1mo"},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        result = r.json()["chart"]["result"][0]
        meta   = result["meta"]
        closes = [c for c in result["indicators"]["quote"][0].get("close", []) if c is not None]

        session = get_session()
        if session == "PRE":
            price = meta.get("preMarketPrice")
        elif session == "AFTER":
            price = meta.get("postMarketPrice")
        else:
            price = meta.get("regularMarketPrice")

        return {
            "price": price,
            "high":  meta.get("regularMarketDayHigh"),
            "low":   meta.get("regularMarketDayLow"),
            "rsi":   calc_rsi(closes) if len(closes) >= 15 else None,
        }
    except Exception:
        return None


def fetch_yahoo_float(symbol: str) -> float | None:
    """Yahoo quoteSummary — float shares only (never shares outstanding)."""
    cached = _float_cache.get(symbol)
    if cached:
        v, ts = cached
        if time.time() - ts < FLOAT_CACHE_TTL:
            return v
    try:
        r = _http.get(
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
            params={"modules": "defaultKeyStatistics"},
            headers={"Accept": "application/json"},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        ks = r.json()["quoteSummary"]["result"][0]["defaultKeyStatistics"]
        raw = ks.get("floatShares", {}).get("raw")
        if not raw or float(raw) > 500_000_000:  # >500M = shares outstanding, not float
            return None
        result = round(float(raw) / 1e6, 2)
        _float_cache[symbol] = (result, time.time())
        return result
    except Exception:
        return None


def _fetch_float_fallback(symbol: str) -> float | None:
    try:
        r    = _http.get(f"https://stockanalysis.com/stocks/{symbol.lower()}/", timeout=10)
        text = BeautifulSoup(r.text, "html.parser").get_text()
        m    = re.search(r'Float[\s:]*([0-9,.]+)\s*([MKB])', text, re.IGNORECASE)
        if m:
            val  = float(m.group(1).replace(",", ""))
            unit = m.group(2).upper()
            return val * {"M": 1.0, "K": 0.001, "B": 1000.0}[unit]
    except Exception:
        pass
    return None


def fetch_finviz_raw(symbol: str) -> dict | None:
    """Finviz only — float, mcap, rel_vol, news, cached RSI."""
    try:
        r = _http.get(
            f"https://finviz.com/quote.ashx?t={symbol}&ty=c&ta=1&p=d",
            timeout=15,
        )
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        kv   = {}
        for tbl in soup.find_all("table"):
            cells = tbl.find_all("td")
            for i in range(0, len(cells) - 1, 2):
                kv[cells[i].get_text(strip=True)] = cells[i+1].get_text(strip=True)

        def kf(key, scale=1.0):
            n = parse_num(kv.get(key, ""))
            return (n / scale) if n else None

        rsi         = kf("RSI (14)") or kf("RSI")
        float_m     = kf("Shs Float", 1e6) or kf("Float", 1e6)
        mcap_m      = kf("Market Cap", 1e6)
        rel_vol     = kf("Rel Volume") or kf("Rel Vol")
        short_float = kf("Short Float") or kf("Short Float %")

        try:    high = float(kv.get("High", "").replace(",", ""))
        except: high = None
        try:    low  = float(kv.get("Low",  "").replace(",", ""))
        except: low  = None

        if not float_m:
            float_m = _fetch_float_fallback(symbol)
        if float_m:
            _float_cache[symbol] = (float_m, time.time())

        news = "No recent news"
        nt   = soup.find("table", id="news-table")
        if nt:
            row = nt.find("tr")
            if row:
                tds = row.find_all("td")
                if len(tds) >= 2:
                    news = tds[1].get_text(strip=True)[:120]

        return {"rsi": rsi, "float_m": float_m, "mcap_m": mcap_m,
                "rel_vol": rel_vol, "high": high, "low": low,
                "news": news, "rsi_source": "cached",
                "short_float": short_float}
    except Exception as e:
        log.warning(f"Finviz error {symbol}: {e}")
        return None


def fetch_stock_data(symbol: str) -> dict | None:
    """Webull + Finviz + Yahoo float all run in parallel."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        wb_fut  = pool.submit(fetch_webull, symbol)
        fv_fut  = pool.submit(fetch_finviz_raw, symbol)
        yf_fut  = pool.submit(fetch_yahoo_float, symbol)
        webull  = wb_fut.result()
        fv      = fv_fut.result()
        yf_float = yf_fut.result()

    if fv is None and webull is None:
        return None

    data = fv or {"rsi": None, "float_m": None, "mcap_m": None,
                  "rel_vol": None, "high": None, "low": None,
                  "news": "No recent news", "rsi_source": "cached"}

    if webull:
        if webull.get("price"):       data["price"]   = webull["price"]
        if webull.get("rsi"):
            data["rsi"]        = webull["rsi"]
            data["rsi_source"] = webull.get("rsi_type", "intraday")
        if webull.get("high"):        data["high"]    = webull["high"]
        if webull.get("low"):         data["low"]     = webull["low"]
        if webull.get("volume"):      data["volume"]  = webull["volume"]
        # Finviz RelVol = intraday (vs same time of day) — more useful for scalping
        # Only use Webull's 10-day-avg RelVol if Finviz didn't provide one
        if webull.get("rel_vol") and not data.get("rel_vol"):
            data["rel_vol"] = webull["rel_vol"]
        if webull.get("mcap_m"):      data["mcap_m"]  = webull["mcap_m"]
        if webull.get("float_m"):     data["float_m"] = webull["float_m"]
        if webull.get("news"):        data["news"]    = webull["news"]
        if webull.get("vwap"):        data["vwap"]    = webull["vwap"]

    # Yahoo float fills gap when Webull fundamentals (417) and Finviz both fail
    if not data.get("float_m") and yf_float:
        data["float_m"] = yf_float

    # Yahoo fallback — price + RSI if Webull failed
    if data.get("rsi_source") == "cached" or not data.get("price"):
        yahoo = fetch_yahoo(symbol)
        if yahoo:
            if not data.get("price") and yahoo.get("price"):
                data["price"] = yahoo["price"]
            if yahoo.get("rsi"):
                data["rsi"]        = yahoo["rsi"]
                data["rsi_source"] = "live"
            if yahoo.get("high"): data["high"] = yahoo["high"]
            if yahoo.get("low"):  data["low"]  = yahoo["low"]

    return data


def fetch_spy_change() -> float:
    """Returns SPY % change for the day. 0.0 on failure."""
    try:
        r = _http.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/SPY",
            params={"interval": "1d", "range": "2d"},
            headers={"Accept": "application/json"},
            timeout=8,
        )
        if r.status_code != 200:
            return 0.0
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        prev  = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price and prev and prev != 0:
            return round((price - prev) / prev * 100, 2)
    except Exception:
        pass
    return 0.0

def fetch_price_live(symbol: str) -> float | None:
    """Live price for portfolio tracking. Webull first, Finviz fallback."""
    wb = fetch_webull(symbol)
    if wb and wb.get("price"):
        return wb["price"]
    try:
        r    = _http.get(f"https://finviz.com/quote.ashx?t={symbol}", timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        kv   = {}
        for tbl in soup.find_all("table"):
            cells = tbl.find_all("td")
            for i in range(0, len(cells) - 1, 2):
                kv[cells[i].get_text(strip=True)] = cells[i+1].get_text(strip=True)
        v = kv.get("Price") or kv.get("Last")
        if v:
            return float(v.replace(",", ""))
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
#  SCORING & GRADING
# ═══════════════════════════════════════════════════════════════

def score_catalyst(news: str) -> tuple:
    """Score news as a catalyst. Uses Claude if API key set, falls back to keywords."""
    if not news or news in ("—", "No recent news", ""):
        return 0, "❌ No catalyst"

    # ── Claude scoring (when API available) ──────────────────
    client = _get_claude()
    if client:
        ck = hash(news[:200])
        if ck in _claude_cache:
            return _claude_cache[ck]
        try:
            r = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                timeout=6.0,
                messages=[{
                    "role": "user",
                    "content": (
                        "Score this stock news headline as a trading catalyst.\n"
                        f"Headline: {news[:200]}\n\n"
                        "Reply with exactly one line:\n"
                        "SCORE: <0|1|2>  LABEL: <5 words max>\n\n"
                        "2 = strong real catalyst (FDA approval, government contract, "
                        "acquisition, earnings beat, clinical trial win)\n"
                        "1 = neutral or unverified (vague announcement, conference, "
                        "strategic review, MOU with no dollar value)\n"
                        "0 = negative or pump signal (stock offering/dilution, "
                        "executive resignation, FDA rejection, reverse split, "
                        "no news, social media hype)"
                    ),
                }],
            )
            text = r.content[0].text.strip()
            ms = re.search(r"SCORE:\s*([012])", text)
            ml = re.search(r"LABEL:\s*(.+)", text)
            if ms and ml:
                sc     = int(ms.group(1))
                lbl    = ml.group(1).strip()[:35]
                icons  = {2: "🔥", 1: "⚠️", 0: "❌"}
                result = (sc, f"{icons[sc]} {lbl}")
                _claude_cache[ck] = result
                _claude_ok()
                log.debug(f"[Claude] catalyst {sc}: {news[:55]}")
                return result
            _claude_fail("bad parse")
        except Exception as e:
            _claude_fail("score_catalyst")
            log.debug(f"[Claude] score_catalyst: {e}")

    # ── Keyword fallback (always works, no API needed) ────────
    nl = news.lower()
    if any(kw in nl for kw in STRONG_KEYWORDS):
        return 2, "🔥 Strong catalyst"
    if any(kw in nl for kw in WEAK_KEYWORDS):
        return 0, "❌ No / weak catalyst"
    return 1, "⚠️ Neutral / unverified"


def claude_pnd_check(sym: str, stock: dict, fv: dict) -> tuple:
    """
    Final AI gate — called only when stock passes all numeric filters.
    Returns (is_legit: bool, reason: str).
    Defaults to True (allow) if API unavailable or times out.
    """
    client = _get_claude()
    if not client:
        return True, ""

    price  = fv.get("price") or stock.get("price", 0)
    change = stock.get("change", 0)
    rsi    = fv.get("rsi")
    flt    = fv.get("float_m")
    rv     = fv.get("rel_vol")
    news   = fv.get("news", "No news")
    h, l   = fv.get("high"), fv.get("low")

    pos_str = "unknown"
    if h and l and h != l:
        pos     = (price - l) / (h - l)
        pos_str = f"{pos:.0%} of day range"

    prompt = (
        f"You are a momentum trading risk filter for small-cap US stocks.\n"
        f"Ticker: {sym}\n"
        f"Price: ${price:.2f}  Move today: {change:+.1f}%\n"
        f"RSI: {f'{rsi:.0f}' if rsi else 'unknown'}\n"
        f"Float: {f'{flt:.1f}M' if flt else 'unknown'}\n"
        f"Relative Volume: {f'{rv:.1f}x' if rv else 'unknown'}\n"
        f"Day range position: {pos_str}\n"
        f"Latest news: {news[:150]}\n\n"
        "Is this a legitimate momentum trade or a pump-and-dump?\n"
        "Reply with exactly one line:\n"
        "VERDICT: <LEGIT or PUMP>  REASON: <reason in 8 words max>"
    )

    try:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            timeout=6.0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = r.content[0].text.strip()
        m = re.search(r"VERDICT:\s*(LEGIT|PUMP)\s+REASON:\s*(.+)", text, re.IGNORECASE)
        if m:
            verdict = m.group(1).upper()
            reason  = m.group(2).strip()[:60]
            _claude_ok()
            log.info(f"  [Claude] {sym}: {verdict} — {reason}")
            if verdict == "PUMP":
                return False, f"AI: {reason}"
            return True, reason
        _claude_fail("bad parse")
    except Exception as e:
        _claude_fail("pnd_check")
        log.debug(f"[Claude] pnd_check {sym}: {e}")

    return True, ""  # default allow on any error — keyword rules already ran

def calc_targets(price: float, fv: dict) -> dict:
    """
    Dynamic targets based on RSI + Float + Catalyst + Day range position.

    Tier 1 — Explosive  : RSI<50, Float<10M, strong catalyst, near day low
    Tier 2 — Normal     : default
    Tier 3 — Tired      : RSI>65, near day high, weak/no catalyst
    """
    rsi      = fv.get("rsi")
    flt      = fv.get("float_m")
    h        = fv.get("high")
    l        = fv.get("low")
    cat_pts  = score_catalyst(fv.get("news", ""))[0]

    score = 0
    if rsi:
        score += 3 if rsi < 45 else (2 if rsi < 55 else (1 if rsi < 65 else 0))
    if flt:
        score += 3 if flt < 5  else (2 if flt < 10  else (1 if flt < 20  else 0))
    score += cat_pts  # 0, 1, or 2
    if h and l and h != l:
        pos    = (price - l) / (h - l)
        score += 2 if pos < 0.35 else (1 if pos < 0.65 else 0)

    # score 0-10
    if score >= 8:
        t1_pct, t2_pct, stop_pct = 0.22, 0.45, 0.09
        label = "🔥 Explosive setup"
    elif score >= 5:
        t1_pct, t2_pct, stop_pct = 0.15, 0.30, 0.09
        label = "✅ Normal setup"
    elif score >= 3:
        t1_pct, t2_pct, stop_pct = 0.10, 0.18, 0.08
        label = "⚠️ Tired setup"
    else:
        t1_pct, t2_pct, stop_pct = 0.07, 0.13, 0.07
        label = "🔴 Weak setup"

    return {
        "t1":      round(price * (1 + t1_pct),  2),
        "t2":      round(price * (1 + t2_pct),  2),
        "stop":    round(price * (1 - stop_pct), 2),
        "t1_pct":  int(t1_pct  * 100),
        "t2_pct":  int(t2_pct  * 100),
        "stop_pct":int(stop_pct * 100),
        "label":   label,
        "score":   score,
    }


def compute_grade(stock: dict, fv: dict) -> str:
    pts = 0
    rsi = fv.get("rsi")
    flt = fv.get("float_m")
    rv  = fv.get("rel_vol")
    vol = fv.get("volume") or stock.get("volume", 0)
    h   = fv.get("high")
    l   = fv.get("low")
    p   = fv.get("price") or stock["price"]

    if rsi: pts += 2 if rsi < 50 else (1 if rsi < 65 else 0)
    if flt: pts += 2 if flt < 5  else (1 if flt < 15  else 0)

    # Micro-float (< 2M): use float turnover — RelVol is misleading at this size
    # Normal float: use RelVol
    if flt and flt < 2.0 and vol:
        turnover = vol / (flt * 1_000_000)  # % of float traded today
        pts += 2 if turnover > 0.20 else (1 if turnover > 0.10 else 0)
    elif rv:
        pts += 2 if rv > 20 else (1 if rv > 5 else 0)

    pts += score_catalyst(fv.get("news", ""))[0]
    if h and l and h != l:
        pos  = (p - l) / (h - l)
        pts += 2 if pos < 0.35 else (1 if pos < 0.70 else 0)
    return "A" if pts >= 8 else ("B" if pts >= 5 else "C")


# ═══════════════════════════════════════════════════════════════
#  ALERT MESSAGE
# ═══════════════════════════════════════════════════════════════

def range_bar(price, high, low) -> str:
    if not high or not low or high == low:
        return "N/A"
    pos   = (price - low) / (high - low) * 100
    label = "🟢 Low (good entry)" if pos < 35 else ("🟡 Mid" if pos < 70 else "🔴 High — wait for dip")
    return f"{pos:.0f}%  {label}"

def build_alert_simple(stock: dict, fv: dict, session: str) -> str:
    """Short alert for auto-scan broadcasts — entry & stop, no indicators."""
    sym    = stock["symbol"]
    price  = fv.get("price") or stock["price"]
    change = stock["change"]
    news   = fv.get("news", "")
    grade  = compute_grade(stock, fv)
    grade_icon   = {"A": "🅰️", "B": "🅱️", "C": "⚠️"}[grade]
    _, cat_label = score_catalyst(news)
    tgt          = calc_targets(price, fv)
    entry_lo     = round(price * 0.99, 2)
    entry_hi     = round(price * 1.01, 2)
    D = "━━━━━━━━━━━━━━━━━━━━"
    news_line = f"<i>{news[:120]}</i>\n" if news and news != "—" else ""
    return (
        f"{grade_icon} <b>{sym}</b>   ${price:.2f}   {change:+.1f}%\n"
        f"Entry    ${entry_lo} – ${entry_hi}\n"
        f"Stop     ${tgt['stop']}   -{tgt['stop_pct']}%\n"
        f"{tgt['label']}\n"
        f"📰 {cat_label}\n"
        f"{news_line}"
        f"{D}\n"
        f"💬 <code>/check {sym}</code> for full analysis"
    )


def build_alert(stock: dict, fv: dict, session: str) -> str:
    """Full alert (used by /check) — same clean format as broadcast."""
    sym    = stock["symbol"]
    price  = fv.get("price") or stock["price"]
    change = stock["change"]
    news   = fv.get("news", "")

    grade      = compute_grade(stock, fv)
    grade_icon = {"A": "🅰️", "B": "🅱️", "C": "⚠️"}[grade]
    _, cat_label = score_catalyst(news)
    tgt          = calc_targets(price, fv)
    entry_lo     = round(price * 0.99, 2)
    entry_hi     = round(price * 1.01, 2)
    news_line    = f"<i>{news[:120]}</i>\n" if news and news != "—" else ""
    D = "━━━━━━━━━━━━━━━━━━━━"

    return (
        f"{grade_icon} <b>{sym}</b>   ${price:.2f}   {change:+.1f}%\n"
        f"Entry    ${entry_lo} – ${entry_hi}\n"
        f"Stop     ${tgt['stop']}   -{tgt['stop_pct']}%\n"
        f"T1       ${tgt['t1']}   +{tgt['t1_pct']}%  → sell 50%\n"
        f"T2       ${tgt['t2']}   +{tgt['t2_pct']}%  → sell all\n"
        f"{tgt['label']}\n"
        f"📰 {cat_label}\n"
        f"{news_line}"
        f"{D}\n"
        f"💬 <code>BUY {sym} {price:.2f}</code>"
    )


# ═══════════════════════════════════════════════════════════════
#  PORTFOLIO TRACKER
# ═══════════════════════════════════════════════════════════════

def add_position(uid: str, sym: str, entry: float, qty: int = None):
    # Save position immediately with basic targets, then refine with live data
    tgt = calc_targets(entry, {})
    pos = {
        "entry":       entry,
        "stop":        tgt["stop"],
        "t1":          tgt["t1"],
        "t2":          tgt["t2"],
        "t1_hit":      False,
        "t2_hit":      False,
        "rsi_warned":  False,
        "vol_warned":  False,
        "exit_warned": False,
        "qty":         qty,
    }
    with _lock:
        portfolio.setdefault(uid, {})[sym] = pos
    save_portfolio()

    # Fetch live data then send one single message
    def _send():
        try:
            fv   = fetch_stock_data(sym) or {}
            tgt2 = calc_targets(entry, fv) if fv else tgt
            with _lock:
                if uid in portfolio and sym in portfolio[uid]:
                    portfolio[uid][sym].update({
                        "stop": tgt2["stop"],
                        "t1":   tgt2["t1"],
                        "t2":   tgt2["t2"],
                    })
            save_portfolio()
            stop_dist = entry - tgt2["stop"]
            size_line = ""
            if qty:
                invested  = entry * qty
                size_line = f"Size     {qty} shares  (${invested:,.0f})\n"
            elif stop_dist > 0:
                shares    = max(1, int(PORTFOLIO_SIZE * 0.02 / stop_dist))
                size_line = f"Size     {shares} shares  (${shares * entry:,.0f})  — 2% risk\n"
            send_to(uid,
                f"📌 <b>Tracking {sym}</b>\n"
                f"Entry  : ${entry:.2f}\n"
                f"Stop   : ${tgt2['stop']}  (-{tgt2['stop_pct']}%)\n"
                f"T1     : ${tgt2['t1']}  (+{tgt2['t1_pct']}%)  → sell 50%\n"
                f"T2     : ${tgt2['t2']}  (+{tgt2['t2_pct']}%)  → sell rest\n"
                f"{size_line}"
                f"{tgt2['label']}\n\n"
                f"I will alert you when any level is hit."
            )
        except Exception:
            send_to(uid, f"📌 Tracking {sym} at ${entry:.2f}")
    threading.Thread(target=_send, daemon=True).start()

def check_portfolio():
    with _lock:
        snapshot = {uid: dict(pos) for uid, pos in portfolio.items()}

    for uid, positions in snapshot.items():
        for sym, pos in positions.items():
            # One Webull call gives price + RSI + RelVol together
            wb      = fetch_webull(sym)
            price   = (wb.get("price") if wb else None) or fetch_price_live(sym)
            if price is None:
                continue

            rsi     = wb.get("rsi")     if wb else None
            rel_vol = wb.get("rel_vol") if wb else None
            entry   = pos["entry"]
            pct     = (price - entry) / entry * 100

            # ── Price levels ──────────────────────────────────
            if price <= pos["stop"]:
                send_to(uid,
                    f"🛑 <b>STOP HIT — {sym}</b>\n"
                    f"Price  : ${price:.2f}\n"
                    f"Entry  : ${entry:.2f}\n"
                    f"Loss   : {pct:+.1f}%\n\n<b>EXIT NOW — sell all</b>"
                )
                with _lock:
                    portfolio.get(uid, {}).pop(sym, None)
                save_portfolio()
                time.sleep(0.3)
                continue

            elif not pos["t1_hit"] and price >= pos["t1"]:
                send_to(uid,
                    f"🥇 <b>TARGET 1 HIT — {sym}</b>\n"
                    f"Price  : ${price:.2f}\n"
                    f"Entry  : ${entry:.2f}\n"
                    f"Profit : {pct:+.1f}%\n\n"
                    f"→ Sell 50%\n→ Move stop to ${entry:.2f} (breakeven)"
                )
                with _lock:
                    if uid in portfolio and sym in portfolio[uid]:
                        portfolio[uid][sym]["t1_hit"] = True
                        portfolio[uid][sym]["stop"]   = entry
                save_portfolio()

            elif pos["t1_hit"] and not pos["t2_hit"] and price >= pos["t2"]:
                send_to(uid,
                    f"🥈 <b>TARGET 2 HIT — {sym}</b>\n"
                    f"Price  : ${price:.2f}\n"
                    f"Entry  : ${entry:.2f}\n"
                    f"Profit : {pct:+.1f}%\n\n"
                    f"→ Sell rest — <b>FULL EXIT</b>"
                )
                with _lock:
                    portfolio.get(uid, {}).pop(sym, None)
                save_portfolio()
                time.sleep(0.3)
                continue

            # ── RSI / Volume exit signals ─────────────────────
            rsi_danger = rsi is not None and rsi > 75
            vol_danger = rel_vol is not None and rel_vol < 2.0

            if rsi_danger and vol_danger and not pos.get("exit_warned"):
                send_to(uid,
                    f"🚨 <b>EXIT SIGNAL — {sym}</b>\n"
                    f"Price   : ${price:.2f}  ({pct:+.1f}%)\n"
                    f"RSI     : {rsi:.1f}  🔴 Overbought\n"
                    f"RelVol  : {rel_vol:.1f}x  🔴 Volume dying\n\n"
                    f"<b>Consider exiting now</b>"
                )
                with _lock:
                    if uid in portfolio and sym in portfolio[uid]:
                        portfolio[uid][sym]["exit_warned"] = True

            elif rsi_danger and not vol_danger and not pos.get("rsi_warned"):
                send_to(uid,
                    f"⚠️ <b>RSI WARNING — {sym}</b>\n"
                    f"Price  : ${price:.2f}  ({pct:+.1f}%)\n"
                    f"RSI    : {rsi:.1f}  🔴 Overbought (>75)\n\n"
                    f"Momentum fading — consider partial exit"
                )
                with _lock:
                    if uid in portfolio and sym in portfolio[uid]:
                        portfolio[uid][sym]["rsi_warned"] = True

            elif vol_danger and not rsi_danger and not pos.get("vol_warned"):
                send_to(uid,
                    f"⚠️ <b>VOLUME WARNING — {sym}</b>\n"
                    f"Price   : ${price:.2f}  ({pct:+.1f}%)\n"
                    f"RelVol  : {rel_vol:.1f}x  🔴 Volume drying up\n\n"
                    f"Low volume = weak momentum — watch closely"
                )
                with _lock:
                    if uid in portfolio and sym in portfolio[uid]:
                        portfolio[uid][sym]["vol_warned"] = True

            # Reset warnings if conditions improve
            elif not rsi_danger and not vol_danger:
                with _lock:
                    if uid in portfolio and sym in portfolio[uid]:
                        portfolio[uid][sym]["rsi_warned"]  = False
                        portfolio[uid][sym]["vol_warned"]   = False
                        portfolio[uid][sym]["exit_warned"]  = False

            time.sleep(0.3)


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM COMMANDS
# ═══════════════════════════════════════════════════════════════

def handle_command(uid: str, text: str, sender_name: str = "", sender_username: str = ""):
    uid = str(uid)
    cmd = text.strip().lower()

    if not is_allowed(uid) and not is_admin(uid):
        send_to(ADMIN_ID,
            f"👤 <b>New user wants access</b>\n"
            f"Name     : {sender_name}\n"
            f"Username : @{sender_username}\n"
            f"ID       : <code>{uid}</code>\n\n"
            f"To approve: /adduser {uid}"
        )
        send_to(uid, "⏳ Access request sent to admin.\nYou will be notified once approved.")
        return

    if cmd.startswith("/adduser"):
        if not is_admin(uid):
            send_to(uid, "❌ Admin only command.")
            return
        parts = text.split()
        if len(parts) < 2:
            send_to(uid, "Usage: /adduser 123456789")
            return
        target = parts[1].strip()
        if add_user(target):
            if DB_OK:
                db_set_pin(target, "1234")
            send_to(uid,    f"✅ User {target} added.")
            send_to(target,
                "✅ <b>Access granted!</b>\n\n"
                "Send /help to see available commands.\n\n"
                "📊 <b>Dashboard access:</b>\n"
                "URL : http://localhost:8050\n"
                "User: your Telegram name\n"
                "PIN : 1234  (change with /setpin XXXX)"
            )
        else:
            send_to(uid, f"✅ User {target} re-activated.")

    elif cmd.startswith("/removeuser"):
        if not is_admin(uid):
            send_to(uid, "❌ Admin only command.")
            return
        parts = text.split()
        if len(parts) < 2:
            send_to(uid, "Usage: /removeuser 123456789")
            return
        target = parts[1].strip()
        if remove_user(target):
            send_to(uid,    f"✅ User {target} removed.")
            send_to(target, "❌ Your access has been removed.")
        else:
            send_to(uid, f"❌ User {target} not found or is admin.")

    elif cmd == "/users":
        if not is_admin(uid):
            send_to(uid, "❌ Admin only command.")
            return
        active   = sorted(
            [(i, u) for i, u in users.items() if u.get("active")],
            key=lambda x: (0 if x[1].get("is_admin") else 1, x[1].get("added", ""))
        )
        inactive = [(i, u) for i, u in users.items() if not u.get("active")]
        lines    = [f"👥 <b>Users ({len(active)} active)</b>\n"]
        for n, (i, u) in enumerate(active, 1):
            tag   = " 👑" if u.get("is_admin") else ""
            uname = f"@{u['username']}" if u.get("username") else ""
            lines.append(f"{n}. {u['name']} {uname} — <code>{i}</code>{tag}  (added {u.get('added','')})")
        if inactive:
            lines.append(f"\n🚫 Inactive ({len(inactive)})")
            for i, u in inactive:
                lines.append(f"• {u['name']} — <code>{i}</code>")
        send_to(uid, "\n".join(lines))

    elif cmd.startswith("/track"):
        parts = text.strip().split()
        if len(parts) < 2:
            with _lock:
                t = sorted(tracked_symbols)
            send_to(uid, f"📌 Tracked symbols: {', '.join(t) if t else 'none'}\nAdd: /track NNVC\nRemove: /untrack NNVC")
            return
        sym = parts[1].upper()
        with _lock:
            tracked_symbols.add(sym)
        save_tracked()
        send_to(uid, f"📌 <b>{sym}</b> added to tracked list — will be scanned every cycle.")

    elif cmd.startswith("/untrack"):
        parts = text.strip().split()
        if len(parts) < 2:
            send_to(uid, "Usage: /untrack NNVC")
            return
        sym = parts[1].upper()
        with _lock:
            tracked_symbols.discard(sym)
        save_tracked()
        send_to(uid, f"✅ <b>{sym}</b> removed from tracked list.")

    elif cmd.startswith("/check"):
        parts = text.strip().split()
        if len(parts) < 2:
            send_to(uid, "Usage: /check NNVC")
            return
        sym = parts[1].upper()
        send_to(uid, f"🔍 Fetching {sym}...")
        def _do_check():
            fv = fetch_stock_data(sym)
            if not fv or not fv.get("price"):
                send_to(uid, f"❌ Could not fetch data for {sym}. Check the symbol.")
                return
            session = get_session() or "OPEN"
            change  = fv.get("change_pct") or 0.0
            stock   = {"symbol": sym, "change": change, "price": fv["price"], "volume": fv.get("volume", 0)}
            send_to(uid, build_alert(stock, fv, session))
        threading.Thread(target=_do_check, daemon=True).start()

    elif cmd == "/scan":
        send_to(uid, "🔄 Scanning now...")
        run_scan(requester_id=uid)

    elif cmd == "/status":
        session  = get_session()
        ksa_time = datetime.now(KSA_TZ).strftime("%I:%M %p KSA")
        edt_time = datetime.now(EASTERN).strftime("%I:%M %p EDT")
        f        = FILTERS.get(session or "OPEN")
        with _lock:
            n_pos   = len(portfolio.get(uid, {}))
            n_alert = len(alerted)
            n_users = len(get_active_users())
        send_to(uid,
            f"📊 <b>Bot Status</b>\n\n"
            f"Session  : {SESSION_LABELS.get(session, '🔴 Market Closed')}\n"
            f"Time     : {edt_time}  |  {ksa_time}\n\n"
            f"Filters:\n"
            f"  Price   : ${MIN_PRICE}–${MAX_PRICE}\n"
            f"  Change  : +{f['min_change']}%+\n"
            f"  Volume  : {f['min_volume']:,}+\n"
            f"  Float   : &lt;{f['max_float_m']}M\n"
            f"  RSI     : &lt;{f['max_rsi']} (parabolic block)\n\n"
            f"Your positions  : {n_pos}\n"
            f"Stocks alerted  : {n_alert}\n"
            f"Active users    : {n_users}"
        )

    elif cmd == "/watchlist":
        with _lock:
            recent = list(watchlist_log)
        if not recent:
            send_to(uid, "📋 No stocks alerted yet this session.")
        else:
            lines = ["📋 <b>Recent Alerts</b>\n"]
            for item in reversed(recent[-10:]):
                lines.append(
                    f"• <b>{item['sym']}</b>  ${item['price']:.2f}  "
                    f"({item['change']:+.1f}%)  Grade:{item['grade']}  {item['time']}"
                )
            send_to(uid, "\n".join(lines))

    elif cmd == "/portfolio":
        with _lock:
            pos = dict(portfolio.get(uid, {}))
        if not pos:
            send_to(uid, "📂 You have no open tracked positions.")
        else:
            send_to(uid, "📂 Fetching live prices...")
            def _pf():
                lines = ["📂 <b>Your Positions</b>\n"]
                for sym, p in pos.items():
                    wb    = fetch_webull(sym)
                    price = (wb.get("price") if wb else None) or fetch_price_live(sym)
                    entry = p["entry"]
                    if price:
                        pct  = (price - entry) / entry * 100
                        icon = "🟢" if pct >= 0 else "🔴"
                        pl   = f"  {icon} {pct:+.1f}%  now ${price:.2f}"
                    else:
                        pl = ""
                    t1_tag = "✅" if p.get("t1_hit") else "○"
                    lines.append(
                        f"<b>{sym}</b>  entry:${entry:.2f}{pl}\n"
                        f"  Stop:${p['stop']:.2f}  T1:{t1_tag}${p['t1']:.2f}  T2:${p['t2']:.2f}"
                    )
                send_to(uid, "\n\n".join(lines))
            threading.Thread(target=_pf, daemon=True).start()

    elif cmd.startswith("buy "):
        parts = text.upper().split()
        if len(parts) >= 3:
            try:
                sym_b = parts[1]
                entry = float(parts[2])
                qty   = int(parts[3]) if len(parts) >= 4 else None
                add_position(uid, sym_b, entry, qty)
            except ValueError:
                send_to(uid, "❌ Format: BUY SYMBOL PRICE [SHARES]\nExamples:\n  BUY NNVC 1.75\n  BUY MASK 4.80 50")
        else:
            send_to(uid, "❌ Format: BUY SYMBOL PRICE [SHARES]\nExamples:\n  BUY NNVC 1.75\n  BUY MASK 4.80 50")

    elif cmd.startswith("add "):
        # ADD AUUD 1.75 100  → average into existing position
        parts = text.upper().split()
        if len(parts) < 3:
            send_to(uid, "❌ Format: ADD SYMBOL PRICE [SHARES]\nExample: ADD AUUD 1.75 100")
        else:
            try:
                sym_a     = parts[1]
                new_price = float(parts[2])
                new_qty   = int(parts[3]) if len(parts) >= 4 else None
                with _lock:
                    existing = portfolio.get(uid, {}).get(sym_a)
                if not existing:
                    send_to(uid, f"❌ {sym_a} not in portfolio. Use <code>BUY {sym_a} {new_price:.2f}</code> to start tracking.")
                else:
                    old_entry = existing["entry"]
                    old_qty   = existing.get("qty")
                    if new_qty and old_qty:
                        avg       = round((old_entry * old_qty + new_price * new_qty) / (old_qty + new_qty), 4)
                        total_qty = old_qty + new_qty
                    else:
                        avg       = round((old_entry + new_price) / 2, 4)
                        total_qty = (old_qty or 0) + (new_qty or 0) or None
                    tgt = calc_targets(avg, {})
                    with _lock:
                        existing["entry"] = avg
                        existing["stop"]  = tgt["stop"]
                        existing["t1"]    = tgt["t1"]
                        existing["t2"]    = tgt["t2"]
                        existing["qty"]   = total_qty
                    save_portfolio()
                    if DB_OK:
                        db_save_position(uid, sym_a, existing)
                    send_to(uid,
                        f"📊 <b>{sym_a}</b> averaged\n"
                        f"${old_entry:.2f} + ${new_price:.2f}  →  avg <b>${avg:.2f}</b>"
                        + (f"  ×{total_qty}" if total_qty else "") + "\n"
                        f"Stop ${tgt['stop']}   T1 ${tgt['t1']}   T2 ${tgt['t2']}"
                    )
            except ValueError:
                send_to(uid, "❌ Format: ADD SYMBOL PRICE [SHARES]\nExample: ADD AUUD 1.75 100")

    elif cmd.startswith("sell "):
        parts = text.upper().split()
        if len(parts) >= 2:
            sym = parts[1]
            # SELL NNVC [exit_price] [qty]
            exit_price = float(parts[2]) if len(parts) >= 3 else None
            qty_sell   = int(parts[3])   if len(parts) >= 4 else None
            with _lock:
                removed = portfolio.get(uid, {}).pop(sym, None)
                if removed:
                    _last_removed[uid] = {"sym": sym, "pos": dict(removed)}
            if removed:
                save_portfolio()
                entry     = removed.get("entry", 0)
                # Use stored qty from BUY if not provided in SELL
                qty_final = qty_sell or removed.get("qty")
                # Remove from DB portfolio and log closed trade
                if DB_OK:
                    db_remove_position(uid, sym)
                    db_log_trade(
                        chat_id=uid, symbol=sym,
                        entry=entry,
                        exit_price=exit_price, qty=qty_final,
                    )
                if exit_price:
                    pnl_p  = (exit_price - entry) / entry * 100 if entry else 0
                    icon   = "🟢" if exit_price >= entry else "🔴"
                    pnl_d  = round((exit_price - entry) * qty_final, 2) if qty_final else None
                    pnl_d_str = f"  (${pnl_d:+.2f})" if pnl_d is not None else ""
                    send_to(uid,
                        f"✅ <b>{sym}</b> closed\n"
                        f"Entry ${entry:.2f}  →  Exit ${exit_price:.2f}  "
                        f"{icon}{pnl_p:+.1f}%{pnl_d_str}\n"
                        f"Logged to database."
                    )
                else:
                    tip = f"<code>SELL {sym} 3.60</code>" if not removed.get("qty") \
                          else f"<code>SELL {sym} 3.60</code>  (qty {removed['qty']} stored)"
                    send_to(uid,
                        f"✅ {sym} removed from tracking.\n"
                        f"Tip: {tip} to log the exit price & P&L."
                    )
            else:
                # Not tracked — but if user gave exit price, log it anyway
                if exit_price:
                    qty_final = qty_sell
                    if DB_OK:
                        db_log_trade(
                            chat_id=uid, symbol=sym,
                            entry=0.0,
                            exit_price=exit_price, qty=qty_final,
                        )
                    qty_str = f"  ×{qty_final}" if qty_final else ""
                    send_to(uid,
                        f"✅ <b>{sym}</b> exit logged at ${exit_price:.2f}{qty_str}\n"
                        f"⚠️ No entry tracked — send <code>BUY {sym} price</code> next time to track P&L."
                    )
                else:
                    send_to(uid,
                        f"❌ {sym} is not in your portfolio.\n"
                        f"Send <code>BUY {sym} price</code> first to track it."
                    )

    elif cmd.startswith("edit buy ") or cmd.startswith("edit sell ") or cmd.startswith("/edit buy ") or cmd.startswith("/edit sell "):
        parts = text.upper().lstrip("/").split()
        # parts[0]=EDIT  parts[1]=BUY/SELL  parts[2]=SYM  parts[3]=PRICE  parts[4]=QTY
        if len(parts) < 4:
            send_to(uid,
                "❌ Format:\n"
                "  <code>EDIT BUY AUUD 1.70</code>      → fix wrong entry price\n"
                "  <code>EDIT BUY AUUD 1.70 200</code>  → fix entry + shares\n"
                "  <code>EDIT SELL AUUD 1.75</code>     → fix last sell price\n"
                "  <code>EDIT SELL AUUD 1.75 200</code> → fix sell price + shares"
            )
        else:
            action  = parts[1]   # BUY or SELL
            sym_e   = parts[2]
            try:
                new_price = float(parts[3])
                new_qty   = int(parts[4]) if len(parts) >= 5 else None

                if action == "BUY":
                    with _lock:
                        pos = portfolio.get(uid, {}).get(sym_e)
                    if pos is not None:
                        # Open position — update portfolio
                        old_entry = pos["entry"]
                        tgt = calc_targets(new_price, {})
                        with _lock:
                            pos["entry"] = new_price
                            pos["stop"]  = tgt["stop"]
                            pos["t1"]    = tgt["t1"]
                            pos["t2"]    = tgt["t2"]
                            if new_qty is not None:
                                pos["qty"] = new_qty
                        save_portfolio()
                        if DB_OK:
                            db_save_position(uid, sym_e, pos)
                        qty_str = f"  ×{pos['qty']}" if pos.get("qty") else ""
                        send_to(uid,
                            f"✅ <b>{sym_e}</b> entry corrected\n"
                            f"${old_entry:.2f}  →  ${new_price:.2f}{qty_str}\n"
                            f"Stop ${tgt['stop']}   T1 ${tgt['t1']}   T2 ${tgt['t2']}"
                        )
                    else:
                        # Closed trade — update last trade record in DB
                        updated = db_update_last_trade(
                            uid, sym_e, entry=new_price, qty=new_qty
                        ) if DB_OK else False
                        if updated:
                            send_to(uid, f"✅ <b>{sym_e}</b> buy price corrected to ${new_price:.2f} in trade history.")
                        else:
                            send_to(uid, f"❌ No trade record found for {sym_e}.")

                else:  # SELL
                    updated = db_update_last_trade(uid, sym_e,
                                                      exit_price=new_price,
                                                      qty=new_qty) if DB_OK else False
                    qty_str = f"  ×{new_qty}" if new_qty else ""
                    if updated:
                        send_to(uid, f"✅ <b>{sym_e}</b> last sell corrected to ${new_price:.2f}{qty_str}")
                    else:
                        send_to(uid, f"⚠️ {sym_e} sell updated locally — no trade record found in DB.")

            except ValueError:
                send_to(uid,
                    "❌ Format:\n"
                    "  <code>EDIT BUY AUUD 1.70</code>\n"
                    "  <code>EDIT SELL AUUD 1.75 200</code>"
                )

    elif cmd == "undo":
        last = _last_removed.get(uid)
        if not last:
            send_to(uid, "❌ Nothing to undo.")
        else:
            sym_u = last["sym"]
            pos_u = last["pos"]
            with _lock:
                portfolio.setdefault(uid, {})[sym_u] = pos_u
                _last_removed.pop(uid, None)
            save_portfolio()
            if DB_OK:
                db_save_position(uid, sym_u, pos_u)
            send_to(uid,
                f"↩️ <b>{sym_u}</b> restored to portfolio\n"
                f"Entry ${pos_u['entry']:.2f}   Stop ${pos_u['stop']:.2f}   "
                f"T1 ${pos_u['t1']:.2f}   T2 ${pos_u['t2']:.2f}"
            )

    elif cmd.startswith("/setpin"):
        parts = text.strip().split()
        if len(parts) < 2 or not parts[1].strip().isdigit() or not (3 <= len(parts[1].strip()) <= 10):
            send_to(uid, "Usage: /setpin 1234\n\nPIN must be 3–10 digits.")
            return
        new_pin = parts[1].strip()
        if DB_OK:
            db_set_pin(uid, new_pin)
            send_to(uid,
                f"✅ <b>PIN updated!</b>\n\n"
                f"Your new dashboard PIN: <b>{new_pin}</b>\n\n"
                f"Login at: http://localhost:8050\n"
                f"Username: your Telegram name"
            )
        else:
            send_to(uid, "❌ Database not connected. PIN not saved.")

    elif cmd == "/claude":
        paused   = time.time() < _claude_pause_until
        resume_s = max(0, int(_claude_pause_until - time.time()))
        if _claude_active:
            send_to(uid,
                f"🤖 <b>Claude AI — Active</b>\n\n"
                f"Model  : claude-haiku-4-5\n"
                f"Status : ✅ Online\n"
                f"Cache  : {len(_claude_cache)} headlines scored\n\n"
                f"<b>What Claude does:</b>\n"
                f"• Reads news headlines for real catalyst quality\n"
                f"• Final LEGIT/PUMP check before every alert\n"
                f"• Falls back to keyword logic if API times out"
            )
        elif paused:
            send_to(uid,
                f"🤖 <b>Claude AI — Circuit Breaker Open</b>\n\n"
                f"Too many API errors — paused for {resume_s//60}m {resume_s%60}s\n"
                f"Running on keyword logic until then.\n\n"
                f"Auto-resumes in {resume_s//60} min."
            )
        elif ANTHROPIC_API_KEY:
            send_to(uid,
                "🤖 <b>Claude AI — Key Set, Not Tested Yet</b>\n\n"
                "API key is configured. Claude will activate\n"
                "on first scan that has a stock to analyze."
            )
        else:
            send_to(uid,
                "⚠️ <b>Claude AI — Inactive</b>\n\n"
                "No API key set. Running on keyword logic only.\n"
                "Keyword rules cover: RSI, float, P&D patterns,\n"
                "80+ catalyst keywords, dilution signals.\n\n"
                "To activate Claude: add credits at console.anthropic.com"
            )

    elif cmd in ("/help", "/start"):
        admin_section = (
            "\n\n<b>Admin commands (you only):</b>\n"
            "/adduser 123456789    → approve user\n"
            "/removeuser 123456789 → remove user\n"
            "/users                → list all users"
        ) if is_admin(uid) else ""
        send_to(uid,
            "📖 <b>Commands</b>\n\n"
            "/check NNVC   → full analysis on any stock\n"
            "/scan         → trigger scan now\n"
            "/status       → session & filter info\n"
            "/claude       → Claude AI status\n"
            "/watchlist    → last 10 alerts\n"
            "/portfolio    → your tracked positions\n"
            "/track NNVC   → always scan this symbol every cycle\n"
            "/untrack NNVC → remove from tracked list\n"
            "/setpin 1234  → change your dashboard PIN\n\n"
            "<b>Position tracking:</b>\n"
            "BUY NNVC 1.75        → new buy (shares optional)\n"
            "BUY NNVC 1.75 100   → new buy with shares\n"
            "ADD NNVC 1.80 100    → second buy (averages entry)\n"
            "SELL NNVC            → stop tracking\n"
            "SELL NNVC 2.10 100   → stop + log P&L\n\n"
            "<b>Fix mistakes:</b>\n"
            "EDIT BUY NNVC 1.70        → correct wrong buy price\n"
            "EDIT BUY NNVC 1.70 100    → correct buy price + shares\n"
            "EDIT SELL NNVC 2.10       → correct last sell price\n"
            "EDIT SELL NNVC 2.10 100   → correct sell price + shares\n"
            "UNDO                      → restore last sold position\n\n"
            "<b>Grades:</b>\n"
            "🅰️ A = strong setup — best trades\n"
            "🅱️ B = good setup\n"
            "⚠️ C = weak — not alerted"
            + admin_section
        )


def telegram_listener():
    log.info("[Telegram] listener started")
    cmd_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="cmd")

    def _dispatch(uid, text, name, username):
        try:
            handle_command(uid, text, name, username)
        except Exception as e:
            log.error(f"handle_command error: {e}")
            send_to(uid, "❌ Something went wrong. Try again.")

    while True:
        try:
            updates = get_updates(_last_upd_id[0])
            for upd in updates:
                _last_upd_id[0] = upd["update_id"] + 1
                msg      = upd.get("message", {})
                text     = msg.get("text", "").strip()
                sender   = msg.get("from", {})
                uid      = str(sender.get("id", ""))
                name     = sender.get("first_name", "")
                username = sender.get("username", "")
                if text and uid:
                    log.info(f"[Telegram] {uid} ({username or name}): {text}")
                    with _lock:
                        if uid in users and (name or username):
                            stored = users[uid]
                            real_name = name or username or uid
                            if stored.get("name") == uid or not stored.get("name"):
                                stored["name"] = real_name
                            if username:
                                stored["username"] = username
                            if DB_OK:
                                db_upsert_user(uid, stored["name"], username,
                                                  stored["active"], stored.get("is_admin", False))
                    cmd_pool.submit(_dispatch, uid, text, name, username)
        except Exception as e:
            log.warning(f"[Listener error] {e}")
        time.sleep(2)


# ═══════════════════════════════════════════════════════════════
#  MAIN SCAN  (parallel)
# ═══════════════════════════════════════════════════════════════

def passes_filters(stock: dict, fv: dict, f: dict) -> tuple:
    """Returns (passed: bool, reason: str)"""
    p       = fv.get("price") or stock["price"]
    chg     = stock["change"]
    vol     = fv.get("volume") or stock["volume"]
    rsi     = fv.get("rsi")
    flt     = fv.get("float_m")
    mc      = fv.get("mcap_m")
    rv      = fv.get("rel_vol")
    cat_pts = score_catalyst(fv.get("news", ""))[0]

    if not (MIN_PRICE <= p <= MAX_PRICE): return False, f"price ${p:.2f} out of range"
    if chg < f["min_change"]:             return False, f"change {chg:.0f}% < {f['min_change']:.0f}%"
    if vol and vol < f["min_volume"] and not (rv and rv > 15):
                                          return False, f"vol {vol:,} < {f['min_volume']:,}"
    # Hard liquidity floor — absolute $-volume (price x shares). Cannot be
    # bypassed by high rel-volume: a tiny-float stock always shows huge rel-vol
    # while barely any money trades, so you get stuck with no buyers on exit.
    dollar_vol = (p or 0) * (vol or 0)
    if vol and dollar_vol < f.get("min_dollar_vol", 0):
                                          return False, (f"thin liquidity ${dollar_vol/1e6:.2f}M $-vol "
                                                         f"< ${f['min_dollar_vol']/1e6:.1f}M — no buyers to exit safely")
    if rsi is None:
        if cat_pts < 2:                   return False, "RSI unknown — no strong catalyst to verify momentum"
    if rsi and rsi > f["max_rsi"]:        return False, f"RSI {rsi:.0f} > {f['max_rsi']:.0f} — parabolic"
    if rsi and rsi < 45:                  return False, f"RSI {rsi:.0f} < 45 — momentum fading/dumping"
    mfi = fv.get("mfi")
    if mfi and mfi >= 90 and (rsi is None or rsi > 75):
                                          return False, f"MFI {mfi:.0f} — money flow exhausted"
    if flt is None and mc and mc > 100:   return False, f"no float, mcap ${mc:.0f}M too large"
    if mc and mc < 1:                     return False, f"nano-cap ${mc:.1f}M"
    vwap = fv.get("vwap")
    if vwap and vwap > 0:
        vwap_limit = 1.8 if (fv.get("rsi_source") == "daily" and chg > 100) else 2.5
        if p > vwap * vwap_limit:         return False, f"price ${p:.2f} > {vwap_limit}x VWAP ${vwap:.2f}"
    if rv and rv < 0.5 and chg > 50:      return False, f"dying volume RelVol={rv:.1f}x"
    h, l      = fv.get("high"), fv.get("low")
    obv       = fv.get("obv_trend", "→")
    pos       = (p - l) / (h - l) if (h and l and h != l) else None

    # ── Already ran and dumped today ─────────────────────────
    if h and h > 0 and p < h * 0.75:
        return False, f"${p:.2f} is {((h-p)/h*100):.0f}% below day high ${h:.2f} — already ran and dumped"

    # ── P&D Detection Rules ───────────────────────────────────
    # 1. No catalyst on a big move
    if chg >= 75  and cat_pts == 0:
        return False, f"no catalyst on {chg:.0f}% move — likely P&D"
    # 2. Parabolic move needs verified catalyst
    if chg >= 150 and cat_pts < 2:
        return False, f"no strong catalyst on {chg:.0f}% move"
    # 3. Micro-float coordinated pump
    if flt and flt < 2 and chg > 60 and cat_pts < 2:
        return False, f"micro-float {flt:.1f}M + {chg:.0f}% + no strong catalyst"
    # 4. Near day high with weak catalyst — at peak of pump
    if pos is not None and pos > 0.85 and cat_pts < 2:
        return False, f"at {pos:.0%} of day range — near peak, weak catalyst"
    # 5. MFI overbought + near high = buying exhaustion at pump peak
    if mfi and mfi > 80 and pos is not None and pos > 0.75 and cat_pts < 2:
        return False, f"MFI {mfi:.0f} overbought + at {pos:.0%} of range — peak pump"
    # 6. OBV falling while price is up — distribution (smart money exiting)
    if obv == "↓" and chg > 30 and cat_pts < 2:
        return False, f"OBV falling while up {chg:.0f}% — distribution, not accumulation"
    # 7. RSI extreme + big move + no catalyst
    if rsi and rsi > 75 and chg > 50 and cat_pts == 0:
        return False, f"RSI {rsi:.0f} + {chg:.0f}% + no catalyst — chasing a pump"
    # 8. Extreme relative volume with no catalyst = coordinated buying
    if rv and rv > 50 and chg > 80 and cat_pts == 0:
        return False, f"RelVol {rv:.0f}x + {chg:.0f}% + no catalyst — unusual volume spike"

    if flt and flt > f["max_float_m"]:    return False, f"float {flt:.1f}M > {f['max_float_m']:.0f}M"
    if mc  and mc  > f["max_mcap_m"]:     return False, f"mcap ${mc:.0f}M > ${f['max_mcap_m']:.0f}M"
    if compute_grade(stock, fv) == "C":   return False, "Grade C"
    # ── Final AI gate (Claude when available, skipped when offline) ──
    legit, ai_reason = claude_pnd_check(stock.get("symbol", ""), stock, fv)
    if not legit:
        return False, ai_reason
    return True, ""

def _process_stock(stock: dict, f: dict):
    sym     = stock["symbol"]
    session = get_session() or "UNK"
    fv      = fetch_stock_data(sym)
    if fv is None:
        return None
    grade = compute_grade(stock, fv)
    passed, reason = passes_filters(stock, fv, f)
    if not passed:
        log.info(f"  Skip {sym:6s}  ${stock['price']:.2f}  {stock['change']:+.0f}%  "
                 f"RSI={fv.get('rsi','?')}  Float={fv.get('float_m','?')}M  Grade={grade}  [{reason}]")
        if DB_OK:
            db_log_scan(sym, stock["price"], stock["change"], grade, False, reason, session)
        return None
    if DB_OK:
        db_log_scan(sym, fv.get("price") or stock["price"], stock["change"], grade, True, "", session)
    return (stock, fv, grade)

def run_scan(requester_id=None):
    def _reply(msg):
        if requester_id:
            send_to(requester_id, msg)

    session = get_session()
    now_str = datetime.now(EASTERN).strftime("%H:%M:%S")
    if session is None:
        log.info(f"[{now_str}] Market closed — skipping")
        _reply("🔴 Market is closed right now. No scan running.")
        return


    f = FILTERS[session]

    # ── Market context (SPY) ──────────────────────────────
    spy_chg = fetch_spy_change()
    _market_context["spy_chg"] = spy_chg
    if spy_chg <= -3.0:
        log.info(f"[{now_str}] SPY {spy_chg:+.1f}% — market crash, skipping scan")
        broadcast(f"⚠️ <b>Market Alert</b>\nSPY {spy_chg:+.1f}% — market is crashing. Skipping scan — avoid new entries.")
        check_portfolio()
        return
    if spy_chg <= -1.5:
        log.info(f"[{now_str}] SPY {spy_chg:+.1f}% — weak market, raising filters")
        f = {**f, "min_change": f["min_change"] + 5}

    log.info(f"[{now_str}] {SESSION_LABELS[session]} scan...  SPY {spy_chg:+.1f}%")

    gainers = fetch_gainers(session)
    if not gainers:
        log.warning("No data returned")
        _reply("⚠️ Could not fetch gainers list. Site may be slow — try again in 1 min.")
        return

    # Basic pre-filter — no API calls yet
    # Take top 20 by change%; drop only out-of-range price and cooldown stocks.
    # Volume check is skipped here — Webull gives accurate live volume in passes_filters.
    candidates = []
    for stock in gainers[:20]:
        p, chg = stock["price"], stock["change"]
        if not (MIN_PRICE <= p <= MAX_PRICE): continue
        if chg < f["min_change"]:             continue
        with _lock:
            if (time.time() - alerted.get(stock["symbol"], 0)) < ALERT_COOLDOWN:
                continue
        candidates.append(stock)

    if not candidates:
        log.info("  No candidates after basic filter")
        _reply(f"🔍 Scan done — no stocks passed basic filters.\n"
               f"(Change >{f['min_change']}%, Price ${MIN_PRICE}–${MAX_PRICE})")
        check_portfolio()
        return

    log.info(f"  {len(candidates)} candidates — fetching in parallel...")

    # Fetch all candidates simultaneously
    results = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        futures = {pool.submit(_process_stock, s, f): s for s in candidates}
        for future in as_completed(futures):
            r = future.result()
            if r:
                results.append(r)

    # A grades first
    results.sort(key=lambda x: x[2])

    sent = 0
    for stock, fv, grade in results:
        sym = stock["symbol"]
        with _lock:
            if (time.time() - alerted.get(sym, 0)) < ALERT_COOLDOWN:
                continue
        # Re-alert gate: allow if fresh money flow confirms a new leg
        prev = next((item for item in reversed(list(watchlist_log)) if item["sym"] == sym), None)
        curr_price = fv.get("price") or stock["price"]
        if prev:
            ratio     = curr_price / prev["price"]
            obv_fresh = fv.get("obv_trend") == "↑"
            mfi       = fv.get("mfi")
            mfi_ok    = mfi is None or mfi < 75
            if 0.85 <= ratio <= 1.50:
                if obv_fresh and mfi_ok:
                    mfi_s = f"{mfi:.0f}" if mfi else "N/A"
                    log.info(f"  Allow {sym} re-alert — OBV↑ + MFI={mfi_s} → fresh new leg vs prev ${prev['price']:.2f}")
                else:
                    log.info(f"  Skip {sym} re-alert — no fresh money flow (OBV={fv.get('obv_trend')}, MFI={mfi}) vs prev ${prev['price']:.2f}")
                    continue
        broadcast(build_alert_simple(stock, fv, session))
        with _lock:
            alerted[sym] = time.time()
            watchlist_log.append({
                "sym": sym, "price": stock["price"], "change": stock["change"],
                "grade": grade, "time": datetime.now(EASTERN).strftime("%H:%M"),
            })
        save_watchlist()
        save_alerted()
        # Log alert to SQL Server
        if DB_OK:
            db_log_alert(
                symbol=sym, price=stock["price"], grade=grade,
                change_pct=stock["change"],
                float_m=fv.get("float_m"), rsi=fv.get("rsi"),
                volume=fv.get("volume") or stock.get("volume"),
                rel_vol=fv.get("rel_vol"), mcap_m=fv.get("mcap_m"),
                session=session,
            )
        sent += 1
        log.info(f"  Broadcast → {sym}  ${stock['price']:.2f}  ({stock['change']:+.1f}%)  Grade={grade}")

        # Sector follow-through — scan peers in background
        peers = SECTOR_PEERS.get(sym, [])
        if peers:
            def _check_peers(triggered=sym, peer_list=peers, sess=session, fil=f):
                time.sleep(2)
                for peer in peer_list:
                    with _lock:
                        if (time.time() - alerted.get(peer, 0)) < ALERT_COOLDOWN:
                            continue
                    fv2 = fetch_stock_data(peer)
                    if not fv2 or not fv2.get("price"):
                        continue
                    p2 = fv2["price"]
                    if not (MIN_PRICE <= p2 <= MAX_PRICE):
                        continue
                    st2 = {"symbol": peer, "change": 0.0, "price": p2, "volume": fv2.get("volume", 0)}
                    grade2 = compute_grade(st2, fv2)
                    if grade2 == "C":
                        continue
                    broadcast(
                        f"🔗 <b>SECTOR PLAY — {peer}</b> (same sector as {triggered})\n"
                        + build_alert_simple(st2, fv2, sess)
                    )
                    with _lock:
                        alerted[peer] = time.time()
                    log.info(f"  Sector follow → {peer}  Grade={grade2}")
                    time.sleep(0.5)
            threading.Thread(target=_check_peers, daemon=True).start()

        time.sleep(0.5)

    # ── Tracked symbols — always scan regardless of gainers list ──
    with _lock:
        tracked = set(tracked_symbols)
    for sym in tracked:
        with _lock:
            if (time.time() - alerted.get(sym, 0)) < ALERT_COOLDOWN:
                continue
        fv2 = fetch_stock_data(sym)
        if not fv2 or not fv2.get("price"):
            continue
        p2  = fv2["price"]
        if not (MIN_PRICE <= p2 <= MAX_PRICE):
            continue
        st2   = {"symbol": sym, "change": 0.0, "price": p2, "volume": fv2.get("volume", 0)}
        grade2 = compute_grade(st2, fv2)
        if grade2 == "C":
            continue
        broadcast("📌 <b>TRACKED</b>  " + build_alert_simple(st2, fv2, session))
        with _lock:
            alerted[sym] = time.time()
        save_alerted()
        log.info(f"  Tracked alert → {sym}  Grade={grade2}")

    check_portfolio()

    if sent == 0:
        log.info("  No A/B grade matches")
        _reply("🔍 Scan done — no Grade A/B stocks right now. Filters are working but nothing qualifies yet.")


def reset_stale_cooldowns():
    """Reset alert cooldown for stocks that have pulled back 15%+ from alert price."""
    with _lock:
        to_reset = list(alerted.items())
    for sym, ts in to_reset:
        if time.time() - ts < 900:   # only check if alerted 15+ min ago
            continue
        wb = fetch_webull(sym)
        if not wb or not wb.get("price"):
            continue
        # Find alert price from watchlist log
        alert_price = next(
            (item["price"] for item in watchlist_log if item["sym"] == sym), None
        )
        if not alert_price:
            continue
        drop = (wb["price"] - alert_price) / alert_price * 100
        if drop <= -15:
            with _lock:
                alerted.pop(sym, None)
            log.info(f"  Cooldown reset {sym} (dropped {drop:.1f}% from alert)")


# ═══════════════════════════════════════════════════════════════
#  SINGLE-INSTANCE LOCK  (cross-platform)
# ═══════════════════════════════════════════════════════════════

_lock_handle = None

def acquire_lock() -> bool:
    global _lock_handle
    if platform.system() == "Windows":
        import ctypes
        _lock_handle = ctypes.windll.kernel32.CreateMutexW(None, False, "StockScannerBot")
        if ctypes.windll.kernel32.GetLastError() == 183:
            log.error("Another instance already running. Exiting.")
            return False
    else:
        import fcntl
        try:
            _lock_handle = open("/tmp/stockbot.lock", "w")
            fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error("Another instance already running. Exiting.")
            return False
    return True

def release_lock():
    global _lock_handle
    if _lock_handle is None:
        return
    if platform.system() == "Windows":
        import ctypes
        ctypes.windll.kernel32.CloseHandle(_lock_handle)
    else:
        import fcntl
        fcntl.flock(_lock_handle, fcntl.LOCK_UN)
        _lock_handle.close()
    _lock_handle = None


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def reset_daily():
    """
    Run at 9:25 AM ET (just before market open).
    Clears watchlist_log so yesterday's alert prices don't block today's fresh setups.
    """
    global watchlist_log
    with _lock:
        count = len(watchlist_log)
        watchlist_log.clear()
    save_watchlist()
    log.info(f"[Daily Reset] watchlist_log cleared ({count} entries) — fresh start for today")


def check_alert_performance():
    """
    Run at 16:05 ET (market close + 5 min).
    Fetches close prices for today's alerts, marks PASS/FAIL, sends summary.
    """
    if not DB_OK:
        return
    alerts = db_get_todays_alerts()
    if not alerts:
        return

    log.info(f"[Performance] Checking {len(alerts)} alert(s) from today...")
    results = []
    for a in alerts:
        sym         = a["symbol"]
        alert_price = float(a["alert_price"])
        grade       = a.get("grade", "")
        session     = a.get("session", "")
        close       = fetch_price_live(sym)
        if not close:
            continue
        pct     = round((close - alert_price) / alert_price * 100, 2)
        outcome = "PASS" if pct >= 0 else "FAIL"
        db_update_alert_outcome(a["id"], close, pct, outcome)
        results.append({"sym": sym, "grade": grade, "session": session,
                         "alert": alert_price, "close": close,
                         "pct": pct, "outcome": outcome})

    if not results:
        return

    passed  = [r for r in results if r["outcome"] == "PASS"]
    failed  = [r for r in results if r["outcome"] == "FAIL"]
    win_rate = round(len(passed) / len(results) * 100) if results else 0
    today_s  = datetime.now(EASTERN).strftime("%b %d, %Y")

    lines = [
        f"📊 <b>Daily Alert Report — {today_s}</b>\n",
        f"Total: {len(results)}  |  ✅ {len(passed)} Pass  |  ❌ {len(failed)} Fail  |  Win: {win_rate}%\n",
    ]
    if passed:
        lines.append("✅ <b>PASSED:</b>")
        for r in passed:
            lines.append(f"  <b>{r['sym']}</b> [{r['grade']}]  ${r['alert']:.2f} → ${r['close']:.2f}  <b>({r['pct']:+.1f}%)</b>")
    if failed:
        lines.append("\n❌ <b>FAILED:</b>")
        for r in failed:
            lines.append(f"  <b>{r['sym']}</b> [{r['grade']}]  ${r['alert']:.2f} → ${r['close']:.2f}  ({r['pct']:+.1f}%)")

    broadcast("\n".join(lines))
    log.info(f"[Performance] Report sent — {len(passed)}/{len(results)} passed ({win_rate}%)")


def main():
    if not acquire_lock():
        sys.exit(1)

    import atexit
    atexit.register(release_lock)

    test_connection()   # sets DB_OK — must run before load_users
    test_claude()          # tests API key, sets _claude_active flag
    load_users()
    load_portfolio()
    load_watchlist()
    load_alerted()
    load_tracked()
    log.info("=" * 60)
    log.info("Stock Scanner Bot v4  (Multi-User | Parallel)")
    log.info(f"  Admin       : {ADMIN_ID}")
    log.info(f"  Active users: {len(get_active_users())}")
    log.info(f"  Scan every  : {SCAN_EVERY_MIN} min  |  A & B grades only")
    log.info(f"  Workers     : {SCAN_WORKERS} parallel stocks")
    log.info("=" * 60)

    threading.Thread(target=telegram_listener, daemon=True).start()

    session  = get_session()
    ksa_time = datetime.now(KSA_TZ).strftime("%I:%M %p KSA")
    edt_time = datetime.now(EASTERN).strftime("%I:%M %p EDT")

    send_to(ADMIN_ID,
        f"🚀 <b>Stock Scanner Bot v4 — Online</b>\n\n"
        f"Session : {SESSION_LABELS.get(session, '🔴 Market Closed')}\n"
        f"Time    : {edt_time}  |  {ksa_time}\n"
        f"Users   : {len(get_active_users())} active\n\n"
        f"Send /help for all commands"
    )

    run_scan()
    schedule.every(SCAN_EVERY_MIN).minutes.do(run_scan)
    schedule.every(15).minutes.do(reset_stale_cooldowns)
    schedule.every().day.at("09:25").do(reset_daily)        # ET — clear yesterday's alerts before open
    schedule.every().day.at("16:05").do(check_alert_performance)
    while True:
        schedule.run_pending()
        time.sleep(20)


# ========================================================================
# WEB DASHBOARD  (Dash)
# ========================================================================

"""
StockBot Dashboard — Linux / SQLite version
Run : python dashboard.py
Open: http://YOUR_SERVER_IP:8050  (or Railway $PORT)

Login with your Telegram name + PIN (default PIN: 1234)
Change PIN in Telegram: /setpin XXXX

Features:
  • Responsive — works on desktop, tablet (iPad) and mobile.
  • Bilingual — English / العربية toggle in the top bar, full RTL when Arabic.
  • Pro insights — equity curve, trade stats, per-symbol leaderboard, alert funnel.
"""
import sqlite3
import os
import pandas as pd
from datetime import datetime, date
from dash import Dash, dcc, html, Input, Output, State, no_update
import plotly.graph_objects as go

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DB_PATH can be overridden (e.g. a Railway Volume at /data) for persistence.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  THEME
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BG      = "#0A0E1A"
SURFACE = "#111827"
SURF2   = "#1C2333"
BORDER  = "#1F2D45"
ACCENT  = "#3B82F6"
GREEN   = "#22C55E"
RED     = "#EF4444"
YELLOW  = "#F59E0B"
CYAN    = "#06B6D4"
PURPLE  = "#A855F7"
TEXT    = "#E2E8F0"
MUTED   = "#64748B"
WHITE   = "#F8FAFC"

FONT_STACK = "Inter, Cairo, system-ui, -apple-system, sans-serif"

# Translucent accent for chart area fills. Plotly rejects 8-digit hex (#RRGGBBAA),
# so use rgba — ACCENT (#3B82F6) at ~13% opacity.
ACCENT_FILL = "rgba(59,130,246,0.13)"

_INPUT = {
    "width": "100%", "boxSizing": "border-box",
    "background": BG, "border": f"1px solid {BORDER}",
    "borderRadius": "8px", "padding": "11px 14px",
    "color": WHITE, "fontSize": "14px", "outline": "none",
    "marginBottom": "18px", "fontFamily": FONT_STACK,
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  i18n  —  English / العربية
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TR = {
    "en": {
        # nav / chrome
        "nav_overview": "Overview", "nav_alerts": "Alerts", "nav_trades": "Trades",
        "nav_scanlog": "Scan Log", "nav_portfolio": "Portfolio", "nav_insights": "Insights",
        "brand_sub": "Dashboard", "your_account": "your account", "admin": "ADMIN",
        "sign_out": "Sign out",
        # login
        "sign_in_sub": "Sign in to your dashboard", "username": "Username",
        "username_ph": "Your Telegram name", "pin": "PIN", "pin_ph": "4-digit PIN",
        "sign_in": "Sign In",
        "login_hint": "Default PIN: 1234  ·  Change with /setpin in Telegram",
        "login_error": "Incorrect name or PIN. Check your Telegram name and try again.",
        # market
        "WEEKEND": "WEEKEND", "MARKET OPEN": "MARKET OPEN", "PRE-MARKET": "PRE-MARKET",
        "AFTER-HOURS": "AFTER-HOURS", "CLOSED": "CLOSED", "UNKNOWN": "UNKNOWN",
        # kpis
        "kpi_alerts_today": "Alerts Today", "kpi_win_rate": "Win Rate",
        "kpi_net_pnl": "Net P&L", "kpi_scan_pass": "Scan Pass Rate",
        "kpi_open_pos": "Open Positions", "kpi_total_alerts": "Total Alerts",
        "kpi_today": "Today", "kpi_avg_change": "Avg Change", "kpi_bot_win": "Bot Win Rate",
        "kpi_trades": "Trades", "kpi_profit_factor": "Profit Factor",
        "kpi_avg_win": "Avg Win", "kpi_avg_loss": "Avg Loss",
        "kpi_total_scanned": "Total Scanned", "kpi_passed": "Passed",
        "kpi_filtered_out": "Filtered Out", "kpi_with_shares": "With Shares",
        "kpi_expectancy": "Expectancy", "kpi_best_trade": "Best Trade",
        "kpi_worst_trade": "Worst Trade", "kpi_win_streak": "Win Streak",
        # kpi sub-text
        "sub_total": "total", "sub_all_my_trades": "all my trades", "sub_passed": "passed",
        "sub_my_portfolio": "my portfolio", "sub_all_time": "all time",
        "sub_new_today": "new today", "sub_per_alert": "per alert",
        "sub_alerts_worked": "alerts worked", "sub_filtered_trades": "filtered trades",
        "sub_gross_wl": "gross W ÷ L", "sub_per_winner": "per winner",
        "sub_per_loser": "per loser", "sub_didnt_qualify": "didn't qualify",
        "sub_scanned_today": "scanned today", "sub_tracked_by_bot": "tracked by bot",
        "sub_qty_recorded": "qty recorded", "sub_per_trade": "per trade",
        "sub_max": "max", "sub_best_grade": "avg % after alert", "sub_best_sess": "success rate",
        # section headers
        "sec_alert_activity": "Alert Activity — Last 14 Days", "sec_by_session": "By Session",
        "sec_my_recent_trades": "My Recent Trades", "sec_by_grade": "By Grade",
        "sec_top_stocks": "Top Stocks", "sec_today_alert_perf": "Today's Alert Performance",
        "sec_all_alerts": "All Alerts", "sec_pnl_by_symbol": "P&L by Symbol",
        "sec_cumulative_pnl": "Cumulative P&L", "sec_trade_history": "Trade History",
        "sec_top_filter_reasons": "Top Filter Reasons", "sec_recent_scan_log": "Recent Scan Log",
        "sec_open_positions": "Open Positions", "sec_equity_curve": "Equity Curve",
        "sec_trade_stats": "Trade Statistics", "sec_symbol_leaderboard": "Symbol Leaderboard",
        "sec_alert_funnel": "Alert Quality Funnel", "sec_grade_perf": "Does Grade A Beat B?",
        "sec_best_session": "Best Session for Alerts",
        # filter bar
        "period": "Period:", "all_time_opt": "All Time", "by_day": "By Day", "by_month": "By Month",
        # messages
        "no_trades_period": "No trades for selected period.\n\nUse  BUY SYMBOL PRICE  and  SELL SYMBOL PRICE  in Telegram.",
        "no_data": "No data yet.",
        "no_scan": "No scan data yet. Scans run every few minutes during market hours.",
        # badges / values
        "pass": "PASS", "fail": "FAIL", "pending": "PENDING",
        "res_win": "WIN", "res_loss": "LOSS", "val_pass": "PASS", "val_skip": "SKIP",
        # perf / leaderboard table headers
        "th_symbol": "Symbol", "th_grade": "Grade", "th_alert_price": "Alert Price",
        "th_close_price": "Close Price", "th_change": "Change", "th_outcome": "Outcome",
        "th_trades": "Trades", "th_winrate": "Win Rate", "th_net_pnl": "Net P&L",
        # funnel stages
        "funnel_scanned": "Scanned", "funnel_passed": "Passed Filters",
        "funnel_alerted": "Alerted", "funnel_worked": "Worked",
        "lang_switch": "العربية",
    },
    "ar": {
        "nav_overview": "نظرة عامة", "nav_alerts": "التنبيهات", "nav_trades": "الصفقات",
        "nav_scanlog": "سجل الفحص", "nav_portfolio": "المحفظة", "nav_insights": "التحليلات",
        "brand_sub": "لوحة التحكم", "your_account": "حسابك", "admin": "مشرف",
        "sign_out": "تسجيل الخروج",
        "sign_in_sub": "سجّل الدخول إلى لوحتك", "username": "اسم المستخدم",
        "username_ph": "اسمك في تيليجرام", "pin": "الرمز السري", "pin_ph": "رمز من ٤ أرقام",
        "sign_in": "دخول",
        "login_hint": "الرمز الافتراضي: 1234  ·  غيّره بأمر /setpin في تيليجرام",
        "login_error": "الاسم أو الرمز غير صحيح. تحقق من اسمك في تيليجرام وحاول مجددًا.",
        "WEEKEND": "عطلة الأسبوع", "MARKET OPEN": "السوق مفتوح", "PRE-MARKET": "ما قبل الافتتاح",
        "AFTER-HOURS": "ما بعد الإغلاق", "CLOSED": "مغلق", "UNKNOWN": "غير معروف",
        "kpi_alerts_today": "تنبيهات اليوم", "kpi_win_rate": "نسبة الربح",
        "kpi_net_pnl": "صافي الربح", "kpi_scan_pass": "نسبة اجتياز الفحص",
        "kpi_open_pos": "المراكز المفتوحة", "kpi_total_alerts": "إجمالي التنبيهات",
        "kpi_today": "اليوم", "kpi_avg_change": "متوسط التغير", "kpi_bot_win": "نسبة نجاح البوت",
        "kpi_trades": "الصفقات", "kpi_profit_factor": "معامل الربح",
        "kpi_avg_win": "متوسط الربح", "kpi_avg_loss": "متوسط الخسارة",
        "kpi_total_scanned": "إجمالي المفحوص", "kpi_passed": "اجتاز",
        "kpi_filtered_out": "مُستبعد", "kpi_with_shares": "بأسهم",
        "kpi_expectancy": "التوقع", "kpi_best_trade": "أفضل صفقة",
        "kpi_worst_trade": "أسوأ صفقة", "kpi_win_streak": "سلسلة الربح",
        "sub_total": "الإجمالي", "sub_all_my_trades": "كل صفقاتي", "sub_passed": "اجتاز",
        "sub_my_portfolio": "محفظتي", "sub_all_time": "كل الوقت",
        "sub_new_today": "جديد اليوم", "sub_per_alert": "لكل تنبيه",
        "sub_alerts_worked": "تنبيهات نجحت", "sub_filtered_trades": "الصفقات المصفّاة",
        "sub_gross_wl": "إجمالي الربح ÷ الخسارة", "sub_per_winner": "لكل رابحة",
        "sub_per_loser": "لكل خاسرة", "sub_didnt_qualify": "لم تتأهل",
        "sub_scanned_today": "فُحص اليوم", "sub_tracked_by_bot": "يتتبعها البوت",
        "sub_qty_recorded": "الكمية مسجلة", "sub_per_trade": "لكل صفقة",
        "sub_max": "الأقصى", "sub_best_grade": "متوسط % بعد التنبيه", "sub_best_sess": "نسبة النجاح",
        "sec_alert_activity": "نشاط التنبيهات — آخر ١٤ يومًا", "sec_by_session": "حسب الجلسة",
        "sec_my_recent_trades": "صفقاتي الأخيرة", "sec_by_grade": "حسب التصنيف",
        "sec_top_stocks": "أبرز الأسهم", "sec_today_alert_perf": "أداء تنبيهات اليوم",
        "sec_all_alerts": "كل التنبيهات", "sec_pnl_by_symbol": "الربح/الخسارة حسب الرمز",
        "sec_cumulative_pnl": "الربح التراكمي", "sec_trade_history": "سجل الصفقات",
        "sec_top_filter_reasons": "أهم أسباب الاستبعاد", "sec_recent_scan_log": "سجل الفحص الأخير",
        "sec_open_positions": "المراكز المفتوحة", "sec_equity_curve": "منحنى رأس المال",
        "sec_trade_stats": "إحصائيات الصفقات", "sec_symbol_leaderboard": "ترتيب الرموز",
        "sec_alert_funnel": "مسار جودة التنبيهات", "sec_grade_perf": "هل التصنيف A أفضل من B؟",
        "sec_best_session": "أفضل جلسة للتنبيهات",
        "period": "الفترة:", "all_time_opt": "كل الوقت", "by_day": "حسب اليوم", "by_month": "حسب الشهر",
        "no_trades_period": "لا توجد صفقات للفترة المحددة.\n\nاستخدم  BUY SYMBOL PRICE  و  SELL SYMBOL PRICE  في تيليجرام.",
        "no_data": "لا توجد بيانات بعد.",
        "no_scan": "لا توجد بيانات فحص بعد. يعمل الفحص كل بضع دقائق أثناء ساعات السوق.",
        "pass": "ناجح", "fail": "فاشل", "pending": "قيد الانتظار",
        "res_win": "ربح", "res_loss": "خسارة", "val_pass": "ناجح", "val_skip": "مُستبعد",
        "th_symbol": "الرمز", "th_grade": "التصنيف", "th_alert_price": "سعر التنبيه",
        "th_close_price": "سعر الإغلاق", "th_change": "التغير", "th_outcome": "النتيجة",
        "th_trades": "الصفقات", "th_winrate": "نسبة الربح", "th_net_pnl": "صافي الربح",
        "funnel_scanned": "تم فحصها", "funnel_passed": "اجتازت الفلاتر",
        "funnel_alerted": "تم التنبيه", "funnel_worked": "نجحت",
        "lang_switch": "EN",
    },
}

# Column-header labels for data tables
COL_TR = {
    "en": {
        "symbol": "Symbol", "entry_price": "Entry", "exit_price": "Exit", "qty": "Qty",
        "pnl_dollar": "P&L $", "pnl_pct": "P&L %", "result": "Result", "trade_date": "Date",
        "user_name": "User", "alert_price": "Alert Price", "grade": "Grade",
        "change_pct": "Change %", "float_m": "Float (M)", "rsi": "RSI", "session": "Session",
        "alerted_at": "Alerted", "outcome": "Outcome", "pct_after_alert": "% After",
        "price": "Price", "passed": "Passed", "skip_reason": "Skip Reason",
        "scanned_at": "Scanned", "stop_price": "Stop", "t1_price": "T1", "t2_price": "T2",
        "t1_hit": "T1 Hit", "t2_hit": "T2 Hit", "added_at": "Added", "close_price": "Close",
        "volume": "Volume", "rel_vol": "Rel Vol", "mcap_m": "MCap (M)",
    },
    "ar": {
        "symbol": "الرمز", "entry_price": "سعر الدخول", "exit_price": "سعر الخروج", "qty": "الكمية",
        "pnl_dollar": "الربح $", "pnl_pct": "الربح %", "result": "النتيجة", "trade_date": "التاريخ",
        "user_name": "المستخدم", "alert_price": "سعر التنبيه", "grade": "التصنيف",
        "change_pct": "التغير %", "float_m": "الأسهم الحرة (م)", "rsi": "RSI", "session": "الجلسة",
        "alerted_at": "وقت التنبيه", "outcome": "النتيجة", "pct_after_alert": "% بعد التنبيه",
        "price": "السعر", "passed": "اجتاز", "skip_reason": "سبب الاستبعاد",
        "scanned_at": "وقت الفحص", "stop_price": "وقف الخسارة", "t1_price": "هدف ١", "t2_price": "هدف ٢",
        "t1_hit": "تحقق هدف ١", "t2_hit": "تحقق هدف ٢", "added_at": "أُضيف", "close_price": "الإغلاق",
        "volume": "الحجم", "rel_vol": "الحجم النسبي", "mcap_m": "القيمة السوقية (م)",
    },
}

def t(key, lang="en"):
    lang = lang if lang in TR else "en"
    return TR[lang].get(key) or TR["en"].get(key) or key

def col_label(col, lang="en"):
    lang = lang if lang in COL_TR else "en"
    return (COL_TR[lang].get(col) or COL_TR["en"].get(col)
            or str(col).replace("_", " ").title())

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DATABASE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def q(sql: str) -> pd.DataFrame:
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as c:
            return pd.read_sql(sql, c)
    except Exception as e:
        print(f"[DB] {e}")
        return pd.DataFrame()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AUTH
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_login(identifier: str, pin: str):
    df = q("SELECT chat_id, name, username, is_admin, pin FROM users WHERE is_active = 1")
    if df.empty:
        return None
    ident = identifier.strip().lower().lstrip("@")
    for _, row in df.iterrows():
        name_match = str(row.get("name")     or "").lower() == ident
        user_match = str(row.get("username") or "").lower() == ident
        if name_match or user_match:
            stored = str(row.get("pin") or "1234")
            if pin.strip() == stored:
                return {
                    "chat_id":  str(int(row["chat_id"])),
                    "name":     str(row["name"]),
                    "is_admin": bool(row["is_admin"]),
                }
    return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def today_s():
    return str(date.today())

def market_status():
    """Returns (status_key, color). status_key is translated by the caller."""
    try:
        from pytz import timezone
        et = datetime.now(timezone("US/Eastern"))
        m, wd = et.hour * 60 + et.minute, et.weekday()
        if wd >= 5:                return "WEEKEND",     MUTED
        if 9*60+30 <= m < 16*60:   return "MARKET OPEN", GREEN
        if 4*60    <= m < 9*60+30: return "PRE-MARKET",  YELLOW
        if 16*60   <= m <= 20*60:  return "AFTER-HOURS", CYAN
        return "CLOSED", MUTED
    except Exception:
        return "UNKNOWN", MUTED

def fmt_usd(v):
    try:    return f"${float(v):+,.2f}"
    except: return "—"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UI COMPONENTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def kpi(label, value, sub="", color=ACCENT):
    return html.Div(style={
        "background":  SURFACE, "border": f"1px solid {BORDER}",
        "borderTop":   f"3px solid {color}", "borderRadius": "10px",
        "padding":     "16px 18px", "flex": "1", "minWidth": "130px",
    }, children=[
        html.P(label, style={"color": MUTED, "fontSize": "10px", "fontWeight": "700",
                              "letterSpacing": "1.2px", "textTransform": "uppercase",
                              "margin": "0 0 8px 0"}),
        html.P(str(value), style={"color": color, "fontSize": "24px", "fontWeight": "800",
                                   "margin": "0 0 4px 0", "lineHeight": "1"}),
        html.P(sub, style={"color": MUTED, "fontSize": "11px", "margin": 0}),
    ])

def kpi_row(children):
    return html.Div(style={"display": "flex", "gap": "12px",
                            "flexWrap": "wrap", "marginBottom": "16px"}, children=children)

def card(children, mb="14px"):
    return html.Div(children, style={
        "background": SURFACE, "border": f"1px solid {BORDER}",
        "borderRadius": "10px", "padding": "18px 20px", "marginBottom": mb,
    })

def sec(text, color=MUTED):
    return html.P(text, style={
        "color": color, "fontSize": "10px", "fontWeight": "700",
        "letterSpacing": "1.5px", "textTransform": "uppercase",
        "margin": "0 0 14px 0", "paddingBottom": "10px",
        "borderBottom": f"1px solid {BORDER}",
    })

def empty_msg(msg):
    return html.Div(msg, style={"color": MUTED, "fontSize": "13px",
                                 "textAlign": "center", "padding": "40px",
                                 "whiteSpace": "pre-line"})

def chart(fig, h=280):
    fig.update_layout(
        height=h, paper_bgcolor=SURFACE, plot_bgcolor=SURF2,
        font=dict(color=TEXT, family=FONT_STACK, size=11),
        margin=dict(t=12, b=28, l=10, r=10),
        xaxis=dict(gridcolor=BORDER, linecolor=BORDER),
        yaxis=dict(gridcolor=BORDER, linecolor=BORDER),
        legend=dict(bgcolor="rgba(0,0,0,0)", bordercolor=BORDER),
        hoverlabel=dict(bgcolor=SURF2, bordercolor=BORDER, font=dict(color=TEXT)),
    )
    return dcc.Graph(figure=fig, config={"displayModeBar": False, "responsive": True},
                     style={"borderRadius": "8px"})

def tbl(df: pd.DataFrame, lang="en", max_rows=100):
    if df is None or df.empty:
        return empty_msg(t("no_data", lang))

    def cell_style(col, val, bg):
        s = {"padding": "7px 12px", "background": bg,
             "borderBottom": f"1px solid {BORDER}",
             "fontSize": "12px", "color": TEXT, "whiteSpace": "nowrap"}
        if col == "result":
            s.update({"color": GREEN if val == "WIN" else RED, "fontWeight": "700"})
        elif col == "grade":
            s.update({"color": GREEN if val == "A" else ACCENT if val == "B" else MUTED,
                       "fontWeight": "700"})
        elif col == "passed":
            s.update({"color": GREEN if val else RED, "fontWeight": "600"})
        elif col in ("pnl_dollar", "pnl_pct", "change_pct"):
            try:
                v = float(val)
                s.update({"color": GREEN if v >= 0 else RED, "fontWeight": "600"})
            except: pass
        return s

    def fmt(col, val):
        if val is None:              return "—"
        if hasattr(val, "strftime"): return str(val)[:16]
        if col == "result":          return t("res_win", lang) if val == "WIN" else t("res_loss", lang)
        if col == "passed":          return t("val_pass", lang) if val else t("val_skip", lang)
        if col == "pnl_pct":
            try: return f"{float(val):+.2f}%"
            except: pass
        if col in ("pnl_dollar", "change_pct"):
            try: return f"{float(val):+.2f}"
            except: pass
        if isinstance(val, float):   return f"{val:.2f}"
        return str(val)

    header = html.Tr([
        html.Th(col_label(c, lang), style={
            "padding": "9px 12px", "background": BG, "color": MUTED,
            "fontSize": "10px", "fontWeight": "700", "letterSpacing": "1px",
            "textTransform": "uppercase", "whiteSpace": "nowrap",
            "borderBottom": f"2px solid {ACCENT}",
        }) for c in df.columns
    ])
    rows = []
    for i, (_, row) in enumerate(df.head(max_rows).iterrows()):
        bg = SURFACE if i % 2 == 0 else SURF2
        rows.append(html.Tr([
            html.Td(fmt(col, row[col]), style=cell_style(col, row[col], bg))
            for col in df.columns
        ]))
    return html.Div(
        style={"overflowX": "auto", "borderRadius": "8px",
               "border": f"1px solid {BORDER}"},
        children=[html.Table([html.Thead(header), html.Tbody(rows)],
                              style={"width": "100%", "borderCollapse": "collapse"})]
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SIDEBAR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NAV = [
    ("overview",  "nav_overview",  "▣"),
    ("insights",  "nav_insights",  "✦"),
    ("alerts",    "nav_alerts",    "◈"),
    ("trades",    "nav_trades",    "◎"),
    ("scanlog",   "nav_scanlog",   "◉"),
    ("portfolio", "nav_portfolio", "◐"),
]

def sidebar(active, auth, lang="en"):
    links = []
    for pid, label_key, icon in NAV:
        on = pid == active
        links.append(dcc.Link(href=f"/{pid}", style={
            "display": "flex", "alignItems": "center", "gap": "12px",
            "padding": "10px 20px",
            "color": WHITE if on else MUTED,
            "background": f"{ACCENT}18" if on else "transparent",
            "borderRight": f"3px solid {ACCENT}" if on else "3px solid transparent",
            "textDecoration": "none", "fontSize": "13px",
            "fontWeight": "600" if on else "400", "marginBottom": "2px",
        }, children=[html.Span(icon, style={"fontSize": "14px"}), t(label_key, lang)]))

    mkt, mkt_c = market_status()
    admin_badge = (
        html.Span(" " + t("admin", lang), style={
            "background": YELLOW, "color": BG, "fontSize": "9px",
            "fontWeight": "800", "borderRadius": "4px",
            "padding": "2px 6px", "letterSpacing": "0.5px",
        }) if auth.get("is_admin") else ""
    )

    return html.Div(className="app-sidebar", style={
        "width": "200px", "minWidth": "200px",
        "background": SURFACE, "borderRight": f"1px solid {BORDER}",
        "display": "flex", "flexDirection": "column",
        "position": "sticky", "top": "0", "height": "100vh",
    }, children=[
        html.Div(style={"padding": "22px 20px 18px", "borderBottom": f"1px solid {BORDER}"}, children=[
            html.Div(style={"display": "flex", "alignItems": "center", "gap": "10px"}, children=[
                html.Div("↗", style={
                    "width": "32px", "height": "32px", "background": ACCENT,
                    "borderRadius": "8px", "display": "flex", "alignItems": "center",
                    "justifyContent": "center", "fontSize": "16px",
                    "fontWeight": "800", "color": WHITE,
                }),
                html.Div([
                    html.P("StockBot", style={"color": WHITE, "fontWeight": "800",
                                               "fontSize": "13px", "margin": "0"}),
                    html.P(t("brand_sub", lang), style={"color": MUTED, "fontSize": "10px", "margin": "0"}),
                ]),
            ]),
        ]),
        html.Div(className="sidebar-hide-mobile", style={
            "padding": "12px 20px", "borderBottom": f"1px solid {BORDER}",
            "display": "flex", "alignItems": "center", "gap": "8px",
        }, children=[
            html.Div(style={
                "width": "28px", "height": "28px", "borderRadius": "50%",
                "background": ACCENT, "display": "flex",
                "alignItems": "center", "justifyContent": "center",
                "fontSize": "12px", "fontWeight": "800", "color": WHITE, "flexShrink": "0",
            }, children=str(auth.get("name", "?"))[0].upper()),
            html.Div([
                html.Div(style={"display": "flex", "alignItems": "center", "gap": "6px"}, children=[
                    html.P(auth.get("name", ""), style={"color": WHITE, "fontSize": "12px",
                                                         "fontWeight": "600", "margin": "0"}),
                    admin_badge,
                ]),
                html.P(t("your_account", lang), style={"color": MUTED, "fontSize": "10px", "margin": "0"}),
            ]),
        ]),
        html.Div(links, className="nav-links", style={"flex": "1", "padding": "14px 0", "overflowY": "auto"}),
        html.Div(className="sidebar-hide-mobile", style={"padding": "14px 20px", "borderTop": f"1px solid {BORDER}"}, children=[
            html.Div(style={"display": "flex", "alignItems": "center",
                             "gap": "6px", "marginBottom": "4px"}, children=[
                html.Div(style={"width": "7px", "height": "7px",
                                 "borderRadius": "50%", "background": mkt_c}),
                html.P(t(mkt, lang), style={"color": mkt_c, "fontSize": "10px",
                                            "fontWeight": "700", "margin": "0"}),
            ]),
            html.P(datetime.now().strftime("%d %b · %H:%M"),
                   style={"color": MUTED, "fontSize": "10px", "margin": "0"}),
        ]),
    ])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PAGES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def page_overview(auth, lang="en"):
    uid      = int(auth["chat_id"])
    is_admin = auth["is_admin"]

    alerts  = q("SELECT * FROM alerts")
    trades  = q("SELECT * FROM trades") if is_admin else q(f"SELECT * FROM trades WHERE chat_id = {uid}")
    port    = q("SELECT * FROM portfolio") if is_admin else q(f"SELECT * FROM portfolio WHERE chat_id = {uid}")
    scan    = q("SELECT * FROM scan_log")

    a_total = len(alerts)
    a_today = (len(alerts[alerts["alerted_at"].astype(str).str[:10] == today_s()])
               if a_total else 0)
    t_total = len(trades)
    t_wins  = int((trades["result"] == "WIN").sum()) if t_total else 0
    wr      = round(t_wins / t_total * 100, 1) if t_total else 0
    pnl     = round(trades["pnl_dollar"].sum(), 2) if t_total and "pnl_dollar" in trades.columns else 0
    s_total = len(scan)
    s_pass  = int(scan["passed"].sum()) if s_total else 0
    pnl_c   = GREEN if pnl >= 0 else RED

    kpis = kpi_row([
        kpi(t("kpi_alerts_today", lang),  a_today,      f"{a_total} {t('sub_total', lang)}",    ACCENT),
        kpi(t("kpi_win_rate", lang),      f"{wr}%",     f"{t_wins}/{t_total}",                  GREEN),
        kpi(t("kpi_net_pnl", lang),       fmt_usd(pnl), t("sub_all_my_trades", lang),           pnl_c),
        kpi(t("kpi_scan_pass", lang),     f"{round(s_pass/s_total*100,1) if s_total else 0}%",
                                          f"{s_pass} {t('sub_passed', lang)}",                   YELLOW),
        kpi(t("kpi_open_pos", lang),      len(port),    t("sub_my_portfolio", lang),            CYAN),
    ])

    charts = html.Div()
    if a_total:
        alerts["day"] = alerts["alerted_at"].astype(str).str[:10]
        daily = alerts.groupby("day").size().reset_index(name="n").tail(14)
        fig_bar = go.Figure(go.Bar(
            x=daily["day"], y=daily["n"],
            marker=dict(color=ACCENT, line=dict(width=0)),
            hovertemplate="%{x}: <b>%{y}</b><extra></extra>",
        ))
        sess = (alerts.groupby("session").size().reset_index(name="n")
                if "session" in alerts.columns else pd.DataFrame())
        fig_pie = go.Figure()
        if not sess.empty:
            fig_pie.add_trace(go.Pie(
                labels=sess["session"], values=sess["n"], hole=0.55,
                marker=dict(colors=[ACCENT, YELLOW, CYAN, PURPLE]),
                hovertemplate="%{label}: <b>%{value}</b><extra></extra>",
            ))
        charts = html.Div(className="charts-grid", style={"display": "grid",
                                   "gridTemplateColumns": "2fr 1fr",
                                   "gap": "12px", "marginBottom": "16px"}, children=[
            card([sec(t("sec_alert_activity", lang)), chart(fig_bar, 240)], mb="0"),
            card([sec(t("sec_by_session", lang)),     chart(fig_pie, 240)], mb="0"),
        ])

    my_trades = q(f"SELECT * FROM trades WHERE chat_id = {uid} ORDER BY closed_at DESC")
    cols = [c for c in ["symbol","entry_price","exit_price","qty",
                         "pnl_dollar","pnl_pct","result","trade_date"]
            if c in my_trades.columns]
    recent = card([sec(t("sec_my_recent_trades", lang)),
                   tbl(my_trades[cols].head(10) if cols else my_trades.head(10), lang)])
    return html.Div([kpis, charts, recent])


def page_insights(auth, lang="en"):
    """Pro insights: equity curve + trade stats, per-symbol leaderboard, alert funnel."""
    uid      = int(auth["chat_id"])
    is_admin = auth["is_admin"]
    where    = "1=1" if is_admin else f"chat_id = {uid}"
    tr       = q(f"SELECT * FROM trades WHERE {where} ORDER BY closed_at ASC")

    # ── 1. Trade statistics + equity curve ──────────────────
    if tr.empty or "pnl_dollar" not in tr.columns:
        stats_block = card(empty_msg(t("no_trades_period", lang)))
    else:
        total  = len(tr)
        wins   = int((tr["result"] == "WIN").sum())
        wr     = wins / total if total else 0
        pnl_s  = tr["pnl_dollar"].fillna(0)
        win_v  = pnl_s[pnl_s > 0]
        loss_v = pnl_s[pnl_s < 0]
        avg_w  = round(win_v.mean(), 2) if len(win_v) else 0
        avg_l  = round(loss_v.mean(), 2) if len(loss_v) else 0
        gw     = win_v.sum()
        gl     = abs(loss_v.sum())
        pf     = round(gw / gl, 2) if gl else (99.9 if gw else 0)
        expc   = round(wr * avg_w + (1 - wr) * avg_l, 2)
        best   = round(pnl_s.max(), 2)
        worst  = round(pnl_s.min(), 2)
        # streaks (chronological)
        longest, run, cur = 0, 0, 0
        for r in tr["result"]:
            if r == "WIN": run += 1; longest = max(longest, run)
            else:          run = 0
        for r in reversed(list(tr["result"])):
            if r == "WIN": cur += 1
            else:          break

        kpis = kpi_row([
            kpi(t("kpi_profit_factor", lang), f"{pf}×",       t("sub_gross_wl", lang),  YELLOW),
            kpi(t("kpi_expectancy", lang),    fmt_usd(expc),  t("sub_per_trade", lang), ACCENT if expc >= 0 else RED),
            kpi(t("kpi_avg_win", lang),       fmt_usd(avg_w), t("sub_per_winner", lang), GREEN),
            kpi(t("kpi_avg_loss", lang),      fmt_usd(avg_l), t("sub_per_loser", lang),  RED),
            kpi(t("kpi_best_trade", lang),    fmt_usd(best),  "",                        GREEN),
            kpi(t("kpi_worst_trade", lang),   fmt_usd(worst), "",                        RED),
            kpi(t("kpi_win_streak", lang),    f"{cur}",       f"{t('sub_max', lang)} {longest}", PURPLE),
        ])

        cum = pnl_s.cumsum()
        xv  = (tr["closed_at"].astype(str).str[:16].tolist()
               if "closed_at" in tr.columns else list(range(1, total + 1)))
        fig_eq = go.Figure(go.Scatter(
            x=xv, y=cum.tolist(), mode="lines",
            line=dict(color=ACCENT, width=2), fill="tozeroy", fillcolor=ACCENT_FILL,
            hovertemplate="%{x}<br><b>%{y:+,.2f}</b><extra></extra>",
        ))
        fig_eq.add_hline(y=0, line_color=BORDER)
        stats_block = html.Div([
            kpis,
            card([sec(t("sec_equity_curve", lang)), chart(fig_eq, 300)]),
        ])

    # ── 2. Per-symbol leaderboard ───────────────────────────
    if tr.empty or "symbol" not in tr.columns or "pnl_dollar" not in tr.columns:
        leaderboard = html.Div()
    else:
        g = (tr.assign(_win=(tr["result"] == "WIN").astype(int))
               .groupby("symbol")
               .agg(pnl=("pnl_dollar", "sum"), n=("pnl_dollar", "size"), w=("_win", "sum"))
               .reset_index().sort_values("pnl", ascending=False))
        g["winrate"] = (g["w"] / g["n"] * 100).round(0)

        fig_lb = go.Figure(go.Bar(
            y=g["symbol"], x=g["pnl"], orientation="h",
            marker_color=[GREEN if v >= 0 else RED for v in g["pnl"]],
            text=[fmt_usd(v) for v in g["pnl"]],
            textposition="outside", textfont=dict(color=TEXT),
        ))
        fig_lb.update_layout(yaxis=dict(autorange="reversed"),
                             xaxis=dict(zeroline=True, zerolinecolor=BORDER))

        head = html.Tr([
            html.Th(h, style={"padding": "9px 12px", "background": BG, "color": MUTED,
                              "fontSize": "10px", "fontWeight": "700", "letterSpacing": "1px",
                              "textTransform": "uppercase", "borderBottom": f"2px solid {ACCENT}"})
            for h in [t("th_symbol", lang), t("th_trades", lang),
                      t("th_winrate", lang), t("th_net_pnl", lang)]
        ])
        rows = []
        for i, (_, r) in enumerate(g.head(50).iterrows()):
            bg = SURFACE if i % 2 == 0 else SURF2
            pc = GREEN if r["pnl"] >= 0 else RED
            rows.append(html.Tr([
                html.Td(html.B(r["symbol"]), style={"padding": "7px 12px", "background": bg,
                        "color": WHITE, "borderBottom": f"1px solid {BORDER}", "fontSize": "12px"}),
                html.Td(f"{int(r['n'])} ({int(r['w'])}W)", style={"padding": "7px 12px", "background": bg,
                        "color": TEXT, "borderBottom": f"1px solid {BORDER}", "fontSize": "12px"}),
                html.Td(f"{r['winrate']:.0f}%", style={"padding": "7px 12px", "background": bg,
                        "color": TEXT, "borderBottom": f"1px solid {BORDER}", "fontSize": "12px"}),
                html.Td(fmt_usd(r["pnl"]), style={"padding": "7px 12px", "background": bg,
                        "color": pc, "fontWeight": "700", "borderBottom": f"1px solid {BORDER}", "fontSize": "12px"}),
            ]))
        lb_table = html.Div(style={"overflowX": "auto", "borderRadius": "8px",
                                   "border": f"1px solid {BORDER}"},
                            children=[html.Table([html.Thead(head), html.Tbody(rows)],
                                      style={"width": "100%", "borderCollapse": "collapse"})])
        leaderboard = html.Div(className="charts-grid", style={"display": "grid",
                                "gridTemplateColumns": "1fr 1fr", "gap": "12px",
                                "marginBottom": "16px"}, children=[
            card([sec(t("sec_pnl_by_symbol", lang)), chart(fig_lb, 320)], mb="0"),
            card([sec(t("sec_symbol_leaderboard", lang)), lb_table], mb="0"),
        ])

    # ── 3. Alert quality funnel ─────────────────────────────
    scan = q("SELECT * FROM scan_log")
    al   = q("SELECT * FROM alerts")
    scanned = len(scan)
    passed  = int(scan["passed"].sum()) if scanned and "passed" in scan.columns else 0
    alerted = len(al)
    worked  = (int((al["outcome"] == "PASS").sum())
               if alerted and "outcome" in al.columns else 0)

    fig_fun = go.Figure(go.Funnel(
        y=[t("funnel_scanned", lang), t("funnel_passed", lang),
           t("funnel_alerted", lang), t("funnel_worked", lang)],
        x=[scanned, passed, alerted, worked],
        marker=dict(color=[MUTED, ACCENT, YELLOW, GREEN]),
        textinfo="value+percent initial",
    ))

    # Grade A vs B  (avg % move after alert)
    fig_grade = go.Figure()
    if alerted and "grade" in al.columns and "pct_after_alert" in al.columns:
        gp = (al.dropna(subset=["pct_after_alert"])
                .groupby("grade")["pct_after_alert"].mean().reset_index())
        if not gp.empty:
            gc = {"A": GREEN, "B": ACCENT, "C": MUTED}
            fig_grade.add_trace(go.Bar(
                x=gp["grade"], y=gp["pct_after_alert"].round(2),
                marker_color=[gc.get(g, ACCENT) for g in gp["grade"]],
                text=[f"{v:+.1f}%" for v in gp["pct_after_alert"]],
                textposition="outside", textfont=dict(color=TEXT)))

    funnel = html.Div(className="charts-grid", style={"display": "grid",
                       "gridTemplateColumns": "1fr 1fr", "gap": "12px",
                       "marginBottom": "16px"}, children=[
        card([sec(t("sec_alert_funnel", lang)), chart(fig_fun, 320)], mb="0"),
        card([sec(t("sec_grade_perf", lang)),   chart(fig_grade, 320)], mb="0"),
    ])

    return html.Div([stats_block, leaderboard, funnel])


def page_alerts(auth, lang="en"):
    df       = q("SELECT * FROM alerts ORDER BY alerted_at DESC")
    total    = len(df)
    today_df = df[df["alerted_at"].astype(str).str[:10] == today_s()] if total else pd.DataFrame()
    today_n  = len(today_df)
    avg_ch   = round(df["change_pct"].mean(), 1) if total and "change_pct" in df.columns else 0
    perf_df  = df[df["outcome"].notna()] if "outcome" in df.columns else pd.DataFrame()
    p_total  = len(perf_df)
    p_pass   = int((perf_df["outcome"] == "PASS").sum()) if p_total else 0
    p_rate   = round(p_pass / p_total * 100, 1) if p_total else 0

    kpis = kpi_row([
        kpi(t("kpi_total_alerts", lang), total,        t("sub_all_time", lang),                          ACCENT),
        kpi(t("kpi_today", lang),        today_n,      t("sub_new_today", lang),                         CYAN),
        kpi(t("kpi_avg_change", lang),   f"{avg_ch}%", t("sub_per_alert", lang),                         YELLOW),
        kpi(t("kpi_bot_win", lang),      f"{p_rate}%", f"{p_pass}/{p_total} {t('sub_alerts_worked', lang)}", GREEN),
    ])

    charts = html.Div()
    if total:
        by_grade = (df.groupby("grade").size().reset_index(name="n")
                    if "grade" in df.columns else pd.DataFrame())
        by_sess  = (df.groupby("session").size().reset_index(name="n")
                    if "session" in df.columns else pd.DataFrame())
        top_syms = (df.groupby("symbol").size().reset_index(name="n").nlargest(10, "n")
                    if "symbol" in df.columns else pd.DataFrame())
        fig_g, fig_s, fig_t = go.Figure(), go.Figure(), go.Figure()
        gc = {"A": GREEN, "B": ACCENT, "C": MUTED}
        if not by_grade.empty:
            fig_g.add_trace(go.Bar(x=by_grade["grade"], y=by_grade["n"],
                marker_color=[gc.get(g, ACCENT) for g in by_grade["grade"]],
                text=by_grade["n"], textposition="outside", textfont=dict(color=TEXT)))
        if not by_sess.empty:
            fig_s.add_trace(go.Bar(x=by_sess["session"], y=by_sess["n"],
                marker_color=YELLOW,
                text=by_sess["n"], textposition="outside", textfont=dict(color=TEXT)))
        if not top_syms.empty:
            fig_t.add_trace(go.Bar(y=top_syms["symbol"], x=top_syms["n"],
                orientation="h", marker_color=CYAN,
                text=top_syms["n"], textposition="outside", textfont=dict(color=TEXT)))
            fig_t.update_layout(yaxis=dict(autorange="reversed"))
        charts = html.Div(className="charts-grid", style={"display": "grid",
                                   "gridTemplateColumns": "1fr 1fr 1fr",
                                   "gap": "12px", "marginBottom": "16px"}, children=[
            card([sec(t("sec_by_grade", lang)),   chart(fig_g, 260)], mb="0"),
            card([sec(t("sec_by_session", lang)), chart(fig_s, 260)], mb="0"),
            card([sec(t("sec_top_stocks", lang)), chart(fig_t, 260)], mb="0"),
        ])

    perf_section = html.Div()
    if not today_df.empty and "outcome" in today_df.columns:
        today_perf = today_df[today_df["outcome"].notna()]
        if not today_perf.empty:
            rows = []
            for _, r in today_df.iterrows():
                outcome = r.get("outcome")
                pct_val = r.get("pct_after_alert")
                if outcome == "PASS":
                    badge  = html.Span(t("pass", lang), style={"background": GREEN, "color": "#000",
                        "borderRadius": "4px", "padding": "2px 8px", "fontSize": "11px", "fontWeight": "700"})
                    pct_el = html.Span(f"{pct_val:+.1f}%" if pct_val is not None else "", style={"color": GREEN})
                elif outcome == "FAIL":
                    badge  = html.Span(t("fail", lang), style={"background": RED, "color": WHITE,
                        "borderRadius": "4px", "padding": "2px 8px", "fontSize": "11px", "fontWeight": "700"})
                    pct_el = html.Span(f"{pct_val:+.1f}%" if pct_val is not None else "", style={"color": RED})
                else:
                    badge  = html.Span(t("pending", lang), style={"background": YELLOW, "color": "#000",
                        "borderRadius": "4px", "padding": "2px 8px", "fontSize": "11px", "fontWeight": "700"})
                    pct_el = html.Span("—", style={"color": MUTED})
                rows.append(html.Tr([
                    html.Td(html.B(r.get("symbol", "")), style={"padding": "8px 12px", "color": WHITE}),
                    html.Td(r.get("grade", ""),          style={"padding": "8px 12px", "color": MUTED}),
                    html.Td(f"${float(r['alert_price']):.2f}" if r.get("alert_price") else "—",
                             style={"padding": "8px 12px"}),
                    html.Td(f"${float(r['close_price']):.2f}" if r.get("close_price") is not None
                             and str(r.get("close_price")) not in ("nan","None") else "—",
                             style={"padding": "8px 12px"}),
                    html.Td(pct_el, style={"padding": "8px 12px"}),
                    html.Td(badge,  style={"padding": "8px 12px"}),
                ]))
            perf_table = html.Table(style={"width": "100%", "borderCollapse": "collapse"}, children=[
                html.Thead(html.Tr([
                    html.Th(c, style={"padding": "8px 12px", "color": MUTED, "fontSize": "11px",
                                       "fontWeight": "700", "textAlign": "left",
                                       "borderBottom": f"1px solid {BORDER}", "textTransform": "uppercase"})
                    for c in [t("th_symbol", lang), t("th_grade", lang), t("th_alert_price", lang),
                              t("th_close_price", lang), t("th_change", lang), t("th_outcome", lang)]
                ])),
                html.Tbody(rows),
            ])
            perf_section = card([sec(t("sec_today_alert_perf", lang)), perf_table])

    cols = [c for c in ["symbol","alert_price","grade","change_pct",
                         "float_m","rsi","session","alerted_at","outcome","pct_after_alert"]
            if c in df.columns]
    return html.Div([kpis, perf_section, charts,
                     card([sec(t("sec_all_alerts", lang)),
                           tbl(df[cols].head(200) if cols else df.head(200), lang)])])


_MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun",
                "Jul","Aug","Sep","Oct","Nov","Dec"]

def _render_trades_content(df, is_admin, lang="en"):
    if df.empty:
        return card(empty_msg(t("no_trades_period", lang)))

    total  = len(df)
    wins   = int((df["result"] == "WIN").sum())
    losses = total - wins
    wr     = round(wins / total * 100, 1)
    pnl    = round(df["pnl_dollar"].sum(), 2) if "pnl_dollar" in df.columns else 0
    avg_w  = (round(df.loc[df["result"]=="WIN",  "pnl_dollar"].mean(), 2)
              if wins   and "pnl_dollar" in df.columns else 0)
    avg_l  = (round(df.loc[df["result"]=="LOSS", "pnl_dollar"].mean(), 2)
              if losses and "pnl_dollar" in df.columns else 0)
    g_w    = df.loc[df["pnl_dollar"] > 0, "pnl_dollar"].sum() if "pnl_dollar" in df.columns else 0
    g_l    = abs(df.loc[df["pnl_dollar"] < 0, "pnl_dollar"].sum()) if "pnl_dollar" in df.columns else 1
    pf     = round(g_w / g_l, 2) if g_l else 0
    pnl_c  = GREEN if pnl >= 0 else RED

    kpis = kpi_row([
        kpi(t("kpi_trades", lang),        f"{total}",      f"{wins}W / {losses}L",        ACCENT),
        kpi(t("kpi_win_rate", lang),      f"{wr}%",        t("sub_filtered_trades", lang), GREEN),
        kpi(t("kpi_net_pnl", lang),       fmt_usd(pnl),    t("sub_filtered_trades", lang), pnl_c),
        kpi(t("kpi_profit_factor", lang), f"{pf}×",        t("sub_gross_wl", lang),        YELLOW),
        kpi(t("kpi_avg_win", lang),       fmt_usd(avg_w),  t("sub_per_winner", lang),      GREEN),
        kpi(t("kpi_avg_loss", lang),      fmt_usd(avg_l),  t("sub_per_loser", lang),       RED),
    ])

    sym_pnl = (df.groupby("symbol")["pnl_dollar"].sum()
                 .reset_index().sort_values("pnl_dollar")
               if "pnl_dollar" in df.columns and "symbol" in df.columns
               else pd.DataFrame())
    fig_sym = go.Figure()
    if not sym_pnl.empty:
        fig_sym.add_trace(go.Bar(
            y=sym_pnl["symbol"], x=sym_pnl["pnl_dollar"], orientation="h",
            marker_color=[GREEN if v >= 0 else RED for v in sym_pnl["pnl_dollar"]],
            text=[fmt_usd(v) for v in sym_pnl["pnl_dollar"]],
            textposition="outside", textfont=dict(color=TEXT),
        ))
        fig_sym.update_layout(yaxis=dict(autorange="reversed"),
                               xaxis=dict(zeroline=True, zerolinecolor=BORDER))

    fig_cum = go.Figure()
    if "pnl_dollar" in df.columns:
        cum = df.iloc[::-1]["pnl_dollar"].cumsum().reset_index(drop=True)
        fig_cum.add_trace(go.Scatter(
            x=list(range(1, len(cum)+1)), y=cum, mode="lines+markers",
            line=dict(color=ACCENT, width=2),
            marker=dict(color=[GREEN if v >= 0 else RED for v in cum], size=5),
            fill="tozeroy", fillcolor=ACCENT_FILL,
            hovertemplate="#%{x}<br><b>%{y:+,.2f}</b><extra></extra>",
        ))

    charts = html.Div(className="charts-grid", style={"display": "grid", "gridTemplateColumns": "1fr 1fr",
                               "gap": "12px", "marginBottom": "16px"}, children=[
        card([sec(t("sec_pnl_by_symbol", lang)),  chart(fig_sym, 300)], mb="0"),
        card([sec(t("sec_cumulative_pnl", lang)), chart(fig_cum, 300)], mb="0"),
    ])

    base_cols = ["symbol","entry_price","exit_price","qty",
                 "pnl_dollar","pnl_pct","result","trade_date"]
    show = [c for c in (["user_name"] if is_admin else []) + base_cols if c in df.columns]
    return html.Div([kpis, charts,
                     card([sec(t("sec_trade_history", lang)), tbl(df[show] if show else df, lang)])])


def page_trades(auth, lang="en"):
    uid      = int(auth["chat_id"])
    is_admin = auth["is_admin"]

    # SQLite: DATE() instead of CAST(... AS DATE)
    if is_admin:
        dates_df = q("SELECT DISTINCT DATE(trade_date) AS d FROM trades ORDER BY d DESC")
    else:
        dates_df = q(f"SELECT DISTINCT DATE(trade_date) AS d FROM trades WHERE chat_id = {uid} ORDER BY d DESC")

    today = date.today()
    day_opts, month_opts, months_seen = [], [], []

    if not dates_df.empty:
        for d in dates_df.iloc[:, 0]:
            ds = str(d)[:10]
            day_opts.append({"label": ds, "value": ds})
            ym = ds[:7]
            if ym not in months_seen:
                months_seen.append(ym)
                y, m = ym.split("-")
                month_opts.append({"label": f"{_MONTH_NAMES[int(m)-1]} {y}", "value": ym})

    if not day_opts:
        day_opts = [{"label": str(today), "value": str(today)}]
    if not month_opts:
        ym = str(today)[:7]; y, m = ym.split("-")
        month_opts = [{"label": f"{_MONTH_NAMES[int(m)-1]} {y}", "value": ym}]

    _DD      = {"background": SURF2, "border": f"1px solid {BORDER}",
                "borderRadius": "8px", "color": TEXT, "fontSize": "13px", "minWidth": "140px"}
    _DD_HIDE = {**_DD, "display": "none"}

    filter_bar = html.Div(style={
        "background": SURFACE, "border": f"1px solid {BORDER}",
        "borderRadius": "12px", "padding": "14px 20px",
        "marginBottom": "16px", "display": "flex",
        "alignItems": "center", "gap": "16px", "flexWrap": "wrap",
    }, children=[
        html.Span(t("period", lang), style={"color": MUTED, "fontSize": "11px",
                                    "fontWeight": "700", "letterSpacing": "1px",
                                    "textTransform": "uppercase"}),
        dcc.RadioItems(
            id="trades-filter-type",
            options=[
                {"label": " " + t("all_time_opt", lang), "value": "all"},
                {"label": " " + t("by_day", lang),       "value": "day"},
                {"label": " " + t("by_month", lang),     "value": "month"},
            ],
            value="all", inline=True,
            inputStyle={"marginRight": "5px", "accentColor": ACCENT},
            labelStyle={"marginRight": "20px", "cursor": "pointer",
                        "fontSize": "13px", "color": TEXT},
        ),
        dcc.Dropdown(id="trades-filter-day",   options=day_opts,
                     value=day_opts[0]["value"],   clearable=False, style=_DD_HIDE),
        dcc.Dropdown(id="trades-filter-month", options=month_opts,
                     value=month_opts[0]["value"], clearable=False, style=_DD_HIDE),
    ])
    return html.Div([filter_bar, html.Div(id="trades-body")])


def page_scan(auth, lang="en"):
    df = q("SELECT * FROM scan_log ORDER BY scanned_at DESC")
    if df.empty:
        return card(empty_msg(t("no_scan", lang)))

    total   = len(df)
    passed  = int(df["passed"].sum()) if "passed" in df.columns else 0
    skipped = total - passed
    rate    = round(passed / total * 100, 1) if total else 0
    today_n = (len(df[df["scanned_at"].astype(str).str[:10] == today_s()]) if total else 0)

    kpis = kpi_row([
        kpi(t("kpi_total_scanned", lang), total,   t("sub_all_time", lang),      ACCENT),
        kpi(t("kpi_passed", lang),        passed,  f"{rate}%",                    GREEN),
        kpi(t("kpi_filtered_out", lang),  skipped, t("sub_didnt_qualify", lang),  RED),
        kpi(t("kpi_today", lang),         today_n, t("sub_scanned_today", lang),  YELLOW),
    ])

    skip_df = pd.DataFrame()
    if "skip_reason" in df.columns:
        skip_df = (df[df["passed"] == False]["skip_reason"]
                   .value_counts().head(10).reset_index())
        skip_df.columns = ["reason", "count"]

    fig_skip = go.Figure()
    if not skip_df.empty:
        fig_skip.add_trace(go.Bar(
            y=skip_df["reason"], x=skip_df["count"], orientation="h",
            marker_color=RED, text=skip_df["count"],
            textposition="outside", textfont=dict(color=TEXT),
        ))
        fig_skip.update_layout(yaxis=dict(autorange="reversed"))

    fig_sess = go.Figure()
    if "session" in df.columns and "passed" in df.columns:
        sdf = df.groupby("session")["passed"].agg(["sum","count"]).reset_index()
        sdf.columns = ["session","passed","total"]
        sdf["skipped"] = sdf["total"] - sdf["passed"]
        fig_sess.add_trace(go.Bar(name=t("kpi_passed", lang),   x=sdf["session"],
                                  y=sdf["passed"],  marker_color=GREEN))
        fig_sess.add_trace(go.Bar(name=t("kpi_filtered_out", lang), x=sdf["session"],
                                  y=sdf["skipped"], marker_color=RED))
        fig_sess.update_layout(barmode="stack")

    charts = html.Div(className="charts-grid", style={"display": "grid", "gridTemplateColumns": "3fr 2fr",
                               "gap": "12px", "marginBottom": "16px"}, children=[
        card([sec(t("sec_top_filter_reasons", lang)), chart(fig_skip, 320)], mb="0"),
        card([sec(t("sec_by_session", lang)),          chart(fig_sess, 320)], mb="0"),
    ])

    cols = [c for c in ["symbol","price","change_pct","grade","passed",
                         "skip_reason","session","scanned_at"] if c in df.columns]
    return html.Div([kpis, charts,
                     card([sec(t("sec_recent_scan_log", lang)),
                           tbl(df[cols].head(100) if cols else df.head(100), lang)])])


def page_portfolio(auth, lang="en"):
    uid      = int(auth["chat_id"])
    is_admin = auth["is_admin"]

    if is_admin:
        df = q("""
            SELECT p.*, u.name AS user_name
            FROM portfolio p LEFT JOIN users u ON p.chat_id = u.chat_id
        """)
    else:
        df = q(f"SELECT * FROM portfolio WHERE chat_id = {uid}")

    kpis = kpi_row([
        kpi(t("kpi_open_pos", lang), len(df), t("sub_tracked_by_bot", lang), CYAN),
        kpi(t("kpi_with_shares", lang),
            int((df["qty"].notna()).sum()) if not df.empty and "qty" in df.columns else 0,
            t("sub_qty_recorded", lang), YELLOW),
    ])

    base = ["symbol","entry_price","stop_price","t1_price","t2_price","qty","t1_hit","t2_hit","added_at"]
    show = [c for c in (["user_name"] if is_admin else []) + base
            if not df.empty and c in df.columns]
    return html.Div([kpis, card([sec(t("sec_open_positions", lang)), tbl(df[show] if show else df, lang)])])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  APP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PAGE_MAP = {
    "overview":  ("nav_overview",  page_overview),
    "insights":  ("nav_insights",  page_insights),
    "alerts":    ("nav_alerts",    page_alerts),
    "trades":    ("nav_trades",    page_trades),
    "scanlog":   ("nav_scanlog",   page_scan),
    "portfolio": ("nav_portfolio", page_portfolio),
}

app = Dash(
    __name__, title="StockBot",
    suppress_callback_exceptions=True,
    external_stylesheets=[
        "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap",
        "https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;800&display=swap",
    ],
    meta_tags=[
        # Makes the layout scale to the device width on phones / iPad
        {"name": "viewport",
         "content": "width=device-width, initial-scale=1, viewport-fit=cover"},
        {"name": "mobile-web-app-capable", "content": "yes"},
        {"name": "apple-mobile-web-app-capable", "content": "yes"},
        {"name": "theme-color", "content": "#0A0E1A"},
    ],
)
server = app.server  # exposed for gunicorn / WSGI if needed

# Responsive + RTL overrides. Inline styles outrank stylesheet rules, so the
# mobile media queries use !important to override layout only on small screens —
# desktop rendering is unchanged.
app.index_string = """
<!DOCTYPE html>
<html>
<head>
    {%metas%}
    <title>{%title%}</title>
    {%favicon%}
    {%css%}
    <style>
        html, body { margin: 0; padding: 0; background: #0A0E1A;
                     -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
        * { -webkit-tap-highlight-color: transparent; }
        /* Plotly charts fill their card and resize on rotate */
        .js-plotly-plot, .plot-container, .svg-container { width: 100% !important; }
        .login-card { max-width: 92vw !important; }
        /* Arabic is cursive — letter-spacing breaks letter joining, so kill it in RTL */
        [dir="rtl"] * { letter-spacing: normal !important; }

        /* Phones + iPad portrait */
        @media (max-width: 900px) {
            .app-shell    { flex-direction: column !important; min-height: auto !important; }
            .app-sidebar  { width: 100% !important; min-width: 0 !important;
                            height: auto !important; position: static !important;
                            flex-direction: row !important; align-items: center !important;
                            flex-wrap: wrap !important; border-right: none !important;
                            border-bottom: 1px solid #1F2D45 !important; }
            .sidebar-hide-mobile { display: none !important; }
            .nav-links    { flex-direction: row !important; flex: 1 1 100% !important;
                            overflow-x: auto !important; padding: 6px 8px !important;
                            -webkit-overflow-scrolling: touch !important; }
            .nav-links a  { border-right: none !important; white-space: nowrap !important;
                            padding: 10px 14px !important; }
            .charts-grid  { grid-template-columns: 1fr !important; }
            .content-pad  { padding: 16px 14px !important; }
            .topbar       { padding: 12px 14px !important; flex-wrap: wrap !important;
                            gap: 10px !important; }
        }
        /* Small phones */
        @media (max-width: 420px) {
            .login-card { width: 100% !important; padding: 28px 22px !important;
                          border-radius: 12px !important; }
            .topbar h2  { font-size: 14px !important; }
        }
    </style>
</head>
<body>
    {%app_entry%}
    <footer>
        {%config%}
        {%scripts%}
        {%renderer%}
    </footer>
</body>
</html>
"""

_SHOW_LOGIN = {"display": "flex", "alignItems": "center",
               "justifyContent": "center", "minHeight": "100vh"}
_HIDE       = {"display": "none"}
_SHOW_DASH  = {"display": "block"}

def _lang_btn_style():
    return {"background": "transparent", "border": f"1px solid {BORDER}",
            "borderRadius": "6px", "padding": "6px 12px", "color": TEXT,
            "fontSize": "11px", "fontWeight": "700", "cursor": "pointer"}

app.layout = html.Div(
    id="root-container", dir="ltr",
    style={"backgroundColor": BG, "fontFamily": FONT_STACK,
           "color": TEXT, "minHeight": "100vh"},
    children=[
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="auth", storage_type="local"),
        dcc.Store(id="lang", storage_type="local"),
        dcc.Interval(id="tick", interval=30_000),

        html.Div(id="login-section", style=_SHOW_LOGIN, children=[
            html.Div(className="login-card", style={
                "width": "380px", "background": SURFACE,
                "border": f"1px solid {BORDER}",
                "borderRadius": "16px", "padding": "40px",
            }, children=[
                html.Div(style={"display": "flex", "justifyContent": "flex-end",
                                "marginBottom": "8px"}, children=[
                    html.Button(t("lang_switch", "en"), id="lang-toggle-login",
                                n_clicks=0, style=_lang_btn_style()),
                ]),
                html.Div(style={"textAlign": "center", "marginBottom": "32px"}, children=[
                    html.Div("↗", style={
                        "width": "52px", "height": "52px", "background": ACCENT,
                        "borderRadius": "12px",
                        "display": "inline-flex", "alignItems": "center",
                        "justifyContent": "center", "fontSize": "24px",
                        "fontWeight": "800", "color": WHITE, "marginBottom": "14px",
                    }),
                    html.H2("StockBot", style={"color": WHITE, "margin": "0 0 4px 0",
                                                "fontSize": "22px", "fontWeight": "800"}),
                    html.P(t("sign_in_sub", "en"), id="login-sub",
                           style={"color": MUTED, "margin": 0, "fontSize": "13px"}),
                ]),
                html.Label(t("username", "en"), id="login-user-label",
                    style={"color": MUTED, "fontSize": "10px", "fontWeight": "700",
                    "letterSpacing": "1px", "textTransform": "uppercase",
                    "display": "block", "marginBottom": "6px"}),
                dcc.Input(id="login-user", type="text", placeholder=t("username_ph", "en"),
                          debounce=False, n_submit=0, style=_INPUT),
                html.Label(t("pin", "en"), id="login-pin-label",
                    style={"color": MUTED, "fontSize": "10px", "fontWeight": "700",
                    "letterSpacing": "1px", "textTransform": "uppercase",
                    "display": "block", "marginBottom": "6px"}),
                dcc.Input(id="login-pin", type="password", placeholder=t("pin_ph", "en"),
                          debounce=False, n_submit=0, style=_INPUT),
                html.Button(t("sign_in", "en"), id="login-btn", n_clicks=0, style={
                    "width": "100%", "background": ACCENT, "color": WHITE,
                    "border": "none", "borderRadius": "8px", "padding": "13px",
                    "fontSize": "14px", "fontWeight": "700",
                    "cursor": "pointer", "letterSpacing": "0.5px", "marginBottom": "14px",
                }),
                html.Div(id="login-error", style={"color": RED, "fontSize": "12px",
                                                   "textAlign": "center", "minHeight": "18px"}),
                html.P(t("login_hint", "en"), id="login-hint",
                       style={"color": MUTED, "fontSize": "11px", "textAlign": "center",
                              "marginTop": "20px", "marginBottom": 0,
                              "borderTop": f"1px solid {BORDER}", "paddingTop": "16px"}),
            ]),
        ]),

        html.Div(id="dash-wrap", style=_HIDE, children=[
            html.Div(className="app-shell", style={"display": "flex", "minHeight": "100vh"}, children=[
                html.Div(id="sidebar-wrap"),
                html.Div(style={"flex":"1","display":"flex",
                                 "flexDirection":"column","minWidth":0}, children=[
                    html.Div(className="topbar", style={
                        "background": SURFACE, "borderBottom": f"1px solid {BORDER}",
                        "padding": "13px 28px", "display": "flex",
                        "alignItems": "center", "justifyContent": "space-between",
                    }, children=[
                        html.H2(id="page-title", style={"color": WHITE, "margin": 0,
                                                         "fontSize": "15px", "fontWeight": "700"}),
                        html.Div(style={"display":"flex","alignItems":"center","gap":"14px"}, children=[
                            html.Div(id="mkt-badge"),
                            html.Div(id="user-badge"),
                            html.Button(t("lang_switch", "en"), id="lang-toggle",
                                        n_clicks=0, style=_lang_btn_style()),
                            html.Button(t("sign_out", "en"), id="logout-btn", n_clicks=0, style={
                                "background": "transparent",
                                "border": f"1px solid {BORDER}",
                                "borderRadius": "6px", "padding": "6px 14px",
                                "color": MUTED, "fontSize": "11px", "cursor": "pointer",
                            }),
                        ]),
                    ]),
                    html.Div(id="page-content", className="content-pad",
                             style={"flex":"1","padding":"24px 28px","overflowY":"auto"}),
                ]),
            ]),
        ]),
    ]
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CALLBACKS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Language toggle (either button flips the stored language)
@app.callback(
    Output("lang", "data"),
    Input("lang-toggle",       "n_clicks"),
    Input("lang-toggle-login", "n_clicks"),
    State("lang", "data"),
    prevent_initial_call=True,
)
def toggle_lang(_n1, _n2, cur):
    cur = cur if cur in TR else "en"
    return "ar" if cur == "en" else "en"


# Apply language to the chrome that lives outside the router (dir, login texts, buttons)
@app.callback(
    Output("root-container",    "dir"),
    Output("lang-toggle",       "children"),
    Output("lang-toggle-login", "children"),
    Output("logout-btn",        "children"),
    Output("login-sub",         "children"),
    Output("login-user-label",  "children"),
    Output("login-user",        "placeholder"),
    Output("login-pin-label",   "children"),
    Output("login-pin",         "placeholder"),
    Output("login-btn",         "children"),
    Output("login-hint",        "children"),
    Input("lang", "data"),
)
def apply_language(lang):
    lang = lang if lang in TR else "en"
    direction = "rtl" if lang == "ar" else "ltr"
    return (
        direction,
        t("lang_switch", lang), t("lang_switch", lang),
        t("sign_out", lang),
        t("sign_in_sub", lang),
        t("username", lang), t("username_ph", lang),
        t("pin", lang), t("pin_ph", lang),
        t("sign_in", lang),
        t("login_hint", lang),
    )


@app.callback(
    Output("trades-body",         "children"),
    Output("trades-filter-day",   "style"),
    Output("trades-filter-month", "style"),
    Input("trades-filter-type",   "value"),
    Input("trades-filter-day",    "value"),
    Input("trades-filter-month",  "value"),
    Input("auth", "data"),
    Input("lang", "data"),
    prevent_initial_call=False,
)
def update_trades_content(filter_type, sel_day, sel_month, auth, lang):
    if not auth or not auth.get("chat_id"):
        return no_update, no_update, no_update
    lang = lang if lang in TR else "en"

    uid      = int(auth["chat_id"])
    is_admin = auth["is_admin"]

    _DD      = {"background": SURF2, "border": f"1px solid {BORDER}",
                "borderRadius": "8px", "color": TEXT, "fontSize": "13px", "minWidth": "140px"}
    _DD_HIDE = {**_DD, "display": "none"}
    day_style   = _DD if filter_type == "day"   else _DD_HIDE
    month_style = _DD if filter_type == "month" else _DD_HIDE

    base_where   = f"t.chat_id = {uid}" if not is_admin else "1=1"
    period_where = "1=1"
    if filter_type == "day" and sel_day:
        period_where = f"DATE(t.trade_date) = '{sel_day}'"
    elif filter_type == "month" and sel_month:
        y, m = sel_month.split("-")
        period_where = f"strftime('%Y', t.trade_date) = '{y}' AND strftime('%m', t.trade_date) = '{int(m):02d}'"

    if is_admin:
        df = q(f"""
            SELECT t.*, u.name AS user_name
            FROM trades t LEFT JOIN users u ON t.chat_id = u.chat_id
            WHERE ({base_where}) AND ({period_where})
            ORDER BY t.closed_at DESC
        """)
    else:
        df = q(f"""
            SELECT * FROM trades t
            WHERE ({base_where}) AND ({period_where})
            ORDER BY t.closed_at DESC
        """)

    return _render_trades_content(df, is_admin, lang), day_style, month_style


@app.callback(
    Output("login-section", "style"),
    Output("dash-wrap",     "style"),
    Output("sidebar-wrap",  "children"),
    Output("page-title",    "children"),
    Output("mkt-badge",     "children"),
    Output("user-badge",    "children"),
    Output("page-content",  "children"),
    Input("url",   "pathname"),
    Input("auth",  "data"),
    Input("lang",  "data"),
    Input("tick",  "n_intervals"),
)
def route(pathname, auth, lang, _):
    if not auth or not auth.get("chat_id"):
        return _SHOW_LOGIN, _HIDE, [], "", "", "", ""
    lang = lang if lang in TR else "en"

    page = (pathname or "/").strip("/") or "overview"
    if page not in PAGE_MAP:
        page = "overview"

    title_key, fn = PAGE_MAP[page]
    content   = fn(auth, lang)
    sb        = sidebar(page, auth, lang)

    mkt, mkt_c = market_status()
    mkt_el = html.Div(style={"display":"flex","alignItems":"center","gap":"6px"}, children=[
        html.Div(style={"width":"7px","height":"7px","borderRadius":"50%","background":mkt_c}),
        html.Span(t(mkt, lang), style={"color":mkt_c,"fontSize":"11px","fontWeight":"600"}),
    ])
    user_el = html.Div(style={"display":"flex","alignItems":"center","gap":"6px"}, children=[
        html.Span("👑 " if auth["is_admin"] else "", style={"fontSize":"12px"}),
        html.Span(auth["name"], style={"color":TEXT,"fontSize":"12px","fontWeight":"600"}),
    ])
    return _HIDE, _SHOW_DASH, sb, t(title_key, lang), mkt_el, user_el, content


@app.callback(
    Output("auth",        "data"),
    Output("login-error", "children"),
    Input("login-btn",    "n_clicks"),
    Input("login-pin",    "n_submit"),
    State("login-user",   "value"),
    State("login-pin",    "value"),
    State("lang",         "data"),
    prevent_initial_call=True,
)
def do_login(n_btn, n_enter, username, pin, lang):
    user = check_login(username or "", pin or "")
    if user:
        return user, ""
    return no_update, t("login_error", lang if lang in TR else "en")


@app.callback(
    Output("auth", "data", allow_duplicate=True),
    Input("logout-btn", "n_clicks"),
    prevent_initial_call=True,
)
def do_logout(_):
    return {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RUN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ============================================================================
#  SINGLE-FILE ENTRYPOINT  —  dashboard in a thread, bot in the foreground
# ============================================================================
def start_dashboard():
    """Run the Dash web app (binds Railway's $PORT, else 8050)."""
    try:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8050)), debug=False)
    except Exception as e:
        log.warning(f"[dashboard] stopped: {e}")


if __name__ == "__main__":
    setup_db()  # create tables if missing (idempotent)
    threading.Thread(target=start_dashboard, daemon=True, name="dashboard").start()
    main()      # bot scanner loop (blocks, keeps the process alive)
