"""
db.py — SQLite layer for Stock Scanner Bot (Linux)
Drop this file next to stock_scanner.py on the server to replace the SQL Server version.
"""
import sqlite3
import logging
import os

log = logging.getLogger("scanner")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "stockbot.db")
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
