"""
Migration: SQL Server -> SQLite
Run once on Windows:  python migrate_to_sqlite.py
This creates stockbot.db with all your existing data.
"""
import pyodbc
import sqlite3
import os

# ── SQL Server connection ─────────────────────────────────────
CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=StockBot;"
    "Trusted_Connection=yes;"
)

# ── Output SQLite file ────────────────────────────────────────
SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stockbot.db")

print("StockBot - SQL Server to SQLite Migration")
print("=" * 45)
print(f"Source : SQL Server (StockBot)")
print(f"Target : {SQLITE_PATH}")
print()

# ── Connect to SQL Server ─────────────────────────────────────
try:
    src = pyodbc.connect(CONN_STR, timeout=10)
    print("  OK  Connected to SQL Server")
except Exception as e:
    print(f"  FAIL  Cannot connect to SQL Server: {e}")
    print("        Make sure SQL Server is running and StockBot DB exists.")
    exit(1)

# ── Create SQLite ─────────────────────────────────────────────
dst = sqlite3.connect(SQLITE_PATH)
dst.executescript("""
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
print("  OK  SQLite tables created")

def clean(val):
    from decimal import Decimal
    if isinstance(val, Decimal):
        return float(val)
    return val

def migrate_table(table, columns):
    cur = src.cursor()
    try:
        cols = ", ".join(columns)
        cur.execute(f"SELECT {cols} FROM {table}")
        rows = cur.fetchall()
        if not rows:
            print(f"  --  {table}: empty, skipping")
            return
        placeholders = ", ".join(["?"] * len(columns))
        cleaned = [tuple(clean(v) for v in r) for r in rows]
        dst.executemany(
            f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})",
            cleaned
        )
        dst.commit()
        print(f"  OK  {table}: {len(rows)} rows migrated")
    except Exception as e:
        print(f"  WARN {table}: {e}")

# ── Migrate each table ────────────────────────────────────────
migrate_table("users", [
    "chat_id", "name", "username", "is_active", "is_admin", "pin", "joined_at"
])

migrate_table("portfolio", [
    "chat_id", "symbol", "entry_price", "stop_price", "t1_price", "t2_price",
    "t1_hit", "t2_hit", "rsi_warned", "vol_warned", "exit_warned", "qty", "added_at"
])

migrate_table("trades", [
    "chat_id", "symbol", "entry_price", "exit_price", "qty",
    "pnl_dollar", "pnl_pct", "result", "trade_date", "closed_at"
])

migrate_table("alerts", [
    "symbol", "alert_price", "grade", "change_pct", "float_m", "rsi",
    "volume", "rel_vol", "mcap_m", "session", "alerted_at",
    "outcome", "close_price", "pct_after_alert"
])

migrate_table("scan_log", [
    "symbol", "price", "change_pct", "grade", "passed", "skip_reason", "session", "scanned_at"
])

src.close()
dst.close()

print()
print("=" * 45)
print(f"Done! File saved: {SQLITE_PATH}")
print()
print("Next step: push this file to Railway.")
print("  Run: python push_db_to_railway.py")
