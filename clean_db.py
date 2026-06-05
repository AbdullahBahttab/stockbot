"""
Run once:  python clean_db.py
Clears all trading data and sets up PIN login for the dashboard.
"""
import pyodbc

CONN = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;DATABASE=StockBot;"
    "Trusted_Connection=yes;Connection Timeout=5;"
)
ADMIN_ID = 179463282

steps = [
    ("Delete scan_log",  "DELETE FROM scan_log"),
    ("Delete alerts",    "DELETE FROM alerts"),
    ("Delete trades",    "DELETE FROM trades"),
    ("Delete portfolio", "DELETE FROM portfolio"),
    ("Delete users",     "DELETE FROM users"),

    ("Add pin column",
     """IF NOT EXISTS (
         SELECT 1 FROM sys.columns
         WHERE object_id = OBJECT_ID('users') AND name = 'pin'
     )
     ALTER TABLE users ADD pin VARCHAR(10) NULL"""),

    ("Ensure admin user",
     f"""IF NOT EXISTS (SELECT 1 FROM users WHERE chat_id = {ADMIN_ID})
         INSERT INTO users (chat_id, name, username, is_active, is_admin, pin)
         VALUES ({ADMIN_ID}, 'Admin', 'admin', 1, 1, '1234')
     ELSE
         UPDATE users SET pin = '1234', is_admin = 1, is_active = 1
         WHERE chat_id = {ADMIN_ID}"""),
]

print("StockBot — Clean Start")
print("=" * 40)
try:
    conn = pyodbc.connect(CONN, autocommit=True)
    cur  = conn.cursor()
    for label, sql in steps:
        cur.execute(sql)
        print(f"  OK  {label}")
    conn.close()
    print("=" * 40)
    print("Done! All data cleared.")
    print()
    print("Dashboard login:")
    print("  Username : Admin")
    print("  PIN      : 1234")
    print()
    print("Change PIN anytime with /setpin XXXX in Telegram.")
except Exception as e:
    print(f"  ERROR: {e}")
