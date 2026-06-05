#!/bin/bash
# Railway startup: use SQLite db, init tables, run bot + dashboard
set -e

# Use the Linux/SQLite modules
cp linux/db.py db.py
cp linux/dashboard.py dashboard.py

# Create tables (idempotent — safe every startup)
python - <<'PYEOF'
import sqlite3, os

DB_PATH  = os.path.join(os.getcwd(), "stockbot.db")
ADMIN_ID = 179463282

conn = sqlite3.connect(DB_PATH, timeout=10)
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
conn.execute("""
    INSERT INTO users (chat_id, name, username, is_active, is_admin, pin)
    VALUES (?, 'Admin', 'admin', 1, 1, '1234')
    ON CONFLICT(chat_id) DO NOTHING
""", (ADMIN_ID,))
conn.commit()
conn.close()
print("DB ready:", DB_PATH)
PYEOF

# Start dashboard in background (Railway exposes it via PORT)
python dashboard.py &

# Start the bot (foreground — keeps the process alive)
exec python stock_scanner.py
