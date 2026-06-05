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
BASE_DIR = os.getcwd()
# DB_PATH can be overridden (e.g. a Railway Volume at /data) for persistence.
DB_PATH  = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "stockbot.db"))

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
if __name__ == "__main__":
    print("StockBot Dashboard  ->  http://0.0.0.0:8050")
    print("Login: Admin / 1234")
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 8050)))
