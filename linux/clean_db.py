"""
Run once on the server:  python clean_db.py
Creates the SQLite database and sets up the admin user.
"""
import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "stockbot.db")
ADMIN_ID = 179463282

print("StockBot — Fresh SQLite Setup")
print("=" * 40)

conn = sqlite3.connect(DB_PATH)
conn.executescript("""
    DROP TABLE IF EXISTS scan_log;
    DROP TABLE IF EXISTS alerts;
    DROP TABLE IF EXISTS trades;
    DROP TABLE IF EXISTS portfolio;
    DROP TABLE IF EXISTS users;

    CREATE TABLE users (
        chat_id   INTEGER PRIMARY KEY,
        name      TEXT,
        username  TEXT,
        is_active INTEGER DEFAULT 1,
        is_admin  INTEGER DEFAULT 0,
        pin       TEXT    DEFAULT '1234',
        joined_at DATETIME DEFAULT (datetime('now'))
    );

    CREATE TABLE portfolio (
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

    CREATE TABLE trades (
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

    CREATE TABLE alerts (
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

    CREATE TABLE scan_log (
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
    ON CONFLICT(chat_id) DO UPDATE SET
        pin='1234', is_admin=1, is_active=1
""", (ADMIN_ID,))
conn.commit()
conn.close()

print("  OK  Tables created")
print("  OK  Admin user added")
print("=" * 40)
print(f"Database: {DB_PATH}")
print()
print("Dashboard login:")
print("  Username : Admin")
print("  PIN      : 1234")
print()
print("Change PIN anytime with /setpin XXXX in Telegram.")
