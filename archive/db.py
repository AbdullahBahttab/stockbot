"""
db.py — SQL Server layer for Stock Scanner Bot
Each function opens its own connection (thread-safe).
If SQL Server is unavailable the bot continues with JSON files.
"""
import logging
import threading

log = logging.getLogger("scanner")

# ─── Config ──────────────────────────────────────────────────────────────────
DB_SERVER  = "localhost"                      # change if SQL Server is remote
DB_NAME    = "StockBot"
DB_DRIVER  = "ODBC Driver 17 for SQL Server"  # try 18 if 17 not installed
DB_TRUSTED = True                             # Windows auth — no password needed

DB_OK = False   # set True after successful first connect

_pyodbc = None  # lazy import


def _import_pyodbc():
    global _pyodbc
    if _pyodbc is None:
        try:
            import pyodbc as _mod
            _pyodbc = _mod
        except ImportError:
            log.warning("[DB] pyodbc not installed. Run: pip install pyodbc")
    return _pyodbc


def get_conn():
    """Open a fresh connection. Returns None if unavailable."""
    mod = _import_pyodbc()
    if not mod:
        return None
    try:
        conn = mod.connect(
            f"DRIVER={{{DB_DRIVER}}};"
            f"SERVER={DB_SERVER};"
            f"DATABASE={DB_NAME};"
            f"{'Trusted_Connection=yes;' if DB_TRUSTED else ''}"
            "Connection Timeout=3;",
            autocommit=True,
        )
        return conn
    except Exception as e:
        log.debug(f"[DB] connect failed: {e}")
        return None


def test_connection() -> bool:
    """Call once at startup. Sets DB_OK flag."""
    global DB_OK
    conn = get_conn()
    if conn:
        conn.close()
        DB_OK = True
        log.info("[DB] SQL Server connected OK  (database: StockBot)")
    else:
        DB_OK = False
        log.warning("[DB] SQL Server not available — running with JSON files only")
    return DB_OK


# ─── Users ────────────────────────────────────────────────────────────────────

def db_load_users() -> dict:
    """Returns {chat_id_str: {name, username, active, is_admin, added}} or {}."""
    conn = get_conn()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT chat_id, name, username, is_active, is_admin, joined_at FROM users"
        )
        result = {}
        for row in cur.fetchall():
            uid = str(row.chat_id)
            result[uid] = {
                "name":     row.name or uid,
                "username": row.username or "",
                "active":   bool(row.is_active),
                "is_admin": bool(row.is_admin),
                "added":    row.joined_at.strftime("%Y-%m-%d") if row.joined_at else "",
            }
        return result
    except Exception as e:
        log.error(f"[DB] load_users: {e}")
        return {}
    finally:
        conn.close()


def db_upsert_user(chat_id: str, name: str, username: str,
                   active: bool, is_admin: bool):
    """Insert or update a user row."""
    conn = get_conn()
    if not conn:
        return
    try:
        cid = int(chat_id)
        cur = conn.cursor()
        cur.execute("""
            MERGE users AS t
            USING (SELECT ? AS chat_id) AS s ON t.chat_id = s.chat_id
            WHEN MATCHED THEN
                UPDATE SET name=?, username=?, is_active=?, is_admin=?
            WHEN NOT MATCHED THEN
                INSERT (chat_id, name, username, is_active, is_admin)
                VALUES (?,      ?,    ?,        ?,         ?);
        """,
        cid,
        name, username, 1 if active else 0, 1 if is_admin else 0,
        cid, name, username, 1 if active else 0, 1 if is_admin else 0,
        )
    except Exception as e:
        log.error(f"[DB] upsert_user {chat_id}: {e}")
    finally:
        conn.close()


def db_set_pin(chat_id: str, pin: str):
    """Set or update the dashboard PIN for a user."""
    conn = get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET pin = ? WHERE chat_id = ?",
            str(pin)[:10], int(chat_id),
        )
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
        cur = conn.cursor()
        cur.execute("SELECT pin FROM users WHERE chat_id = ?", int(chat_id))
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else "1234"
    except Exception as e:
        log.error(f"[DB] get_pin {chat_id}: {e}")
        return "1234"
    finally:
        conn.close()


def db_sync_users(users_dict: dict):
    """Sync the full in-memory users dict to SQL Server."""
    for uid, u in users_dict.items():
        db_upsert_user(
            uid,
            u.get("name", uid),
            u.get("username", ""),
            u.get("active", True),
            u.get("is_admin", False),
        )


# ─── Alerted / Cooldown ───────────────────────────────────────────────────────

def db_load_alerted(cooldown_seconds: int) -> dict:
    """
    Returns {symbol: unix_timestamp} for symbols still in cooldown.
    Used on startup to restore in-memory alerted dict from DB.
    """
    conn = get_conn()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT a.symbol,
                   DATEDIFF(SECOND, '19700101',
                       SWITCHOFFSET(CONVERT(DATETIMEOFFSET, a.alerted_at), '+00:00')) AS ts
            FROM alerts AS a
            INNER JOIN (
                SELECT symbol, MAX(alerted_at) AS last_alert
                FROM   alerts
                GROUP BY symbol
            ) AS mx ON a.symbol = mx.symbol AND a.alerted_at = mx.last_alert
            WHERE DATEDIFF(SECOND, a.alerted_at, GETDATE()) < ?
        """, int(cooldown_seconds))
        return {row.symbol: float(row.ts) for row in cur.fetchall()}
    except Exception as e:
        log.error(f"[DB] load_alerted: {e}")
        return {}
    finally:
        conn.close()


def db_log_alert(symbol: str, price: float, grade: str,
                 change_pct: float, float_m, rsi, volume,
                 rel_vol, mcap_m, session: str):
    """Insert one alert record. Called every time the bot broadcasts a stock."""
    conn = get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO alerts
                (symbol, alert_price, grade, change_pct, float_m, rsi,
                 volume, rel_vol, mcap_m, session)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        symbol, price, grade, change_pct,
        float(float_m) if float_m is not None else None,
        float(rsi)     if rsi     is not None else None,
        int(volume)    if volume  is not None else None,
        float(rel_vol) if rel_vol is not None else None,
        float(mcap_m)  if mcap_m  is not None else None,
        session,
        )
    except Exception as e:
        log.error(f"[DB] log_alert {symbol}: {e}")
    finally:
        conn.close()


def db_get_recent_alerts(hours: int = 24) -> list:
    """Returns list of alert dicts from the last N hours (newest first)."""
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT symbol, alert_price, grade, change_pct,
                   float_m, rsi, session, alerted_at
            FROM   alerts
            WHERE  alerted_at >= DATEADD(HOUR, -?, GETDATE())
            ORDER BY alerted_at DESC
        """, hours)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"[DB] get_recent_alerts: {e}")
        return []
    finally:
        conn.close()


def db_get_todays_alerts() -> list:
    """Returns today's alerts that have not yet been evaluated."""
    conn = get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, symbol, alert_price, grade, change_pct, session, alerted_at
            FROM   alerts
            WHERE  CAST(alerted_at AS DATE) = CAST(GETDATE() AS DATE)
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
    """Update an alert row with its end-of-day performance."""
    conn = get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE alerts
            SET close_price = ?, pct_after_alert = ?, outcome = ?
            WHERE id = ?
        """, round(close_price, 4), round(pct_after, 4), outcome, alert_id)
    except Exception as e:
        log.error(f"[DB] update_alert_outcome {alert_id}: {e}")
    finally:
        conn.close()


# ─── Portfolio ────────────────────────────────────────────────────────────────

def db_load_portfolio() -> dict:
    """Returns {uid_str: {symbol: position_dict}} or {}."""
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
            uid = str(row.chat_id)
            port.setdefault(uid, {})[row.symbol] = {
                "entry":       float(row.entry_price),
                "stop":        float(row.stop_price)  if row.stop_price  is not None else None,
                "t1":          float(row.t1_price)    if row.t1_price    is not None else None,
                "t2":          float(row.t2_price)    if row.t2_price    is not None else None,
                "t1_hit":      bool(row.t1_hit),
                "t2_hit":      bool(row.t2_hit),
                "rsi_warned":  bool(row.rsi_warned),
                "vol_warned":  bool(row.vol_warned),
                "exit_warned": bool(row.exit_warned),
                "qty":         int(row.qty) if row.qty is not None else None,
            }
        return port
    except Exception as e:
        log.error(f"[DB] load_portfolio: {e}")
        return {}
    finally:
        conn.close()


def db_save_position(chat_id: str, symbol: str, pos: dict):
    """Upsert one portfolio position (including qty if provided)."""
    conn = get_conn()
    if not conn:
        return
    try:
        cid = int(chat_id)
        qty = pos.get("qty")
        cur = conn.cursor()
        cur.execute("""
            MERGE portfolio AS t
            USING (SELECT ? AS chat_id, ? AS symbol) AS s
                ON t.chat_id = s.chat_id AND t.symbol = s.symbol
            WHEN MATCHED THEN UPDATE SET
                entry_price=?, stop_price=?, t1_price=?, t2_price=?,
                t1_hit=?, t2_hit=?, rsi_warned=?, vol_warned=?, exit_warned=?, qty=?
            WHEN NOT MATCHED THEN INSERT
                (chat_id, symbol, entry_price, stop_price, t1_price, t2_price,
                 t1_hit,  t2_hit,  rsi_warned,  vol_warned,  exit_warned, qty)
            VALUES
                (?,       ?,      ?,           ?,          ?,        ?,
                 ?,       ?,      ?,           ?,          ?,        ?);
        """,
        # USING
        cid, symbol,
        # UPDATE
        pos.get("entry"), pos.get("stop"), pos.get("t1"), pos.get("t2"),
        1 if pos.get("t1_hit")      else 0,
        1 if pos.get("t2_hit")      else 0,
        1 if pos.get("rsi_warned")  else 0,
        1 if pos.get("vol_warned")  else 0,
        1 if pos.get("exit_warned") else 0,
        qty,
        # INSERT
        cid, symbol,
        pos.get("entry"), pos.get("stop"), pos.get("t1"), pos.get("t2"),
        1 if pos.get("t1_hit")      else 0,
        1 if pos.get("t2_hit")      else 0,
        1 if pos.get("rsi_warned")  else 0,
        1 if pos.get("vol_warned")  else 0,
        1 if pos.get("exit_warned") else 0,
        qty,
        )
    except Exception as e:
        log.error(f"[DB] save_position {chat_id}/{symbol}: {e}")
    finally:
        conn.close()


def db_remove_position(chat_id: str, symbol: str):
    """Delete a position row."""
    conn = get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM portfolio WHERE chat_id=? AND symbol=?",
            int(chat_id), symbol,
        )
    except Exception as e:
        log.error(f"[DB] remove_position {chat_id}/{symbol}: {e}")
    finally:
        conn.close()


# ─── Trades (closed) ─────────────────────────────────────────────────────────

def db_log_trade(chat_id: str, symbol: str,
                 entry: float, exit_price: float = None, qty: int = None):
    """
    Insert a closed trade record.
    Called when user types SELL SYM [price] [qty].
    """
    conn = get_conn()
    if not conn:
        return
    try:
        pnl_d = None
        pnl_p = None
        result = None
        if exit_price is not None and entry:
            qty_val = qty or 0
            pnl_d   = round((exit_price - entry) * qty_val, 2) if qty_val else None
            pnl_p   = round((exit_price - entry) / entry * 100, 4)
            result  = "WIN" if pnl_p > 0 else "LOSS"

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trades
                (chat_id, symbol, entry_price, exit_price, qty,
                 pnl_dollar, pnl_pct, result, trade_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CAST(GETDATE() AS DATE))
        """,
        int(chat_id), symbol, entry, exit_price, qty,
        pnl_d, pnl_p, result,
        )
    except Exception as e:
        log.error(f"[DB] log_trade {chat_id}/{symbol}: {e}")
    finally:
        conn.close()


def db_update_last_trade(chat_id: str, symbol: str,
                         exit_price: float = None, entry: float = None,
                         qty: int = None) -> bool:
    """
    Update the most recent trade row for this user+symbol.
    Returns True if a row was updated, False otherwise.
    """
    conn = get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        # Fetch the most recent trade for this symbol
        cur.execute("""
            SELECT TOP 1 id, entry_price, exit_price, qty
            FROM trades
            WHERE chat_id = ? AND symbol = ?
            ORDER BY closed_at DESC, id DESC
        """, int(chat_id), symbol)
        row = cur.fetchone()
        if not row:
            return False
        tid        = row.id
        new_entry  = entry       if entry       is not None else (float(row.entry_price) if row.entry_price else 0.0)
        new_exit   = exit_price  if exit_price  is not None else (float(row.exit_price)  if row.exit_price  else None)
        new_qty    = qty         if qty         is not None else (int(row.qty)            if row.qty         else None)
        pnl_d = None
        pnl_p = None
        result = None
        if new_exit is not None and new_entry:
            qty_val = new_qty or 0
            pnl_d   = round((new_exit - new_entry) * qty_val, 2) if qty_val else None
            pnl_p   = round((new_exit - new_entry) / new_entry * 100, 4)
            result  = "WIN" if pnl_p > 0 else "LOSS"
        cur.execute("""
            UPDATE trades
            SET entry_price=?, exit_price=?, qty=?, pnl_dollar=?, pnl_pct=?, result=?
            WHERE id=?
        """, new_entry, new_exit, new_qty, pnl_d, pnl_p, result, tid)
        return True
    except Exception as e:
        log.error(f"[DB] update_last_trade {chat_id}/{symbol}: {e}")
        return False
    finally:
        conn.close()


def db_get_trades(chat_id: str = None, days: int = 30) -> list:
    """Returns list of trade dicts for a user (or all users) in last N days."""
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
                WHERE  chat_id = ?
                  AND  closed_at >= DATEADD(DAY, -?, GETDATE())
                ORDER BY closed_at DESC
            """, int(chat_id), days)
        else:
            cur.execute("""
                SELECT chat_id, symbol, entry_price, exit_price, qty,
                       pnl_dollar, pnl_pct, result, trade_date, closed_at
                FROM   trades
                WHERE  closed_at >= DATEADD(DAY, -?, GETDATE())
                ORDER BY closed_at DESC
            """, days)
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
    """Log every stock scanned — both passed and filtered. Runs in background."""
    conn = get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scan_log (symbol, price, change_pct, grade, passed, skip_reason, session)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        symbol,
        float(price) if price is not None else None,
        float(change_pct) if change_pct is not None else None,
        grade or "",
        1 if passed else 0,
        skip_reason or "",
        session or "",
        )
    except Exception as e:
        log.debug(f"[DB] log_scan {symbol}: {e}")
    finally:
        conn.close()
