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
                chat_id     INTEGER PRIMARY KEY,
                name        TEXT,
                username    TEXT,
                is_active   INTEGER DEFAULT 1,
                is_admin    INTEGER DEFAULT 0,
                pin         TEXT    DEFAULT '1234',
                accepted    INTEGER DEFAULT 0,
                accepted_at TEXT,
                joined_at   DATETIME DEFAULT (datetime('now'))
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


def migrate_db():
    """One-off data fixes, safe to run on every startup."""
    conn = get_conn()
    if not conn:
        return
    try:
        # Breakeven trades (entry == exit, 0% P&L) were mislabeled LOSS.
        cur = conn.execute(
            "UPDATE trades SET result='FLAT' "
            "WHERE result='LOSS' AND (pnl_pct = 0 OR entry_price = exit_price)")
        if cur.rowcount:
            log.info(f"[DB] migrated {cur.rowcount} breakeven trade(s) LOSS→FLAT")
        # Trades from a SELL with no prior BUY had entry_price=0 → nan P&L and
        # false LOSS rows. Remove them so they don't corrupt the stats.
        cur2 = conn.execute(
            "DELETE FROM trades WHERE entry_price = 0 OR entry_price IS NULL")
        if cur2.rowcount:
            log.info(f"[DB] removed {cur2.rowcount} corrupt 0-entry trade(s)")
        # Persist disclaimer acceptance across restarts — older DBs lack these
        # columns, so the 'accepted' flag was lost on every redeploy and users
        # had to re-accept. Add the columns; mark all EXISTING users accepted
        # (they were already using the bot → they had accepted before).
        for col, decl in (("accepted", "INTEGER DEFAULT 0"), ("accepted_at", "TEXT")):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
                log.info(f"[DB] added users.{col} column")
                if col == "accepted":
                    conn.execute("UPDATE users SET accepted=1")
            except Exception:
                pass   # column already exists
        conn.commit()
    except Exception as e:
        log.error(f"[DB] migrate: {e}")
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
        migrate_db()
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
        cur.execute("SELECT chat_id, name, username, is_active, is_admin, accepted, accepted_at, joined_at FROM users")
        result = {}
        for row in cur.fetchall():
            uid = str(row["chat_id"])
            result[uid] = {
                "name":        row["name"]     or uid,
                "username":    row["username"] or "",
                "active":      bool(row["is_active"]),
                "is_admin":    bool(row["is_admin"]),
                "accepted":    bool(row["accepted"]),
                "accepted_at": row["accepted_at"] or "",
                "added":       str(row["joined_at"])[:10] if row["joined_at"] else "",
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


def db_get_pin(chat_id: str) -> str:
    """Return the user's dashboard PIN, or '1234' if unset / DB unavailable."""
    conn = get_conn()
    if not conn:
        return "1234"
    try:
        cur = conn.execute("SELECT pin FROM users WHERE chat_id=?", (int(chat_id),))
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else "1234"
    except Exception as e:
        log.error(f"[DB] get_pin {chat_id}: {e}")
        return "1234"
    finally:
        conn.close()


def db_sync_users(users_dict: dict):
    for uid, u in users_dict.items():
        db_upsert_user(uid, u.get("name", uid), u.get("username", ""),
                       u.get("active", True), u.get("is_admin", False))


def db_set_accepted(chat_id: str, accepted_at: str):
    """Persist disclaimer acceptance so it survives restarts/redeploys."""
    conn = get_conn()
    if not conn:
        return
    try:
        conn.execute("UPDATE users SET accepted=1, accepted_at=? WHERE chat_id=?",
                     (accepted_at, int(chat_id)))
        conn.commit()
    except Exception as e:
        log.error(f"[DB] set_accepted {chat_id}: {e}")
    finally:
        conn.close()


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
            result  = "WIN" if pnl_p > 0 else ("FLAT" if pnl_p == 0 else "LOSS")
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
            result  = "WIN" if pnl_p > 0 else ("FLAT" if pnl_p == 0 else "LOSS")
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
DASHBOARD_URL  = os.environ.get(
    "DASHBOARD_URL", "https://worker-production-5710.up.railway.app/overview")
USERS_FILE     = os.path.join(BASE_DIR, "bot_users.json")
PORTFOLIO_FILE = os.path.join(BASE_DIR, "portfolio.json")
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")
ALERTED_FILE   = os.path.join(BASE_DIR, "alerted.json")
TRACKED_FILE   = os.path.join(BASE_DIR, "tracked.json")
LOG_FILE       = os.path.join(BASE_DIR, "scanner.log")

MIN_PRICE       = 1.0
MAX_PRICE       = 50.0
SCAN_EVERY_MIN  = 1      # scan every minute — faster reaction (was 2)
ALERT_COOLDOWN  = 1800    # seconds before same stock can re-alert

# Alert outcome simulation — how a real trade on the alert would have gone.
ALERT_T1_PCT    = 0.05    # first target  → PASS (+5% within the window)
ALERT_T2_PCT    = 0.10    # second target → PASS (+10%, bigger)
ALERT_STOP_PCT  = 0.07    # stop loss     → FAIL
ALERT_OPEN_MIN  = 30      # minutes to hit +5%; no target hit by then → FAIL
MOMENTUM_ALERTS = False   # separate "high-risk momentum" channel for big runners the
                          # strict filter rejects (unverified pumps). OFF by default —
                          # admin can enable live with /momentum on.
INFLOW_REQUIRED = True    # when ON, only alert stocks with confirmed inflow (OBV↑) +
                          # volume surge, caught earlier (lower change bar) — strict/
                          # high-quality. Default ON per user. /inflow off = simple/more.
MOMENTUM_MIN_FROM_HIGH = 0.50  # skip if price has collapsed below this fraction of day high
SCAN_WORKERS    = 10      # stocks fetched in parallel
MAX_CANDIDATES  = 40      # max stocks enriched per scan (screener can return many)
# ── ORB (Opening Range Breakout) — separate fast 1-minute module ──
ORB_RANGE_MIN   = 15      # opening range = high/low of the first 15 minutes
ORB_WINDOW_END  = 11*60   # stop hunting breakouts after 11:00 ET (90 min post-open)
ORB_MIN_RVOL    = 2.0     # breakout bar must have >= this x the opening-range avg volume
orb_alerted     = set()   # symbols already ORB-alerted today (cleared in reset_daily)
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
    "over-allotment", "overallotment", "over allotment", "greenshoe", "green shoe",
    "underwriters", "underwriter exercise", "exercise of the option",
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
momentum_alerted = {}              # symbol → unix timestamp (high-risk channel cooldown)
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
    uid = str(uid)
    if uid == ADMIN_ID:          # primary admin — always, can't be revoked
        return True
    return bool(users.get(uid, {}).get("is_admin", False))

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

def set_admin(uid: str, make: bool) -> bool:
    """Grant/revoke admin on a user. Primary ADMIN_ID can't be changed.
    Returns False if the user is unknown or is the primary admin."""
    uid = str(uid)
    if uid == ADMIN_ID:
        return False
    with _lock:
        if uid not in users:
            return False
        users[uid]["is_admin"] = make
        if make:
            users[uid]["active"] = True
    save_users()
    return True

def get_active_users() -> list[str]:
    return [uid for uid, u in users.items() if u.get("active", False)]


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════

def dash_button():
    """Inline keyboard with a tap-to-open Dashboard link button."""
    return {"inline_keyboard": [[{"text": "📊 Open Dashboard", "url": DASHBOARD_URL}]]}

def send_to(uid: str, text: str, reply_markup=None) -> bool:
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": str(uid), "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, data=data, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.warning(f"send_to({uid}) failed: {e}")
        return False

def send_document(uid: str, filepath: str, caption: str = "") -> bool:
    """Upload a file to a Telegram chat — used for DB/state backups."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    try:
        with open(filepath, "rb") as fh:
            files = {"document": (os.path.basename(filepath), fh)}
            data  = {"chat_id": str(uid)}
            if caption:
                data["caption"] = caption
            r = requests.post(url, data=data, files=files, timeout=60)
            return r.status_code == 200
    except Exception as e:
        log.warning(f"send_document({uid}, {filepath}) failed: {e}")
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

def fetch_chat_info(uid: str) -> tuple:
    """Look up a user's current first_name + @username via Telegram getChat.
    Works for anyone who has ever started the bot — no need to wait for them
    to send a message. Returns (name, username); either may be '' if unknown."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat"
    try:
        r = requests.get(url, params={"chat_id": str(uid)}, timeout=8)
        if r.status_code == 200:
            res = r.json().get("result", {})
            return (res.get("first_name") or res.get("title") or "",
                    res.get("username") or "")
    except Exception as e:
        log.debug(f"getChat({uid}) failed: {e}")
    return ("", "")

def refresh_user_info() -> int:
    """Fill in name/@username for any user missing them, via getChat.
    Returns the number of records updated."""
    updated = 0
    for uid in list(users.keys()):
        u = users.get(uid, {})
        has_name  = u.get("name") and str(u.get("name")) != str(uid)
        has_uname = bool(u.get("username"))
        if has_name and has_uname:
            continue   # already complete — skip the network call
        name, uname = fetch_chat_info(uid)
        with _lock:
            if uid not in users:
                continue
            changed = False
            if name and str(users[uid].get("name") or "") in ("", str(uid)):
                users[uid]["name"] = name
                changed = True
            if uname and users[uid].get("username") != uname:
                users[uid]["username"] = uname
                changed = True
        if changed:
            updated += 1
    if updated:
        save_users()
    return updated


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

def _parse_price(s) -> float | None:
    """Tolerant price parser for chat commands. Accepts '3.3', '$3.3', '3,3', '3.3$'.
    Returns a positive float, or None if it can't be parsed."""
    try:
        v = float(str(s).replace("$", "").replace(",", ".").replace(" ", "").strip())
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None

def _parse_qty(s) -> int | None:
    """Tolerant share-quantity parser. Accepts '50', '50.0', '1,000', '50shares'.
    Returns a positive int, or None if it can't be parsed."""
    try:
        cleaned = str(s).lower().replace(",", "").replace("shares", "").replace("share", "").strip()
        v = int(float(cleaned))
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None

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


def fetch_screener_webull(f: dict, limit: int = 50) -> list[dict]:
    """Full-universe screener via Webull — EVERY US stock matching the price
    and change filters, not just a pre-made top-gainers list. Returns the same
    shape as fetch_gainers: [{symbol, change, price, volume}].

    Note: the screener filters on REGULAR-session change, so it's the broad net
    for the OPEN session. PRE/AFTER movers are still caught by fetch_gainers."""
    try:
        min_chg = (f.get("min_change", 0) or 0) / 100.0
        body = {
            "fetch": limit,
            "rules": {
                "wlas.screener.rule.region":      "securities.region.name.6",
                "wlas.screener.rule.lastPrice":   f"gte={MIN_PRICE}&lte={MAX_PRICE}",
                "wlas.screener.rule.changeRatio": f"gte={min_chg}",
            },
            "sort":   {"rule": "wlas.screener.rule.changeRatio", "desc": True},
            "attach": {"hkexPrivilege": False},
        }
        with _wb_semaphore:
            r = _wb_http.post(
                "https://quotes-gw.webullfintech.com/api/wlas/screener/query",
                json=body, timeout=15,
            )
        if r.status_code != 200:
            log.warning(f"Screener HTTP {r.status_code}")
            return []
        items  = r.json().get("items", [])
        stocks = []
        for it in items:
            t   = it.get("ticker", {})
            sym = t.get("symbol")
            if not sym:
                continue
            try:
                price  = float(t.get("close"))
                change = float(t.get("changeRatio", 0)) * 100
                volume = float(t.get("volume", 0) or 0)
            except (TypeError, ValueError):
                continue
            stocks.append({"symbol": sym, "change": round(change, 2),
                           "price": price, "volume": volume})
        return stocks
    except Exception as e:
        log.error(f"Screener fetch error: {e}")
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


def calc_ema(series: list, period: int) -> list | None:
    """Exponential moving average over an oldest-first series. None if too short."""
    if not series or len(series) < period:
        return None
    k   = 2 / (period + 1)
    ema = series[0]
    out = [ema]
    for x in series[1:]:
        ema = x * k + ema * (1 - k)
        out.append(ema)
    return out


def calc_ignition(closes: list, vols: list,
                  vwap: float | None, price: float | None) -> dict:
    """
    Is an intraday move *igniting* — real and just starting — or extended/fading?
    Computed entirely from the m5 bars already fetched, so no extra API calls.

    All inputs are oldest-first. Returns:
      vwap_hold : price + last 2 closes above VWAP (buyers in control)
      vol_surge : last-15-min avg volume ÷ prior-30-min avg volume   (x)
      accel_pct : % price change over the last ~25 min (5 bars)
      ema_up    : price above a rising 9-period EMA (trend, not chop)
      score     : 0–4 — how many of the four signals are firing
      tags      : short labels for the alert message
    """
    out = {"vwap_hold": False, "vol_surge": 0.0, "accel_pct": 0.0,
           "ema_up": False, "score": 0, "tags": []}
    if not closes or len(closes) < 6 or not price:
        return out

    # 1. VWAP reclaim / hold — price and the last two closes above VWAP.
    if vwap and vwap > 0:
        out["vwap_hold"] = price > vwap and closes[-1] > vwap and closes[-2] > vwap

    # 2. Volume surge — last 3 bars vs the prior 6. Averaging makes it robust to
    #    the newest (partial, still-forming) bar understating volume.
    if len(vols) >= 9:
        recent = sum(vols[-3:]) / 3
        base   = sum(vols[-9:-3]) / 6
        out["vol_surge"] = round(recent / base, 1) if base > 0 else 0.0

    # 3. Acceleration — % move over the last 5 bars (~25 min).
    if closes[-6] > 0:
        out["accel_pct"] = round((closes[-1] - closes[-6]) / closes[-6] * 100, 1)

    # 4. Short-EMA trend — price above a rising 9-EMA.
    ema = calc_ema(closes, 9)
    if ema and len(ema) >= 3:
        out["ema_up"] = price > ema[-1] and ema[-1] > ema[-3]

    tags = []
    if out["vwap_hold"]:       tags.append("VWAP reclaim")
    if out["vol_surge"] >= 2:  tags.append(f"vol surge {out['vol_surge']:g}x")
    if out["accel_pct"] >= 3:  tags.append(f"accel +{out['accel_pct']:g}%/25m")
    if out["ema_up"]:          tags.append("9-EMA up")
    out["tags"]  = tags
    out["score"] = len(tags)
    return out


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
                # market closed — show last extended-hours print if Webull still has one
                price   = sf(q.get("pPrice")) or sf(q.get("close"))
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
            ignition  = None

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
                    # Ignition — is this move real & just starting? (m5 only)
                    if len(C5) >= 6:
                        ignition = calc_ignition(C5, V5, vwap, price)

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
                "ignition": ignition,
                "prev_close": sf(q.get("preClose")),   # yesterday's close — for gap %
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
            # market closed — prefer most recent extended-hours print
            price = (meta.get("postMarketPrice")
                     or meta.get("preMarketPrice")
                     or meta.get("regularMarketPrice"))

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
        if webull.get("prev_close"):  data["prev_close"] = webull["prev_close"]
        # Intraday signals (these had been computed but never merged, so the
        # MFI/OBV/ignition rules downstream never actually saw them).
        if webull.get("mfi") is not None:        data["mfi"]        = webull["mfi"]
        if webull.get("obv_trend"):              data["obv_trend"]  = webull["obv_trend"]
        if webull.get("ignition"):               data["ignition"]   = webull["ignition"]
        if webull.get("change_pct") is not None: data["change_pct"] = webull["change_pct"]

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


def fetch_intraday_bars(symbol: str, count: int = 240) -> list:
    """m5 bars as (ts, high, low, close) tuples, oldest-first. For replaying a
    trade after an alert. ts is unix seconds (UTC)."""
    tid = fetch_webull_id(symbol)
    if not tid:
        return []
    with _wb_semaphore:
        try:
            s = requests.Session()
            s.headers.update(_wb_http.headers)
            r = s.get(
                "https://quotes-gw.webullfintech.com/api/quote/charts/query",
                params={"tickerIds": tid, "type": "m5", "count": count},
                timeout=8,
            )
            if r.status_code != 200:
                return []
            body = r.json()
            if isinstance(body, list) and body and isinstance(body[0], dict):
                data = body[0].get("data", [])
            elif isinstance(body, dict):
                data = body.get("data", [])
            else:
                data = []
            bars = []
            for c in data:
                p = c.split(",") if isinstance(c, str) else c
                if len(p) >= 5:
                    try:
                        # ts, open, close, high, low, ...
                        bars.append((int(float(p[0])), float(p[3]),
                                     float(p[4]), float(p[2])))
                    except Exception:
                        pass
            bars.sort(key=lambda b: b[0])   # oldest-first
            return bars
        except Exception:
            return []


def simulate_trade_outcome(alert_price: float, alerted_ts: float, bars: list):
    """
    Replay the trade after the alert, first-touch:
      T2 (+20%) or T1 (+10%) reached  → ("PASS", pct, label)  WIN
      stop (-7%) reached first         → ("FAIL", -7, label)  LOSS
      neither yet                      → ("OPEN", None, ...)
    Same-bar ties resolve target-first — momentum alerts usually push up
    before pulling back, and once T1 prints you'd have taken profit.
    """
    if not alert_price or alert_price <= 0:
        return None
    t1   = alert_price * (1 + ALERT_T1_PCT)
    t2   = alert_price * (1 + ALERT_T2_PCT)
    stop = alert_price * (1 - ALERT_STOP_PCT)
    window_end = alerted_ts + ALERT_OPEN_MIN * 60   # only count moves within the window
    reached_t1 = False
    for ts, hi, lo, _close in bars:
        if ts < alerted_ts:
            continue
        if ts > window_end:
            break                          # past the target window — stop checking
        if hi >= t2:
            return ("PASS", round(ALERT_T2_PCT * 100, 1), f"T2 +{int(ALERT_T2_PCT*100)}%")
        if hi >= t1:
            reached_t1 = True
            continue                       # lock in T1, keep scanning for T2
        if lo <= stop and not reached_t1:
            return ("FAIL", round(-ALERT_STOP_PCT * 100, 1), f"Stop -{int(ALERT_STOP_PCT*100)}%")
    if reached_t1:
        return ("PASS", round(ALERT_T1_PCT * 100, 1), f"T1 +{int(ALERT_T1_PCT*100)}%")
    return ("OPEN", None, "no target hit")


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
    """
    Grade A / B / C from six 0–2 dimensions (max 12):
      momentum (RSI)        — room to run, not exhausted
      float                 — tighter float moves faster
      volume / turnover     — real participation
      catalyst              — a genuine reason for the move
      entry position        — near day low (good) vs at the high (chasing)
      ignition              — the move is real & starting (VWAP/accel/vol/EMA)
    A ≥ 8, B ≥ 5, else C. Thresholds stay low on purpose so ignition is pure
    upside — it lifts a genuine fresh move, but its absence (e.g. pre-market
    with few intraday bars) never drags a stock down.
    """
    rsi = fv.get("rsi")
    flt = fv.get("float_m")
    rv  = fv.get("rel_vol")
    vol = fv.get("volume") or stock.get("volume", 0)
    h   = fv.get("high")
    l   = fv.get("low")
    p   = fv.get("price") or stock["price"]

    pts = 0

    # 1. Momentum — best when there's still room to run.
    if rsi:
        pts += 2 if rsi < 50 else (1 if rsi < 65 else 0)

    # 2. Float — tighter float = faster move.
    if flt:
        pts += 2 if flt < 5 else (1 if flt < 15 else 0)

    # 3. Volume — micro-float (<2M) uses float turnover (RelVol is misleading
    #    at that size); everything else uses RelVol.
    if flt and flt < 2.0 and vol:
        turnover = vol / (flt * 1_000_000)   # fraction of float traded today
        pts += 2 if turnover > 0.20 else (1 if turnover > 0.10 else 0)
    elif rv:
        pts += 2 if rv > 20 else (1 if rv > 5 else 0)

    # 4. Catalyst (0–2).
    pts += score_catalyst(fv.get("news", ""))[0]

    # 5. Entry position — reward near day-low, penalise chasing the high.
    if h and l and h != l:
        pos  = (p - l) / (h - l)
        pts += 2 if pos < 0.35 else (1 if pos < 0.70 else 0)

    # 6. Ignition — is the move real & just starting?
    ign = fv.get("ignition")
    if ign:
        s = ign.get("score", 0)
        pts += 2 if s >= 3 else (1 if s >= 1 else 0)

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

def gap_line(price, fv) -> str:
    """One-line gap from yesterday's close, or '' when prev close is unknown."""
    pc = fv.get("prev_close")
    if not pc or pc <= 0:
        return ""
    g     = (price - pc) / pc * 100
    arrow = "⬆️" if g >= 0 else "⬇️"
    return f"Gap      {g:+.1f}% {arrow}  (prev close ${pc:.2f})\n"

def build_alert_simple(stock: dict, fv: dict, session: str) -> str:
    """Short alert for auto-scan broadcasts — entry & stop, no indicators."""
    sym    = stock["symbol"]
    price  = fv.get("price") or stock["price"]
    change = stock["change"]
    news   = fv.get("news", "")
    grade  = compute_grade(stock, fv)
    grade_icon   = {"A": "🥇", "B": "🥈", "C": "⚠️"}[grade]
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


def build_momentum_alert(stock: dict, fv: dict, session: str, reject_reason: str = "") -> str:
    """High-risk momentum alert — a big runner the strict filter rejected.
    Clearly flagged as UNVERIFIED / risky so it's never confused with a Grade alert."""
    sym    = stock["symbol"]
    price  = fv.get("price") or stock["price"]
    change = stock["change"]
    news   = fv.get("news", "")
    flt    = fv.get("float_m")
    rsi    = fv.get("rsi")
    rv     = fv.get("rel_vol")
    h, l   = fv.get("high"), fv.get("low")
    _, cat_label = score_catalyst(news)
    tgt          = calc_targets(price, fv)
    entry_lo     = round(price * 0.99, 2)
    entry_hi     = round(price * 1.01, 2)
    D = "━━━━━━━━━━━━━━━━━━━━"

    stats = []
    if flt is not None: stats.append(f"Float {flt:.1f}M")
    if rsi is not None: stats.append(f"RSI {rsi:.0f}")
    if rv  is not None: stats.append(f"RelVol {rv:.0f}x")
    if h and l and h != l:
        stats.append(f"Range {range_bar(price, h, l)}")
    stats_line = ("  •  ".join(stats) + "\n") if stats else ""
    why_line   = f"⚠️ <i>Strict filter skipped: {reject_reason}</i>\n" if reject_reason else ""
    news_line  = f"<i>{news[:120]}</i>\n" if news and news != "—" else ""

    return (
        f"⚡ <b>HIGH-RISK MOMENTUM — {sym}</b>   ${price:.2f}   {change:+.1f}%\n"
        f"🚨 <b>UNVERIFIED PUMP — not a Grade alert. Trade small, fast stop.</b>\n"
        f"{stats_line}"
        f"Entry    ${entry_lo} – ${entry_hi}\n"
        f"Stop     ${tgt['stop']}   -{tgt['stop_pct']}%  (keep it tight)\n"
        f"📰 {cat_label}\n"
        f"{news_line}"
        f"{why_line}"
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
    grade_icon = {"A": "🥇", "B": "🥈", "C": "⚠️"}[grade]
    _, cat_label = score_catalyst(news)
    tgt          = calc_targets(price, fv)
    entry_lo     = round(price * 0.99, 2)
    entry_hi     = round(price * 1.01, 2)
    news_line    = f"<i>{news[:120]}</i>\n" if news and news != "—" else ""
    D = "━━━━━━━━━━━━━━━━━━━━"

    # Indicator summary — full analysis shows the numbers behind the grade.
    ind_bits = []
    if fv.get("rsi") is not None:       ind_bits.append(f"RSI {fv['rsi']:.0f}")
    if fv.get("mfi") is not None:       ind_bits.append(f"MFI {fv['mfi']:.0f}")
    if fv.get("obv_trend", "→") != "→": ind_bits.append(f"OBV {fv['obv_trend']}")
    if fv.get("rel_vol"):               ind_bits.append(f"RelVol {fv['rel_vol']:.0f}x")
    ind_line = ("📊 " + "   ".join(ind_bits) + "\n") if ind_bits else ""

    return (
        f"{grade_icon} <b>{sym}</b>   ${price:.2f}   {change:+.1f}%\n"
        f"Entry    ${entry_lo} – ${entry_hi}\n"
        f"Stop     ${tgt['stop']}   -{tgt['stop_pct']}%\n"
        f"{tgt['label']}\n"
        f"{ind_line}"
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
        "opened_ts":   time.time(),   # for time-based exit warning
        "time_warned": False,
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
                f"{size_line}"
                f"{tgt2['label']}\n\n"
                f"I will alert you if the stop or an exit signal is hit."
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
                if DB_OK:
                    db_remove_position(uid, sym)   # also drop from DB, else it reloads on restart and re-fires the stop
                save_portfolio()
                time.sleep(0.3)
                continue

            # ── Time-based exit warning: open 2h+ and still flat ──
            opened = pos.get("opened_ts")
            if opened is None:
                # position predates this feature (loaded from DB) — start its clock now
                with _lock:
                    if uid in portfolio and sym in portfolio[uid]:
                        portfolio[uid][sym]["opened_ts"] = time.time()
            elif (not pos.get("time_warned")
                  and time.time() - opened >= 7200):
                hrs = (time.time() - opened) / 3600
                send_to(uid,
                    f"⏳ <b>TIME EXIT WARNING — {sym}</b>\n"
                    f"Open   : {hrs:.1f}h, still flat\n"
                    f"Price  : ${price:.2f}  ({pct:+.1f}%)\n\n"
                    f"Scalp rule: if it hasn't run in ~2h, momentum stalled — consider exiting."
                )
                with _lock:
                    if uid in portfolio and sym in portfolio[uid]:
                        portfolio[uid][sym]["time_warned"] = True

            # ── RSI / Volume exit signals ─────────────────────
            rsi_danger = rsi is not None and rsi > 75
            # RelVol ~0 means missing/pre-market data, NOT dying volume — don't warn on it
            vol_danger = rel_vol is not None and 0.1 <= rel_vol < 2.0

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

TERMS_TEXT = (
    "⚠️ <b>RISK DISCLAIMER</b>\n\n"
    "This bot is for educational and informational purposes only and is "
    "<b>NOT financial advice</b>. All trades are your own decision and at your own risk.\n\n"
    "The bot and its operator are <b>NOT responsible for any losses and do not "
    "bear them</b>. Alerts — especially ⚡ HIGH-RISK MOMENTUM — are highly volatile "
    "and can fall sharply within minutes.\n\n"
    "Never invest money you cannot afford to lose. Always use a stop loss. "
    "By using this bot you accept full responsibility for your own trades.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "⚠️ <b>إخلاء المسؤولية</b>\n\n"
    "هذا البوت لأغراض تعليمية ومعلوماتية فقط وليس نصيحة مالية. "
    "جميع الصفقات قرارك وعلى مسؤوليتك الخاصة.\n\n"
    "البوت ومشغّله <b>غير مسؤولين عن أي خسائر ولا يتحمّلونها</b>. "
    "التنبيهات — خاصة ⚡ الزخم عالي المخاطر — متقلبة جدًا وقد تنخفض بسرعة.\n\n"
    "لا تستثمر أموالًا لا تستطيع تحمّل خسارتها. استخدم دائمًا وقف الخسارة. "
    "باستخدامك هذا البوت فإنك تتحمل كامل المسؤولية عن صفقاتك.\n\n"
    "✅ Reply /accept to agree  ·  أرسل /accept للموافقة"
)

GUIDE_TEXT = (
    "📘 <b>How to Use / كيفية الاستخدام</b>\n\n"
    "<b>📊 Grades / التقييمات</b>\n"
    "🥇 A = strong setup — best trades\n"
    "🥈 B = good setup\n"
    "⚠️ C = weak — not alerted\n"
    "⚡ HIGH-RISK MOMENTUM = big runner, unverified pump — risky, trade small\n\n"
    "<b>💼 Position commands / أوامر الصفقات</b>\n"
    "<code>BUY NNVC 1.75</code>       → track a new buy (shares optional)\n"
    "<code>BUY NNVC 1.75 100</code>   → buy with shares\n"
    "<code>ADD NNVC 1.80 100</code>   → average in a second buy\n"
    "<code>SELL NNVC</code>           → stop tracking\n"
    "<code>SELL NNVC 2.10 100</code>  → stop + log P&amp;L\n"
    "<code>EDIT BUY NNVC 1.70</code>  → fix a wrong buy price\n"
    "<code>EDIT SELL NNVC 2.10</code> → fix last sell price\n"
    "<code>UNDO</code>                → restore last sold position\n\n"
    "The bot alerts you on stop-loss and exit signals.\n"
    "ينبّهك البوت عند وقف الخسارة وإشارات الخروج."
)

ACCEPT_PROMPT = (
    "⚠️ <b>Before you start</b>\n\n"
    "Please read the risk disclaimer:  /terms\n"
    "Then reply <b>/accept</b> to confirm you understand and agree.\n\n"
    "⚠️ <b>قبل البدء</b>\n"
    "يرجى قراءة إخلاء المسؤولية:  /terms\n"
    "ثم أرسل <b>/accept</b> للموافقة والمتابعة."
)


def _account_box(uid: str) -> str:
    u     = users.get(uid, {})
    name  = u.get("name") or uid
    uname = u.get("username")
    login = f"{name}  (or @{uname})" if uname else name
    pin   = db_get_pin(uid) if DB_OK else "1234"
    return (
        f"👤 <b>Your account</b>\n"
        f"Name  : {name}\n"
        f"Login : {login}\n"
        f"PIN   : {pin}\n"
    )


def handle_command(uid: str, text: str, sender_name: str = "", sender_username: str = ""):
    global MOMENTUM_ALERTS, INFLOW_REQUIRED
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

    # ── Disclaimer acceptance gate (one-time; admins exempt) ──
    if not is_admin(uid) and not users.get(uid, {}).get("accepted"):
        base = cmd.split()[0] if cmd else ""
        if base == "/accept":
            with _lock:
                if uid in users:
                    users[uid]["accepted"]    = True
                    users[uid]["accepted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            save_users()
            if DB_OK:
                db_set_accepted(uid, users.get(uid, {}).get("accepted_at", ""))
            send_to(uid,
                "✅ <b>Thank you — terms accepted.</b>\nأهلًا بك! تم قبول الشروط.\n\n"
                + _account_box(uid)
                + "\n📘 /guide — commands &amp; grades\n📖 /help — all commands",
                reply_markup=dash_button(),
            )
            return
        if base not in ("/terms", "/disclaimer", "/guide"):
            send_to(uid, ACCEPT_PROMPT)
            return
        # /terms and /guide are allowed before accepting — fall through

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
                "✅ <b>Access granted!</b>  ·  تم منح الوصول\n\n"
                + ACCEPT_PROMPT
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

    elif cmd.startswith("/makeadmin"):
        if not is_admin(uid):
            send_to(uid, "❌ Admin only command.")
            return
        parts = text.split()
        if len(parts) < 2:
            send_to(uid, "Usage: /makeadmin 123456789\n\nSee IDs with /users")
            return
        target = parts[1].strip()
        if target == ADMIN_ID:
            send_to(uid, "✅ That user is already the primary admin.")
        elif target not in users:
            send_to(uid, f"❌ User {target} not found. They must /start the bot and be approved first.")
        elif set_admin(target, True):
            tname = users.get(target, {}).get("name") or target
            send_to(uid, f"👑 <b>{tname}</b> (<code>{target}</code>) is now an admin.")
            send_to(target, "👑 <b>You are now an admin.</b>\nYou can manage users: /users, /adduser, /removeuser.")
        else:
            send_to(uid, f"❌ Could not promote {target}.")

    elif cmd.startswith("/removeadmin"):
        if not is_admin(uid):
            send_to(uid, "❌ Admin only command.")
            return
        parts = text.split()
        if len(parts) < 2:
            send_to(uid, "Usage: /removeadmin 123456789")
            return
        target = parts[1].strip()
        if target == ADMIN_ID:
            send_to(uid, "❌ The primary admin can't be removed.")
        elif set_admin(target, False):
            tname = users.get(target, {}).get("name") or target
            send_to(uid, f"✅ <b>{tname}</b> (<code>{target}</code>) is no longer an admin.")
            send_to(target, "ℹ️ Your admin access has been removed.")
        else:
            send_to(uid, f"❌ User {target} not found or is not an admin.")

    elif cmd == "/users":
        if not is_admin(uid):
            send_to(uid, "❌ Admin only command.")
            return
        refresh_user_info()   # pull names/@usernames via getChat — no waiting
        def _is_adm(i, u):
            return i == ADMIN_ID or u.get("is_admin")
        def _disp_name(i, u):
            nm = u.get("name") or i
            return "❓ (never started the bot)" if str(nm) == str(i) else nm
        active   = sorted(
            [(i, u) for i, u in users.items() if u.get("active")],
            key=lambda x: (0 if _is_adm(*x) else 1, x[1].get("added", ""))
        )
        inactive = [(i, u) for i, u in users.items() if not u.get("active")]
        lines    = [f"👥 <b>Users ({len(active)} active)</b>\n"]
        for n, (i, u) in enumerate(active, 1):
            tag   = " 👑" if _is_adm(i, u) else ""
            uname = f"@{u['username']}" if u.get("username") else ""
            lines.append(f"{n}. <b>{_disp_name(i, u)}</b> {uname} — <code>{i}</code>{tag}")
        if inactive:
            lines.append(f"\n🚫 <b>Inactive ({len(inactive)})</b>")
            for i, u in inactive:
                lines.append(f"• {_disp_name(i, u)} — <code>{i}</code>")
        lines.append(
            "\n<b>Manage</b> (tap an ID to copy):\n"
            "/adduser ID      → approve\n"
            "/removeuser ID   → remove\n"
            "/makeadmin ID    → grant admin 👑\n"
            "/removeadmin ID  → revoke admin"
        )
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
            f"Momentum chan.  : {'⚡ ON' if MOMENTUM_ALERTS else 'OFF'}\n"
            f"Active users    : {n_users}"
        )

    elif cmd == "/backup":
        if not is_admin(uid):
            send_to(uid, "❌ Admin only command.")
            return
        send_to(uid, "📦 Preparing backup...")
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as c:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            log.warning(f"backup checkpoint failed: {e}")
        ok = send_document(uid, DB_PATH, caption=f"🗄️ stockbot.db backup\n<code>{DB_PATH}</code>")
        for fp in (USERS_FILE, PORTFOLIO_FILE, WATCHLIST_FILE, ALERTED_FILE, TRACKED_FILE):
            if os.path.exists(fp):
                send_document(uid, fp)
        send_to(uid, "✅ Backup sent — save these files." if ok else "❌ Backup failed — check logs.")

    elif cmd.startswith("/inflow"):
        if not is_admin(uid):
            send_to(uid, "❌ Admin only command.")
            return
        parts = text.strip().split()
        if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
            INFLOW_REQUIRED = (parts[1].lower() == "on")
        else:
            INFLOW_REQUIRED = not INFLOW_REQUIRED
        send_to(uid,
            f"💧 <b>Inflow filter: {'ON' if INFLOW_REQUIRED else 'OFF'}</b>\n\n"
            + ("Only stocks with confirmed money inflow (OBV↑) + volume surge, caught "
               "earlier in the move — fewer but higher-conviction."
               if INFLOW_REQUIRED else
               "Simple mode — more stocks, standard filters, no inflow requirement.")
            + "\n\nToggle: /inflow on  |  /inflow off"
        )

    elif cmd.startswith("/momentum"):
        if not is_admin(uid):
            send_to(uid, "❌ Admin only command.")
            return
        parts = text.strip().split()
        if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
            MOMENTUM_ALERTS = (parts[1].lower() == "on")
        else:
            MOMENTUM_ALERTS = not MOMENTUM_ALERTS
        send_to(uid,
            f"⚡ <b>High-risk momentum channel: {'ON' if MOMENTUM_ALERTS else 'OFF'}</b>\n\n"
            + ("Big runners the strict filter rejects (unverified pumps) will be sent — clearly labeled as risky."
               if MOMENTUM_ALERTS else "Only strict Grade A/B alerts will be sent.")
            + "\n\nToggle: /momentum on  |  /momentum off"
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
                        icon = "⚪" if pct == 0 else ("🟢" if pct > 0 else "🔴")
                        pl   = f"  {icon} {pct:+.1f}%  now ${price:.2f}"
                    else:
                        pl = ""
                    lines.append(
                        f"<b>{sym}</b>  entry:${entry:.2f}{pl}\n"
                        f"  Stop:${p['stop']:.2f}"
                    )
                send_to(uid, "\n\n".join(lines))
            threading.Thread(target=_pf, daemon=True).start()

    elif cmd.startswith("buy "):
        parts = text.upper().split()
        entry = _parse_price(parts[2]) if len(parts) >= 3 else None
        if len(parts) >= 3 and entry is not None:
            sym_b = parts[1]
            qty   = _parse_qty(parts[3]) if len(parts) >= 4 else None
            add_position(uid, sym_b, entry, qty)
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
                new_price = _parse_price(parts[2])
                new_qty   = _parse_qty(parts[3]) if len(parts) >= 4 else None
                if new_price is None:
                    send_to(uid, "❌ Format: ADD SYMBOL PRICE [SHARES]\nExample: ADD AUUD 1.75 100")
                    return
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
                        f"Stop ${tgt['stop']}"
                    )
            except ValueError:
                send_to(uid, "❌ Format: ADD SYMBOL PRICE [SHARES]\nExample: ADD AUUD 1.75 100")

    elif cmd.startswith("sell "):
        parts = text.upper().split()
        if len(parts) >= 2:
            sym = parts[1]
            # SELL NNVC [exit_price] [qty]
            exit_price = qty_sell = None
            if len(parts) >= 3:
                exit_price = _parse_price(parts[2])
                if exit_price is None:
                    send_to(uid, "❌ Format: SELL SYMBOL [PRICE] [SHARES]\nExamples:\n  SELL NNVC\n  SELL NNVC 3.60\n  SELL NNVC 3.60 50")
                    return
            if len(parts) >= 4:
                qty_sell = _parse_qty(parts[3])
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
                    icon   = "🟢" if exit_price > entry else ("⚪" if exit_price == entry else "🔴")
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
                # Not tracked. Do NOT log a trade with no entry — a 0-entry row
                # produces nan P&L and a false LOSS that corrupts the stats.
                if exit_price:
                    send_to(uid,
                        f"⚠️ <b>{sym}</b> isn't tracked, so there's no entry price — "
                        f"I can't log real P&L for it.\n"
                        f"Next time send <code>BUY {sym} price</code> first, then "
                        f"<code>SELL {sym} {exit_price:.2f}</code> to record the trade."
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
                new_price = _parse_price(parts[3])
                new_qty   = _parse_qty(parts[4]) if len(parts) >= 5 else None
                if new_price is None:
                    raise ValueError("bad price")

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
                            f"Stop ${tgt['stop']}"
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
                f"Entry ${pos_u['entry']:.2f}   Stop ${pos_u['stop']:.2f}"
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
                f"Username: your Telegram name\n\n"
                f"Tap the button below to open the dashboard.",
                reply_markup=dash_button(),
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

    elif cmd in ("/terms", "/disclaimer"):
        send_to(uid, TERMS_TEXT)

    elif cmd == "/guide":
        send_to(uid, GUIDE_TEXT)

    elif cmd == "/accept":
        send_to(uid, "✅ You have already accepted the terms.\nتم قبول الشروط مسبقًا.")

    elif cmd in ("/help", "/start"):
        admin_section = (
            "\n\n<b>Admin:</b>\n"
            "/users                → list &amp; manage users\n"
            "/adduser 123456789    → approve user\n"
            "/removeuser 123456789 → remove user\n"
            "/makeadmin 123456789  → grant admin 👑\n"
            "/removeadmin 12345    → revoke admin\n"
            "/momentum on|off      → toggle momentum channel\n"
            "/inflow on|off        → require inflow+volume, catch earlier 💧\n"
            "/backup               → send DB + state files to you 🗄️"
        ) if is_admin(uid) else ""
        send_to(uid,
            "📖 <b>Commands</b>\n\n"
            + _account_box(uid) + "\n"
            "/check NNVC   → full analysis on a stock\n"
            "/scan         → scan now\n"
            "/status       → session & filter info\n"
            "/watchlist    → last 10 alerts\n"
            "/portfolio    → your positions\n"
            "/track NNVC   → always scan a symbol\n"
            "/untrack NNVC → remove from tracked\n"
            "/setpin 1234  → change your dashboard PIN\n"
            "/claude       → Claude AI status\n\n"
            "<b>Grades:</b>  🥇 A = strong  ·  🥈 B = good  ·  ⚠️ C = not alerted\n\n"
            "📘 /guide  → buy/sell commands &amp; grades\n"
            "⚠️ /terms  → risk disclaimer\n\n"
            "<i>The bot is NOT responsible for any losses. See /terms.</i>\n"
            "🌐 Dashboard — tap the button below"
            + admin_section,
            reply_markup=dash_button(),
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
                            stored  = users[uid]
                            changed = False
                            # Always refresh to the latest real Telegram name so
                            # /users shows names, not numeric IDs.
                            if name and stored.get("name") != name:
                                stored["name"] = name
                                changed = True
                            elif not stored.get("name") or stored.get("name") == uid:
                                stored["name"] = username or uid
                                changed = True
                            if username and stored.get("username") != username:
                                stored["username"] = username
                                changed = True
                            if changed:
                                save_users()
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

    # ── Optional: REQUIRE confirmed inflow + volume surge (toggle /inflow) ──
    # When INFLOW_REQUIRED is ON, only alert when money is actively flowing IN
    # (OBV rising) AND volume is surging — high-conviction "sure inflow + sure
    # volume". When OFF (default), the bot stays simple and surfaces more stocks.
    if INFLOW_REQUIRED:
        ign_vs = (fv.get("ignition") or {}).get("vol_surge", 0)
        if obv != "↑":
            return False, "no inflow — OBV not rising (need money flowing in)"
        if ign_vs < 2 and not (rv and rv >= 3):
            return False, f"no volume surge (15m/30m {ign_vs}x, RelVol {rv}) — wait for volume to start"

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
    # 4. At the top of the day's range = buying the peak, which fades. CLWT was
    #    alerted at pos 1.0 (the exact day high) then dropped. Reject the very top
    #    regardless of catalyst; reject a bit lower too when the catalyst is weak.
    if pos is not None and pos > 0.90:
        return False, f"at {pos:.0%} of day range — too close to day high, bad entry (wait for a dip)"
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
    # 9. Ignition gate — only on big moves, and kept deliberately lenient:
    #    - actively rolling over (price dropping hard over the last 25m) on a
    #      non-strong catalyst → fade, reject.
    #    - truly no catalyst AND zero ignition signals → nothing real behind it.
    #    A neutral-catalyst stock that's merely consolidating is NOT rejected.
    ign = fv.get("ignition")
    if ign and chg >= 50:
        if cat_pts < 2 and ign.get("accel_pct", 0) <= -6:
            return False, f"rolling over {ign['accel_pct']:.0f}% over last 25m — momentum fading"
        if cat_pts == 0 and ign.get("score", 0) == 0:
            return False, f"{chg:.0f}% with no catalyst and no ignition — extended or fading"

    if flt and flt > f["max_float_m"]:    return False, f"float {flt:.1f}M > {f['max_float_m']:.0f}M"
    if mc  and mc  > f["max_mcap_m"]:     return False, f"mcap ${mc:.0f}M > ${f['max_mcap_m']:.0f}M"
    if compute_grade(stock, fv) == "C":   return False, "Grade C"
    # ── Final AI gate (Claude when available, skipped when offline) ──
    legit, ai_reason = claude_pnd_check(stock.get("symbol", ""), stock, fv)
    if not legit:
        return False, ai_reason
    return True, ""

def is_high_risk_momentum(stock: dict, fv: dict, f: dict) -> bool:
    """A stock the STRICT filter rejected, but that's still a real, tradeable
    runner — big move + genuine liquidity you can actually exit. These are the
    'unverified pump' rejects (weak/no catalyst, micro-float, near peak, faded
    off high) — NOT junk (thin liquidity, dying volume, out of range, nano-cap).
    Fires the separate high-risk channel; never the main strict alerts."""
    p   = fv.get("price") or stock["price"]
    chg = stock["change"]
    vol = fv.get("volume") or stock["volume"]
    rsi = fv.get("rsi")
    mc  = fv.get("mcap_m")
    mfi = fv.get("mfi")
    h   = fv.get("high")

    if not (MIN_PRICE <= p <= MAX_PRICE):                return False
    if chg < f["min_change"]:                            return False
    # Hard liquidity floor — money you can actually get back out of
    dollar_vol = (p or 0) * (vol or 0)
    if vol and dollar_vol < f.get("min_dollar_vol", 0): return False
    # Not actively dumping / momentum already dead
    if rsi is not None and rsi < 45:                     return False
    if mfi and mfi >= 90 and (rsi is None or rsi > 75):  return False
    if mc and mc < 1:                                    return False
    # Already fully collapsed off the high — past momentum, not a play anymore
    if h and h > 0 and p < h * MOMENTUM_MIN_FROM_HIGH:   return False
    # Quality floor — must not be bottom-tier on every dimension
    if compute_grade(stock, fv) == "C":                 return False
    return True

def _process_stock(stock: dict, f: dict):
    sym     = stock["symbol"]
    session = get_session() or "UNK"
    fv      = fetch_stock_data(sym)
    if fv is None:
        return None
    grade = compute_grade(stock, fv)
    passed, reason = passes_filters(stock, fv, f)
    if passed:
        if DB_OK:
            db_log_scan(sym, fv.get("price") or stock["price"], stock["change"], grade, True, "", session)
        return (stock, fv, grade, "alert", "")
    # Strict filter rejected — but is it still a tradeable high-risk runner?
    if MOMENTUM_ALERTS and is_high_risk_momentum(stock, fv, f):
        log.info(f"  ⚡Momo {sym:6s}  ${stock['price']:.2f}  {stock['change']:+.0f}%  "
                 f"RSI={fv.get('rsi','?')}  Float={fv.get('float_m','?')}M  Grade={grade}  "
                 f"[high-risk; strict reject: {reason}]")
        if DB_OK:
            db_log_scan(sym, fv.get("price") or stock["price"], stock["change"], grade,
                        False, f"MOMENTUM: {reason}", session)
        return (stock, fv, grade, "momentum", reason)
    log.info(f"  Skip {sym:6s}  ${stock['price']:.2f}  {stock['change']:+.0f}%  "
             f"RSI={fv.get('rsi','?')}  Float={fv.get('float_m','?')}M  Grade={grade}  [{reason}]")
    if DB_OK:
        db_log_scan(sym, stock["price"], stock["change"], grade, False, reason, session)
    return None

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
    # When the inflow filter is ON, catch stocks EARLIER in the move (lower the
    # change bar) — the OBV↑ + volume-surge requirement keeps quality high, so we
    # can trigger during the rise instead of after the wave already finished.
    if INFLOW_REQUIRED:
        f = {**f, "min_change": max(5.0, f["min_change"] - 8.0)}

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

    # Universe = Webull screener (every stock matching price+change) merged with
    # the stockanalysis gainers page (catches pre/after-hours movers the
    # regular-change screener misses). Dedup by symbol, keep the higher change.
    screened = fetch_screener_webull(f)
    gainers  = fetch_gainers(session)
    merged   = {}
    for stock in screened + gainers:
        sym = stock["symbol"]
        if sym not in merged or stock["change"] > merged[sym]["change"]:
            merged[sym] = stock
    universe = sorted(merged.values(), key=lambda s: s["change"], reverse=True)
    log.info(f"  Universe: {len(screened)} screener + {len(gainers)} gainers → {len(universe)} unique")

    if not universe:
        log.warning("No data returned")
        _reply("⚠️ Could not fetch stock list. Sources may be slow — try again in 1 min.")
        return

    # Basic pre-filter — no API calls yet
    # Drop only out-of-range price and cooldown stocks, cap at MAX_CANDIDATES.
    # Volume check is skipped here — Webull gives accurate live volume in passes_filters.
    candidates = []
    for stock in universe[:MAX_CANDIDATES]:
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

    # Split the two channels: strict Grade alerts vs high-risk momentum
    alerts   = [r for r in results if r[3] == "alert"]
    momentum = [r for r in results if r[3] == "momentum"]

    # A grades first
    alerts.sort(key=lambda x: x[2])

    sent = 0
    for stock, fv, grade, _tier, _reason in alerts:
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
            rv_now    = fv.get("rel_vol")
            rv_prev   = prev.get("rel_vol")
            # Second wave: a genuine fresh volume surge vs the prior alert.
            # (These already passed the liquidity floor, so it can't re-open thin names.)
            second_wave = (rv_now is not None and rv_now >= 5
                           and (rv_prev is None or rv_now >= rv_prev * 1.5))
            if 0.85 <= ratio <= 1.50:
                if (obv_fresh or second_wave) and mfi_ok:
                    mfi_s = f"{mfi:.0f}" if mfi else "N/A"
                    why   = (f"2nd-wave RelVol {rv_now:.0f}x"
                             if second_wave and not obv_fresh else f"OBV↑ + MFI={mfi_s}")
                    log.info(f"  Allow {sym} re-alert — {why} → fresh new leg vs prev ${prev['price']:.2f}")
                else:
                    log.info(f"  Skip {sym} re-alert — no fresh money flow "
                             f"(OBV={fv.get('obv_trend')}, MFI={mfi}, RelVol={rv_now}) vs prev ${prev['price']:.2f}")
                    continue
        broadcast(build_alert_simple(stock, fv, session))
        with _lock:
            alerted[sym] = time.time()
            watchlist_log.append({
                "sym": sym, "price": stock["price"], "change": stock["change"],
                "grade": grade, "rel_vol": fv.get("rel_vol"),
                "time": datetime.now(EASTERN).strftime("%H:%M"),
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

    # ── High-risk momentum channel — big runners the strict filter rejected ──
    momo_sent = 0
    for stock, fv, grade, _tier, reason in momentum:
        sym = stock["symbol"]
        with _lock:
            if (time.time() - momentum_alerted.get(sym, 0)) < ALERT_COOLDOWN:
                continue
            if (time.time() - alerted.get(sym, 0)) < ALERT_COOLDOWN:
                continue
        broadcast(build_momentum_alert(stock, fv, session, reason))
        with _lock:
            momentum_alerted[sym] = time.time()
        momo_sent += 1
        log.info(f"  ⚡Momentum → {sym}  ${stock['price']:.2f}  ({stock['change']:+.1f}%)  Grade={grade}")
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
        log.info(f"  No A/B grade matches  ({momo_sent} high-risk momentum sent)")
        if momo_sent:
            _reply(f"🔍 Scan done — no Grade A/B stocks, but {momo_sent} high-risk momentum runner(s) sent. ⚡")
        else:
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
        momentum_alerted.clear()
        orb_alerted.clear()
    save_watchlist()
    log.info(f"[Daily Reset] watchlist_log + momentum cooldowns cleared ({count} entries) — fresh start for today")


def check_alert_performance():
    """
    Replay each of today's open alerts on the m5 bars and record the outcome:
      WIN  → reached T1 (+10%) or T2 (+20%)
      LOSS → hit the stop (-7%) first
      FLAT → hit neither after ALERT_OPEN_MIN minutes
    Scheduled every 20 min, so outcomes resolve as the move happens (no reliance
    on an exact market-close time). Outcomes feed the dashboard win-rate panel.
    """
    if not DB_OK:
        return
    alerts = db_get_todays_alerts()          # today's alerts with outcome still NULL
    if not alerts:
        return

    import calendar
    resolved = 0
    for a in alerts:
        sym         = a["symbol"]
        alert_price = float(a["alert_price"])
        grade       = a.get("grade", "")
        # alerted_at is a UTC string from SQLite datetime('now'); bar ts are UTC epoch.
        try:
            dt = datetime.strptime(str(a["alerted_at"])[:19], "%Y-%m-%d %H:%M:%S")
            alerted_ts = calendar.timegm(dt.timetuple())
        except Exception:
            continue

        bars = fetch_intraday_bars(sym)
        sim  = simulate_trade_outcome(alert_price, alerted_ts, bars) if bars else None
        if not sim:
            continue
        outcome, pct, label = sim

        if outcome in ("PASS", "FAIL"):
            implied = round(alert_price * (1 + pct / 100.0), 4)   # T1/T2/stop price
            db_update_alert_outcome(a["id"], implied, pct, outcome)
            resolved += 1
            log.info(f"[Performance] {sym} [{grade}] → {outcome} ({label})")
        else:
            # No target hit yet — give it ALERT_OPEN_MIN to work, then close FLAT.
            age_min = (time.time() - alerted_ts) / 60.0
            if age_min >= ALERT_OPEN_MIN:
                last  = bars[-1][3] if bars else None
                pct_c = round((last - alert_price) / alert_price * 100, 2) if last else 0.0
                # No +5% within the 30-min window → the alert FAILED (user's definition).
                db_update_alert_outcome(a["id"], last or alert_price, pct_c, "FAIL")
                resolved += 1
                log.info(f"[Performance] {sym} [{grade}] → FAIL (no +5% target, {pct_c:+.1f}% after {age_min:.0f}m)")

    if resolved:
        log.info(f"[Performance] Resolved {resolved} alert outcome(s)")


# ═══════════════════════════════════════════════════════════════
#  ORB — OPENING RANGE BREAKOUT  (separate fast 1-minute module)
#  Marks the first-15-min range and alerts on a volume breakout in the
#  opening 90 minutes, using 1-minute bars. Fully independent of run_scan;
#  any error here is caught so it can never break the main scanner.
# ═══════════════════════════════════════════════════════════════

def fetch_m1_bars(tid: str, count: int = 120) -> list:
    """1-minute OHLCV bars for a Webull tickerId, oldest-first:
    list of (ts, open, close, high, low, volume)."""
    try:
        with _wb_semaphore:
            r = _wb_http.get(
                "https://quotes-gw.webullfintech.com/api/quote/charts/query",
                params={"tickerIds": tid, "type": "m1", "count": count}, timeout=10)
        if r.status_code != 200:
            return []
        body = r.json()
        rows = body[0].get("data", []) if isinstance(body, list) and body else []
        bars = []
        for row in rows:
            p = str(row).split(",")          # ts,open,close,high,low,preClose,volume,vwap
            if len(p) >= 7:
                try:
                    bars.append((int(p[0]), float(p[1]), float(p[2]),
                                 float(p[3]), float(p[4]), float(p[6])))
                except (ValueError, IndexError):
                    continue
        bars.reverse()                       # Webull returns newest-first
        return bars
    except Exception:
        return []


def opening_range(bars: list):
    """High/low/avg-volume of the first ORB_RANGE_MIN minutes of today's regular
    session, plus the session bars after it. None if there isn't enough data yet."""
    et = datetime.now(EASTERN)
    open_ts = int(et.replace(hour=9, minute=30, second=0, microsecond=0).timestamp())
    session = [b for b in bars if b[0] >= open_ts]
    if len(session) < ORB_RANGE_MIN + 1:
        return None
    rng   = session[:ORB_RANGE_MIN]
    after = session[ORB_RANGE_MIN:]
    or_high = max(b[3] for b in rng)
    or_low  = min(b[4] for b in rng)
    or_vol  = sum(b[5] for b in rng) / len(rng)
    return or_high, or_low, or_vol, after


def detect_orb(symbol: str):
    """Return an ORB breakout signal for `symbol`, or None. Breakout = the latest
    1-min bar closes above the opening-range high, the previous bar did not, and
    the breakout bar shows a clear volume surge."""
    tid = fetch_webull_id(symbol)
    if not tid:
        return None
    rng = opening_range(fetch_m1_bars(tid))
    if not rng:
        return None
    or_high, or_low, or_vol, after = rng
    if len(after) < 2 or or_vol <= 0:
        return None
    last, prev = after[-1], after[-2]
    price, vol = last[2], last[5]
    if price > or_high and prev[2] <= or_high and vol >= or_vol * ORB_MIN_RVOL:
        return {"symbol": symbol, "price": price, "or_high": or_high,
                "or_low": or_low, "rvol": round(vol / or_vol, 1)}
    return None


def build_orb_alert(sig: dict) -> str:
    sym, price = sig["symbol"], sig["price"]
    tgt = calc_targets(price, {})
    return (
        f"🚀 <b>ORB BREAKOUT — {sym}</b>   ${price:.2f}\n"
        f"Broke opening-range high ${sig['or_high']:.2f}  ·  vol {sig['rvol']}x\n"
        f"Entry    ${round(price*0.99, 2)} – ${round(price*1.01, 2)}\n"
        f"Stop     ${tgt['stop']}   -{tgt['stop_pct']}%  (keep it tight)\n"
        f"{tgt['label']}\n"
        f"⏱️ Fast trade — first 90 min only.\n"
        f"💬 <code>/check {sym}</code>"
    )


def orb_scan():
    """Opening-Range-Breakout hunter. Scheduled every minute but only acts in the
    9:45–11:00 ET window. Additive — wrapped in try/except so a failure here can
    never break the main scanner."""
    try:
        et = datetime.now(EASTERN)
        if et.weekday() >= 5:
            return
        mins = et.hour * 60 + et.minute
        if not (9*60 + 30 + ORB_RANGE_MIN <= mins <= ORB_WINDOW_END):
            return                            # outside the opening-range window
        f = FILTERS["OPEN"]
        universe = fetch_screener_webull(f) + fetch_gainers("OPEN")
        seen, syms = set(), []
        for s in universe:
            sym = s["symbol"]
            if sym in seen or sym in orb_alerted:
                continue
            if not (MIN_PRICE <= s["price"] <= MAX_PRICE):
                continue
            seen.add(sym)
            syms.append(sym)
        syms = syms[:MAX_CANDIDATES]
        if not syms:
            return
        signals = []
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            for sig in pool.map(detect_orb, syms):
                if sig:
                    signals.append(sig)
        for sig in signals:
            orb_alerted.add(sig["symbol"])
            broadcast(build_orb_alert(sig))
            log.info(f"  [ORB] {sig['symbol']} broke ${sig['or_high']:.2f} "
                     f"@ ${sig['price']:.2f} (vol {sig['rvol']}x)")
        if signals:
            log.info(f"[ORB] {len(signals)} breakout(s) sent")
    except Exception as e:
        log.error(f"[ORB] scan error: {e}")


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
    schedule.every(20).minutes.do(check_alert_performance)   # resolve T1/T2/stop through the day
    schedule.every(1).minutes.do(orb_scan)                   # ORB breakouts — first 90 min only
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
# Accent / semantic colors — concrete (used in BOTH HTML and Plotly), theme-independent.
# Apple system colors so the UI reads "Apple" in light and dark.
ACCENT  = "#0A84FF"   # Apple blue
GREEN   = "#30D158"
RED     = "#FF453A"
YELLOW  = "#FF9F0A"
CYAN    = "#64D2FF"
PURPLE  = "#BF5AF3"
GRADE_C = "#8E8E93"   # neutral gray (Grade C bars / funnel base)
ACCENT_FILL = "rgba(10,132,255,0.14)"

# Theme-switching colors — CSS variables. The .theme-light / .theme-dark class on the
# root flips them, so every inline style using these restyles instantly on toggle.
BG      = "var(--bg)"
SURFACE = "var(--surface)"
SURF2   = "var(--surf2)"
BORDER  = "var(--border)"
TEXT    = "var(--text)"
MUTED   = "var(--muted)"
WHITE   = "var(--strong)"      # high-emphasis text (near-white on dark, near-black on light)

FONT_STACK = '-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Inter", "Cairo", system-ui, sans-serif'

# Concrete palettes for Plotly (CSS variables can't reach the chart canvas).
PALETTE = {
    "dark":  {"paper": "#1C1C1E", "text": "#EBEBF0", "grid": "rgba(255,255,255,0.12)", "tick": "#8E8E93"},
    "light": {"paper": "#FFFFFF", "text": "#1D1D1F", "grid": "rgba(0,0,0,0.10)",       "tick": "#6E6E73"},
}
_theme_ctx = threading.local()   # per-request theme so charts pick the right palette
def _theme() -> str:
    return getattr(_theme_ctx, "value", "dark")

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
def q(sql: str, params=None) -> pd.DataFrame:
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as c:
            return pd.read_sql(sql, c, params=params)
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
        "borderTop":   f"3px solid {color}", "borderRadius": "16px", "boxShadow": "var(--shadow)",
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
        "borderRadius": "16px", "padding": "18px 20px", "marginBottom": mb, "boxShadow": "var(--shadow)",
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

def relabel_breakeven(df):
    """Return a copy with breakeven trades (0% / entry == exit) shown as FLAT
    instead of LOSS — robust even if the DB migration hasn't run."""
    if df is None or df.empty or "result" not in df.columns:
        return df
    df = df.copy()
    be = pd.Series(False, index=df.index)
    if "pnl_pct" in df.columns:
        be = be | (df["pnl_pct"].fillna(-1) == 0)
    if {"entry_price", "exit_price"} <= set(df.columns):
        be = be | (df["entry_price"] == df["exit_price"])
    df.loc[(df["result"] == "LOSS") & be, "result"] = "FLAT"
    return df

def _fig_has_data(fig) -> bool:
    for tr in (fig.data or []):
        for attr in ("y", "x", "values", "labels"):
            v = getattr(tr, attr, None)
            if v is not None and len(v) > 0:
                return True
    return False

def chart(fig, h=300):
    if not _fig_has_data(fig):
        return html.Div("— no data yet —", style={
            "color": MUTED, "fontSize": "13px", "fontWeight": "600",
            "height": f"{h}px", "display": "flex", "alignItems": "center",
            "justifyContent": "center", "background": SURF2, "borderRadius": "14px",
        })
    pal = PALETTE.get(_theme(), PALETTE["dark"])
    fig.update_layout(
        height=h, paper_bgcolor=pal["paper"], plot_bgcolor=pal["paper"],
        font=dict(color=pal["text"], family=FONT_STACK, size=13),
        margin=dict(t=20, b=46, l=54, r=22),
        xaxis=dict(gridcolor=pal["grid"], linecolor=pal["grid"], automargin=True,
                   tickfont=dict(color=pal["tick"], size=12)),
        yaxis=dict(gridcolor=pal["grid"], linecolor=pal["grid"], automargin=True,
                   tickfont=dict(color=pal["tick"], size=12)),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color=pal["text"], size=12)),
        hoverlabel=dict(bgcolor=pal["paper"], font=dict(color=pal["text"], size=13)),
        colorway=[ACCENT, GREEN, YELLOW, CYAN, PURPLE, RED],
        # Preserve UI state across re-renders so charts don't reset/flicker ("go and back")
        uirevision="keep",
        transition={"duration": 0},
    )
    return dcc.Graph(figure=fig, animate=False,
                     config={"displayModeBar": False, "responsive": True},
                     style={"borderRadius": "14px"})

def tbl(df: pd.DataFrame, lang="en", max_rows=100):
    if df is None or df.empty:
        return empty_msg(t("no_data", lang))

    def cell_style(col, val, bg):
        s = {"padding": "7px 12px", "background": bg,
             "borderBottom": f"1px solid {BORDER}",
             "fontSize": "12px", "color": TEXT, "whiteSpace": "nowrap"}
        if col == "result":
            s.update({"color": GREEN if val == "WIN" else (MUTED if val == "FLAT" else RED),
                      "fontWeight": "700"})
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
        if col == "result":          return ("FLAT" if val == "FLAT"
                                              else t("res_win", lang) if val == "WIN"
                                              else t("res_loss", lang))
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
        # Treat day labels as discrete categories — otherwise Plotly reads the
        # date strings as a datetime axis and (with only a day or two of data)
        # zooms to unreadable sub-second ticks.
        fig_bar.update_xaxes(type="category")
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

    my_trades = relabel_breakeven(q(f"SELECT * FROM trades WHERE chat_id = {uid} ORDER BY closed_at DESC"))
    cols = [c for c in ["symbol","entry_price","exit_price","qty",
                         "pnl_dollar","pnl_pct","result","trade_date"]
            if c in my_trades.columns]
    recent = card([sec(t("sec_my_recent_trades", lang)),
                   tbl(my_trades[cols].head(10) if cols else my_trades.head(10), lang)])
    return html.Div([kpis, charts, recent])


def _skip_bucket(reason: str) -> str:
    """Group a free-text scan skip_reason into a tunable category, so the
    diagnostics panel can show WHICH rule rejects the most candidates.
    Order matters: specific phrases are matched before generic ones."""
    r = (reason or "").lower()
    if not r:                                          return "—"
    if "out of range" in r:                            return "Price out of range"
    if "thin liquidity" in r:                          return "Thin liquidity"
    if "dying volume" in r:                            return "Dying volume"
    if "change" in r and "<" in r:                     return "Change % too low"
    if "vol " in r and "<" in r:                       return "Volume too low"
    if "parabolic" in r:                               return "RSI parabolic (too hot)"
    # Rule-9 reasons also contain the word "fading", so match them first.
    if "ignition" in r or "rolling over" in r:         return "No ignition / rolling over"
    if "fading" in r or "dumping" in r:                return "RSI fading"
    if "rsi unknown" in r:                             return "RSI unknown + weak catalyst"
    if "already ran" in r or "below day high" in r:    return "Already ran & dumped"
    if "mfi" in r:                                     return "MFI exhausted"
    if "obv" in r:                                     return "OBV distribution"
    if "vwap" in r:                                    return "Over-extended vs VWAP"
    if "micro-float" in r:                             return "Micro-float pump"
    if "near peak" in r or "day range" in r:           return "Near day high"
    if "no catalyst" in r or "catalyst" in r:          return "Weak / no catalyst"
    if "nano-cap" in r:                                return "Nano-cap"
    if "float" in r and ">" in r:                      return "Float too big"
    if "mcap" in r:                                    return "Market cap out of range"
    if r.startswith("ai:"):                            return "AI: pump verdict"
    if "grade c" in r:                                 return "Grade C (low quality)"
    return reason[:24]


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
        # Discrete categories — avoid Plotly's datetime auto-zoom to sub-second ticks
        fig_eq.update_xaxes(type="category")
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
            textposition="outside",
        ))
        fig_lb.update_layout(yaxis=dict(autorange="reversed"),
                             xaxis=dict(zeroline=True, zerolinecolor="rgba(128,128,128,0.45)"))

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
        marker=dict(color=[GRADE_C, ACCENT, YELLOW, GREEN]),
        textinfo="value+percent initial",
    ))

    # Grade A vs B  (avg % move after alert)
    fig_grade = go.Figure()
    if alerted and "grade" in al.columns and "pct_after_alert" in al.columns:
        gp = (al.dropna(subset=["pct_after_alert"])
                .groupby("grade")["pct_after_alert"].mean().reset_index())
        if not gp.empty:
            gc = {"A": GREEN, "B": ACCENT, "C": GRADE_C}
            fig_grade.add_trace(go.Bar(
                x=gp["grade"], y=gp["pct_after_alert"].round(2),
                marker_color=[gc.get(g, ACCENT) for g in gp["grade"]],
                text=[f"{v:+.1f}%" for v in gp["pct_after_alert"]],
                textposition="outside"))

    funnel = html.Div(className="charts-grid", style={"display": "grid",
                       "gridTemplateColumns": "1fr 1fr", "gap": "12px",
                       "marginBottom": "16px"}, children=[
        card([sec(t("sec_alert_funnel", lang)), chart(fig_fun, 320)], mb="0"),
        card([sec(t("sec_grade_perf", lang)),   chart(fig_grade, 320)], mb="0"),
    ])

    # ── 4. Filter diagnostics — is it too strict? ───────────
    _cell = {"padding": "7px 12px", "borderBottom": f"1px solid {BORDER}", "fontSize": "12px"}
    _th   = {"padding": "9px 12px", "background": BG, "color": MUTED, "fontSize": "10px",
             "fontWeight": "700", "letterSpacing": "1px", "textTransform": "uppercase",
             "borderBottom": f"2px solid {ACCENT}"}

    # Win-rate by grade — only alerts that have been evaluated (have an outcome).
    wr_rows = []
    if alerted and "grade" in al.columns and "outcome" in al.columns:
        ev = al[al["outcome"].isin(["PASS", "FAIL"])]   # resolved win/loss only
        for grade in ["A", "B"]:
            sub = ev[ev["grade"] == grade]
            if len(sub):
                wins = int((sub["outcome"] == "PASS").sum())
                avgp = (round(sub["pct_after_alert"].dropna().mean(), 1)
                        if "pct_after_alert" in sub.columns and sub["pct_after_alert"].notna().any()
                        else 0.0)
                wr_rows.append((grade, len(sub), round(wins / len(sub) * 100), avgp))

    if wr_rows:
        head = html.Tr([html.Th(h, style=_th) for h in
                        ["Grade", "Evaluated", "Win %", "Avg % after"]])
        body = []
        for i, (grade, n, wr_g, avg) in enumerate(wr_rows):
            bg   = SURFACE if i % 2 == 0 else SURF2
            wcol = GREEN if wr_g >= 50 else (YELLOW if wr_g >= 40 else RED)
            acol = GREEN if avg >= 0 else RED
            body.append(html.Tr([
                html.Td(html.B(f"{'🥇' if grade == 'A' else '🥈'} {grade}"),
                        style={**_cell, "background": bg, "color": WHITE}),
                html.Td(str(n),          style={**_cell, "background": bg, "color": TEXT}),
                html.Td(f"{wr_g}%",      style={**_cell, "background": bg, "color": wcol, "fontWeight": "700"}),
                html.Td(f"{avg:+.1f}%",  style={**_cell, "background": bg, "color": acol, "fontWeight": "700"}),
            ]))
        wr_block = html.Div(style={"overflowX": "auto", "borderRadius": "8px",
                                   "border": f"1px solid {BORDER}"},
                            children=[html.Table([html.Thead(head), html.Tbody(body)],
                                      style={"width": "100%", "borderCollapse": "collapse"})])
    else:
        wr_block = empty_msg("No evaluated alerts yet — win-rate fills in once "
                             "alerts have outcomes (price checked after the alert).")

    # Top skip reasons — what the filter rejects most.
    fig_skip = go.Figure()
    if scanned and "skip_reason" in scan.columns and "passed" in scan.columns:
        sk = scan[(scan["passed"] == 0)].copy()
        sk = sk[sk["skip_reason"].astype(str).str.strip() != ""]
        if not sk.empty:
            sk["bucket"] = sk["skip_reason"].astype(str).map(_skip_bucket)
            top = (sk.groupby("bucket").size().reset_index(name="n")
                     .sort_values("n", ascending=False).head(12))
            fig_skip.add_trace(go.Bar(
                y=top["bucket"], x=top["n"], orientation="h",
                marker_color=YELLOW, text=top["n"], textposition="outside"))
            fig_skip.update_layout(yaxis=dict(autorange="reversed"),
                                   xaxis=dict(title="rejections"))

    diagnostics = html.Div(className="charts-grid", style={"display": "grid",
                            "gridTemplateColumns": "1fr 1fr", "gap": "12px",
                            "marginBottom": "16px"}, children=[
        card([sec("WIN-RATE BY GRADE"),
              html.P("Does A actually beat B? If not, the point weights need tuning.",
                     style={"color": MUTED, "fontSize": "11px", "margin": "0 0 12px 0"}),
              wr_block], mb="0"),
        card([sec("TOP SKIP REASONS"),
              html.P("Which rule rejects most. A huge bar = possibly too strict.",
                     style={"color": MUTED, "fontSize": "11px", "margin": "0 0 12px 0"}),
              chart(fig_skip, 300)], mb="0"),
    ])

    return html.Div([stats_block, leaderboard, funnel, diagnostics])


def page_alerts(auth, lang="en"):
    df       = q("SELECT * FROM alerts ORDER BY alerted_at DESC")
    total    = len(df)
    today_df = df[df["alerted_at"].astype(str).str[:10] == today_s()] if total else pd.DataFrame()
    today_n  = len(today_df)
    avg_ch   = round(df["change_pct"].mean(), 1) if total and "change_pct" in df.columns else 0
    perf_df  = df[df["outcome"].isin(["PASS", "FAIL"])] if "outcome" in df.columns else pd.DataFrame()
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
        gc = {"A": GREEN, "B": ACCENT, "C": GRADE_C}
        if not by_grade.empty:
            fig_g.add_trace(go.Bar(x=by_grade["grade"], y=by_grade["n"],
                marker_color=[gc.get(g, ACCENT) for g in by_grade["grade"]],
                text=by_grade["n"], textposition="outside"))
        if not by_sess.empty:
            fig_s.add_trace(go.Bar(x=by_sess["session"], y=by_sess["n"],
                marker_color=YELLOW,
                text=by_sess["n"], textposition="outside"))
        if not top_syms.empty:
            fig_t.add_trace(go.Bar(y=top_syms["symbol"], x=top_syms["n"],
                orientation="h", marker_color=CYAN,
                text=top_syms["n"], textposition="outside"))
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
                elif outcome == "FLAT":
                    badge  = html.Span("FLAT", style={"background": MUTED, "color": "#000",
                        "borderRadius": "4px", "padding": "2px 8px", "fontSize": "11px", "fontWeight": "700"})
                    pct_el = html.Span(f"{pct_val:+.1f}%" if pct_val is not None else "", style={"color": MUTED})
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

    # Breakeven trade (0% / entry == exit) is FLAT, not a LOSS.
    df = relabel_breakeven(df)

    total  = len(df)
    wins   = int((df["result"] == "WIN").sum())
    losses = int((df["result"] == "LOSS").sum())
    decided = wins + losses
    wr     = round(wins / decided * 100, 1) if decided else 0
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
            textposition="outside",
        ))
        fig_sym.update_layout(yaxis=dict(autorange="reversed"),
                               xaxis=dict(zeroline=True, zerolinecolor="rgba(128,128,128,0.45)"))

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
            textposition="outside",
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
    update_title=None,            # no "Updating..." title flicker on every callback
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

# ── Server-side session auth ──────────────────────────────────────────────
# The login identity lives in Flask's signed session cookie (tamper-proof),
# NOT in the client-side dcc.Store. The Store is kept only as a render trigger;
# its contents are never trusted. Without this, anyone could edit localStorage
# to set is_admin=true / another chat_id and read every user's data.
import secrets as _secrets
from flask import session as _flask_session

server.secret_key = os.environ.get("SECRET_KEY") or _secrets.token_hex(32)
if not os.environ.get("SECRET_KEY"):
    log.warning("[dashboard] SECRET_KEY env var not set — using a random key; "
                "dashboard logins reset on every restart. Set SECRET_KEY to persist sessions.")

def _session_auth():
    """Trusted identity from the signed session cookie, or None if not logged in.
    Returns {chat_id, name, is_admin}. NEVER read auth from the client Store."""
    uid = _flask_session.get("uid")
    if not uid:
        return None
    return {
        "chat_id":  str(uid),
        "name":     _flask_session.get("name", ""),
        "is_admin": bool(_flask_session.get("is_admin", False)),
    }

# Speed: gzip every response (shrinks the multi-MB Plotly/JS bundles ~70%).
# No-op if flask-compress isn't installed, so local runs never break.
try:
    from flask_compress import Compress
    Compress(server)
except Exception as _e:
    pass

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
        html, body { margin:0; padding:0; background:var(--bg); -webkit-text-size-adjust:100%; text-size-adjust:100%; }
        * { -webkit-tap-highlight-color:transparent; box-sizing:border-box; }
        * { transition: background-color .25s ease, border-color .25s ease, color .18s ease; }
        .theme-dark{
            --bg:#0B0B0C; --surface:#1C1C1E; --surf2:#2C2C2E;
            --border:rgba(255,255,255,0.12); --text:#EBEBF0; --muted:#8E8E93; --strong:#FFFFFF;
            --shadow:0 1px 2px rgba(0,0,0,.5), 0 8px 28px rgba(0,0,0,.35);
            --topbar:rgba(28,28,30,.72);
        }
        .theme-light{
            --bg:#F5F5F7; --surface:#FFFFFF; --surf2:#F0F0F3;
            --border:rgba(0,0,0,0.10); --text:#1D1D1F; --muted:#6E6E73; --strong:#000000;
            --shadow:0 1px 2px rgba(0,0,0,.06), 0 8px 28px rgba(0,0,0,.08);
            --topbar:rgba(255,255,255,.72);
        }
        .js-plotly-plot, .plot-container, .svg-container { width:100% !important; }
        [dir="rtl"] * { letter-spacing:normal !important; }
        .login-card { max-width:92vw !important; }
        .topbar { background:var(--topbar) !important; border-bottom:1px solid var(--border) !important;
                  backdrop-filter:saturate(180%) blur(20px); -webkit-backdrop-filter:saturate(180%) blur(20px); }
        @media (max-width:900px){
            .app-shell{ flex-direction:column !important; min-height:auto !important; }
            .app-sidebar{ width:100% !important; min-width:0 !important; height:auto !important; position:static !important;
                          flex-direction:row !important; align-items:center !important; flex-wrap:wrap !important;
                          border-right:none !important; border-bottom:1px solid var(--border) !important; }
            .sidebar-hide-mobile{ display:none !important; }
            .nav-links{ flex-direction:row !important; flex:1 1 100% !important; overflow-x:auto !important;
                        padding:6px 8px !important; -webkit-overflow-scrolling:touch !important; }
            .nav-links a{ border-right:none !important; white-space:nowrap !important; padding:10px 14px !important; }
            .charts-grid{ grid-template-columns:1fr !important; }
            .content-pad{ padding:16px 14px !important; }
            .topbar{ padding:12px 14px !important; flex-wrap:wrap !important; gap:10px !important; }
        }
        @media (max-width:420px){
            .login-card{ width:100% !important; padding:28px 22px !important; border-radius:20px !important; }
            .topbar h2{ font-size:14px !important; }
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
    <script>
        // Plotly charts can render at 0 width when first revealed from a hidden
        // container (login -> dashboard), so they look blank while tables show
        // fine. Nudge Plotly to recompute size whenever the DOM changes.
        (function () {
            var timer = null;
            function nudge() {
                clearTimeout(timer);
                timer = setTimeout(function () {
                    window.dispatchEvent(new Event('resize'));
                }, 150);
            }
            window.addEventListener('load', function () {
                nudge();
                new MutationObserver(nudge).observe(
                    document.body, { childList: true, subtree: true });
            });
        })();
    </script>
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
    id="root-container", dir="ltr", className="theme-dark",
    style={"backgroundColor": BG, "fontFamily": FONT_STACK,
           "color": TEXT, "minHeight": "100vh"},
    children=[
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="auth", storage_type="local"),
        dcc.Store(id="lang", storage_type="local"),
        dcc.Store(id="theme", storage_type="local"),
        dcc.Interval(id="tick", interval=30_000),

        html.Div(id="login-section", style=_SHOW_LOGIN, children=[
            html.Div(className="login-card", style={
                "width": "380px", "background": SURFACE,
                "border": f"1px solid {BORDER}",
                "borderRadius": "20px", "padding": "40px", "boxShadow": "var(--shadow)",
            }, children=[
                html.Div(style={"display": "flex", "justifyContent": "flex-end",
                                "gap": "8px", "marginBottom": "8px"}, children=[
                    html.Button("☀️", id="theme-toggle-login",
                                n_clicks=0, style=_lang_btn_style()),
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
                            html.Button("☀️", id="theme-toggle",
                                        n_clicks=0, style=_lang_btn_style()),
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


# Appearance toggle (light <-> dark), persisted per device
@app.callback(
    Output("theme", "data"),
    Input("theme-toggle",       "n_clicks"),
    Input("theme-toggle-login", "n_clicks"),
    State("theme", "data"),
    prevent_initial_call=True,
)
def toggle_theme(_n1, _n2, cur):
    return "light" if (cur or "dark") == "dark" else "dark"


# Apply the theme class to the root container + refresh the toggle icons
@app.callback(
    Output("root-container",     "className"),
    Output("theme-toggle",       "children"),
    Output("theme-toggle-login", "children"),
    Input("theme", "data"),
)
def apply_theme(theme):
    theme = "light" if theme == "light" else "dark"
    icon  = "🌙" if theme == "light" else "☀️"   # icon shows the mode you'll switch TO
    return f"theme-{theme}", icon, icon


@app.callback(
    Output("trades-body",         "children"),
    Output("trades-filter-day",   "style"),
    Output("trades-filter-month", "style"),
    Input("trades-filter-type",   "value"),
    Input("trades-filter-day",    "value"),
    Input("trades-filter-month",  "value"),
    Input("auth", "data"),
    Input("lang", "data"),
    Input("theme", "data"),
    prevent_initial_call=False,
)
def update_trades_content(filter_type, sel_day, sel_month, _auth_trigger, lang, theme):
    auth = _session_auth()   # trust the signed cookie, never the client Store
    if not auth:
        return no_update, no_update, no_update
    lang = lang if lang in TR else "en"
    _theme_ctx.value = "light" if theme == "light" else "dark"

    uid      = int(auth["chat_id"])
    is_admin = auth["is_admin"]

    _DD      = {"background": SURF2, "border": f"1px solid {BORDER}",
                "borderRadius": "8px", "color": TEXT, "fontSize": "13px", "minWidth": "140px"}
    _DD_HIDE = {**_DD, "display": "none"}
    day_style   = _DD if filter_type == "day"   else _DD_HIDE
    month_style = _DD if filter_type == "month" else _DD_HIDE

    # Parameterized — values bound via ? placeholders, never string-interpolated.
    params: list = []
    if is_admin:
        base_where = "1=1"
    else:
        base_where = "t.chat_id = ?"
        params.append(uid)

    period_where = "1=1"
    if filter_type == "day" and sel_day:
        period_where = "DATE(t.trade_date) = ?"
        params.append(str(sel_day))
    elif filter_type == "month" and sel_month:
        try:
            y, m = str(sel_month).split("-")
            period_where = ("strftime('%Y', t.trade_date) = ? "
                            "AND strftime('%m', t.trade_date) = ?")
            params.extend([y, f"{int(m):02d}"])
        except (ValueError, TypeError):
            period_where = "1=1"

    if is_admin:
        df = q(f"""
            SELECT t.*, u.name AS user_name
            FROM trades t LEFT JOIN users u ON t.chat_id = u.chat_id
            WHERE ({base_where}) AND ({period_where})
            ORDER BY t.closed_at DESC
        """, params)
    else:
        df = q(f"""
            SELECT * FROM trades t
            WHERE ({base_where}) AND ({period_where})
            ORDER BY t.closed_at DESC
        """, params)

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
    Input("theme", "data"),
    # NOTE: no "tick" here on purpose — the 30s timer used to rebuild the whole
    # page every cycle, making all charts flicker (disappear/redraw). Pages now
    # render only on navigation / login / language / theme change.
)
def route(pathname, _auth_trigger, lang, theme):
    auth = _session_auth()   # trust the signed cookie, never the client Store
    if not auth:
        return _SHOW_LOGIN, _HIDE, [], "", "", "", ""
    lang = lang if lang in TR else "en"
    _theme_ctx.value = "light" if theme == "light" else "dark"

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
        # Store identity server-side in the signed session cookie.
        _flask_session["uid"]      = user["chat_id"]
        _flask_session["name"]     = user["name"]
        _flask_session["is_admin"] = user["is_admin"]
        _flask_session.permanent   = True
        # The Store value is only a render trigger now — not trusted.
        return {"ok": True}, ""
    return no_update, t("login_error", lang if lang in TR else "en")


@app.callback(
    Output("auth", "data", allow_duplicate=True),
    Input("logout-btn", "n_clicks"),
    prevent_initial_call=True,
)
def do_logout(_):
    _flask_session.clear()
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
