"""
StockBot Dashboard — Linux / SQLite version
Run : python dashboard.py
Open: http://YOUR_SERVER_IP:8050

Login with your Telegram name + PIN (default PIN: 1234)
Change PIN in Telegram: /setpin XXXX
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "stockbot.db")

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

_INPUT = {
    "width": "100%", "boxSizing": "border-box",
    "background": BG, "border": f"1px solid {BORDER}",
    "borderRadius": "8px", "padding": "11px 14px",
    "color": WHITE, "fontSize": "14px", "outline": "none",
    "marginBottom": "18px", "fontFamily": "Inter, system-ui, sans-serif",
}

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
    try:
        from pytz import timezone
        et = datetime.now(timezone("US/Eastern"))
        m, wd = et.hour * 60 + et.minute, et.weekday()
        if wd >= 5:                return "WEEKEND",     MUTED
        if 9*60+30 <= m < 16*60:   return "MARKET OPEN", GREEN
        if 4*60    <= m < 9*60+30: return "PRE-MARKET",  YELLOW
        if 16*60   <= m <= 20*60:  return "AFTER-HOURS", CYAN
        return "CLOSED", MUTED
    except:
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
                                 "textAlign": "center", "padding": "40px"})

def chart(fig, h=280):
    fig.update_layout(
        height=h, paper_bgcolor=SURFACE, plot_bgcolor=SURF2,
        font=dict(color=TEXT, family="Inter, system-ui, sans-serif", size=11),
        margin=dict(t=12, b=28, l=10, r=10),
        xaxis=dict(gridcolor=BORDER, linecolor=BORDER),
        yaxis=dict(gridcolor=BORDER, linecolor=BORDER),
        legend=dict(bgcolor="rgba(0,0,0,0)", bordercolor=BORDER),
        hoverlabel=dict(bgcolor=SURF2, bordercolor=BORDER, font=dict(color=TEXT)),
    )
    return dcc.Graph(figure=fig, config={"displayModeBar": False},
                     style={"borderRadius": "8px"})

def tbl(df: pd.DataFrame, max_rows=100):
    if df is None or df.empty:
        return empty_msg("No data yet.")

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
        if col == "passed":          return "PASS" if val else "SKIP"
        if col == "pnl_pct":
            try: return f"{float(val):+.2f}%"
            except: pass
        if col in ("pnl_dollar", "change_pct"):
            try: return f"{float(val):+.2f}"
            except: pass
        if isinstance(val, float):   return f"{val:.2f}"
        return str(val)

    header = html.Tr([
        html.Th(c, style={
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
    ("overview",  "Overview",   "▣"),
    ("alerts",    "Alerts",     "◈"),
    ("trades",    "Trades",     "◎"),
    ("scanlog",   "Scan Log",   "◉"),
    ("portfolio", "Portfolio",  "◐"),
]

def sidebar(active, auth):
    links = []
    for pid, label, icon in NAV:
        on = pid == active
        links.append(dcc.Link(href=f"/{pid}", style={
            "display": "flex", "alignItems": "center", "gap": "12px",
            "padding": "10px 20px",
            "color": WHITE if on else MUTED,
            "background": f"{ACCENT}18" if on else "transparent",
            "borderRight": f"3px solid {ACCENT}" if on else "3px solid transparent",
            "textDecoration": "none", "fontSize": "13px",
            "fontWeight": "600" if on else "400", "marginBottom": "2px",
        }, children=[html.Span(icon, style={"fontSize": "14px"}), label]))

    mkt, mkt_c = market_status()
    admin_badge = (
        html.Span(" ADMIN", style={
            "background": YELLOW, "color": BG, "fontSize": "9px",
            "fontWeight": "800", "borderRadius": "4px",
            "padding": "2px 6px", "letterSpacing": "0.5px",
        }) if auth.get("is_admin") else ""
    )

    return html.Div(style={
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
                    html.P("Dashboard", style={"color": MUTED, "fontSize": "10px", "margin": "0"}),
                ]),
            ]),
        ]),
        html.Div(style={
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
                html.P("your account", style={"color": MUTED, "fontSize": "10px", "margin": "0"}),
            ]),
        ]),
        html.Div(links, style={"flex": "1", "padding": "14px 0", "overflowY": "auto"}),
        html.Div(style={"padding": "14px 20px", "borderTop": f"1px solid {BORDER}"}, children=[
            html.Div(style={"display": "flex", "alignItems": "center",
                             "gap": "6px", "marginBottom": "4px"}, children=[
                html.Div(style={"width": "7px", "height": "7px",
                                 "borderRadius": "50%", "background": mkt_c}),
                html.P(mkt, style={"color": mkt_c, "fontSize": "10px",
                                    "fontWeight": "700", "margin": "0"}),
            ]),
            html.P(datetime.now().strftime("%d %b · %H:%M"),
                   style={"color": MUTED, "fontSize": "10px", "margin": "0"}),
        ]),
    ])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PAGES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def page_overview(auth):
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

    kpis = html.Div(style={"display": "flex", "gap": "12px",
                             "flexWrap": "wrap", "marginBottom": "16px"}, children=[
        kpi("Alerts Today",  a_today,      f"{a_total} total",    ACCENT),
        kpi("Win Rate",      f"{wr}%",     f"{t_wins}/{t_total}", GREEN),
        kpi("Net P&L",       fmt_usd(pnl), "all my trades",       pnl_c),
        kpi("Scan Pass Rate",f"{round(s_pass/s_total*100,1) if s_total else 0}%",
                             f"{s_pass} passed", YELLOW),
        kpi("Open Positions",len(port),    "my portfolio",        CYAN),
    ])

    charts = html.Div()
    if a_total:
        alerts["day"] = alerts["alerted_at"].astype(str).str[:10]
        daily = alerts.groupby("day").size().reset_index(name="n").tail(14)
        fig_bar = go.Figure(go.Bar(
            x=daily["day"], y=daily["n"],
            marker=dict(color=ACCENT, line=dict(width=0)),
            hovertemplate="%{x}: <b>%{y} alerts</b><extra></extra>",
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
        charts = html.Div(style={"display": "grid",
                                   "gridTemplateColumns": "2fr 1fr",
                                   "gap": "12px", "marginBottom": "16px"}, children=[
            card([sec("Alert Activity — Last 14 Days"), chart(fig_bar, 240)], mb="0"),
            card([sec("By Session"),                    chart(fig_pie, 240)], mb="0"),
        ])

    my_trades = q(f"SELECT * FROM trades WHERE chat_id = {uid} ORDER BY closed_at DESC")
    cols = [c for c in ["symbol","entry_price","exit_price","qty",
                         "pnl_dollar","pnl_pct","result","trade_date"]
            if c in my_trades.columns]
    recent = card([sec("My Recent Trades"),
                   tbl(my_trades[cols].head(10) if cols else my_trades.head(10))])
    return html.Div([kpis, charts, recent])


def page_alerts(auth):
    df       = q("SELECT * FROM alerts ORDER BY alerted_at DESC")
    total    = len(df)
    today_df = df[df["alerted_at"].astype(str).str[:10] == today_s()] if total else pd.DataFrame()
    today_n  = len(today_df)
    avg_ch   = round(df["change_pct"].mean(), 1) if total and "change_pct" in df.columns else 0
    perf_df  = df[df["outcome"].notna()] if "outcome" in df.columns else pd.DataFrame()
    p_total  = len(perf_df)
    p_pass   = int((perf_df["outcome"] == "PASS").sum()) if p_total else 0
    p_rate   = round(p_pass / p_total * 100, 1) if p_total else 0

    kpis = html.Div(style={"display": "flex", "gap": "12px",
                             "flexWrap": "wrap", "marginBottom": "16px"}, children=[
        kpi("Total Alerts",  total,         "all time",                         ACCENT),
        kpi("Today",         today_n,       "new today",                        CYAN),
        kpi("Avg Change",    f"{avg_ch}%",  "per alert",                        YELLOW),
        kpi("Bot Win Rate",  f"{p_rate}%",  f"{p_pass}/{p_total} alerts worked",GREEN),
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
        charts = html.Div(style={"display": "grid",
                                   "gridTemplateColumns": "1fr 1fr 1fr",
                                   "gap": "12px", "marginBottom": "16px"}, children=[
            card([sec("By Grade"),   chart(fig_g, 260)], mb="0"),
            card([sec("By Session"), chart(fig_s, 260)], mb="0"),
            card([sec("Top Stocks"), chart(fig_t, 260)], mb="0"),
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
                    badge  = html.Span("PASS", style={"background": GREEN, "color": "#000",
                        "borderRadius": "4px", "padding": "2px 8px", "fontSize": "11px", "fontWeight": "700"})
                    pct_el = html.Span(f"{pct_val:+.1f}%" if pct_val is not None else "", style={"color": GREEN})
                elif outcome == "FAIL":
                    badge  = html.Span("FAIL", style={"background": RED, "color": WHITE,
                        "borderRadius": "4px", "padding": "2px 8px", "fontSize": "11px", "fontWeight": "700"})
                    pct_el = html.Span(f"{pct_val:+.1f}%" if pct_val is not None else "", style={"color": RED})
                else:
                    badge  = html.Span("PENDING", style={"background": YELLOW, "color": "#000",
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
                    for c in ["Symbol", "Grade", "Alert Price", "Close Price", "Change", "Outcome"]
                ])),
                html.Tbody(rows),
            ])
            perf_section = card([sec("Today's Alert Performance"), perf_table])

    cols = [c for c in ["symbol","alert_price","grade","change_pct",
                         "float_m","rsi","session","alerted_at","outcome","pct_after_alert"]
            if c in df.columns]
    return html.Div([kpis, perf_section, charts,
                     card([sec("All Alerts"), tbl(df[cols].head(200) if cols else df.head(200))])])


_MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun",
                "Jul","Aug","Sep","Oct","Nov","Dec"]

def _render_trades_content(df, is_admin):
    if df.empty:
        return card(empty_msg("No trades for selected period.\n\nUse  BUY SYMBOL PRICE  and  SELL SYMBOL PRICE  in Telegram."))

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

    kpis = html.Div(style={"display": "flex", "gap": "12px",
                             "flexWrap": "wrap", "marginBottom": "16px"}, children=[
        kpi("Trades",        f"{total}",      f"{wins}W / {losses}L", ACCENT),
        kpi("Win Rate",      f"{wr}%",        "filtered trades",      GREEN),
        kpi("Net P&L",       fmt_usd(pnl),    "filtered trades",      pnl_c),
        kpi("Profit Factor", f"{pf}×",        "gross W ÷ L",          YELLOW),
        kpi("Avg Win",       fmt_usd(avg_w),  "per winner",           GREEN),
        kpi("Avg Loss",      fmt_usd(avg_l),  "per loser",            RED),
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
            fill="tozeroy", fillcolor=f"{ACCENT}22",
            hovertemplate="Trade #%{x}<br><b>%{y:+,.2f}</b><extra></extra>",
        ))

    charts = html.Div(style={"display": "grid", "gridTemplateColumns": "1fr 1fr",
                               "gap": "12px", "marginBottom": "16px"}, children=[
        card([sec("P&L by Symbol"),  chart(fig_sym, 300)], mb="0"),
        card([sec("Cumulative P&L"), chart(fig_cum, 300)], mb="0"),
    ])

    base_cols = ["symbol","entry_price","exit_price","qty",
                 "pnl_dollar","pnl_pct","result","trade_date"]
    show = [c for c in (["user_name"] if is_admin else []) + base_cols if c in df.columns]
    return html.Div([kpis, charts,
                     card([sec("Trade History"), tbl(df[show] if show else df)])])


def page_trades(auth):
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
        html.Span("Period:", style={"color": MUTED, "fontSize": "11px",
                                    "fontWeight": "700", "letterSpacing": "1px",
                                    "textTransform": "uppercase"}),
        dcc.RadioItems(
            id="trades-filter-type",
            options=[
                {"label": " All Time", "value": "all"},
                {"label": " By Day",   "value": "day"},
                {"label": " By Month", "value": "month"},
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


def page_scan(auth):
    df = q("SELECT * FROM scan_log ORDER BY scanned_at DESC")
    if df.empty:
        return card(empty_msg("No scan data yet. Scans run every 5 min during market hours."))

    total   = len(df)
    passed  = int(df["passed"].sum()) if "passed" in df.columns else 0
    skipped = total - passed
    rate    = round(passed / total * 100, 1) if total else 0
    today_n = (len(df[df["scanned_at"].astype(str).str[:10] == today_s()]) if total else 0)

    kpis = html.Div(style={"display": "flex", "gap": "12px",
                             "flexWrap": "wrap", "marginBottom": "16px"}, children=[
        kpi("Total Scanned", total,   "all time",      ACCENT),
        kpi("Passed",        passed,  f"{rate}% rate", GREEN),
        kpi("Filtered Out",  skipped, "didn't qualify",RED),
        kpi("Today",         today_n, "scanned today", YELLOW),
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
        fig_sess.add_trace(go.Bar(name="Passed",   x=sdf["session"],
                                  y=sdf["passed"],  marker_color=GREEN))
        fig_sess.add_trace(go.Bar(name="Filtered", x=sdf["session"],
                                  y=sdf["skipped"], marker_color=RED))
        fig_sess.update_layout(barmode="stack")

    charts = html.Div(style={"display": "grid", "gridTemplateColumns": "3fr 2fr",
                               "gap": "12px", "marginBottom": "16px"}, children=[
        card([sec("Top Filter Reasons"), chart(fig_skip, 320)], mb="0"),
        card([sec("By Session"),          chart(fig_sess, 320)], mb="0"),
    ])

    cols = [c for c in ["symbol","price","change_pct","grade","passed",
                         "skip_reason","session","scanned_at"] if c in df.columns]
    return html.Div([kpis, charts,
                     card([sec("Recent Scan Log"),
                           tbl(df[cols].head(100) if cols else df.head(100))])])


def page_portfolio(auth):
    uid      = int(auth["chat_id"])
    is_admin = auth["is_admin"]

    if is_admin:
        df = q("""
            SELECT p.*, u.name AS user_name
            FROM portfolio p LEFT JOIN users u ON p.chat_id = u.chat_id
        """)
    else:
        df = q(f"SELECT * FROM portfolio WHERE chat_id = {uid}")

    kpis = html.Div(style={"display": "flex", "gap": "12px",
                             "flexWrap": "wrap", "marginBottom": "16px"}, children=[
        kpi("Open Positions", len(df), "tracked by bot", CYAN),
        kpi("With Shares",
            int((df["qty"].notna()).sum()) if not df.empty and "qty" in df.columns else 0,
            "qty recorded", YELLOW),
    ])

    base = ["symbol","entry_price","stop_price","t1_price","t2_price","qty","t1_hit","t2_hit","added_at"]
    show = [c for c in (["user_name"] if is_admin else []) + base
            if not df.empty and c in df.columns]
    return html.Div([kpis, card([sec("Open Positions"), tbl(df[show] if show else df)])])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  APP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PAGE_MAP = {
    "overview":  ("Overview",  page_overview),
    "alerts":    ("Alerts",    page_alerts),
    "trades":    ("Trades",    page_trades),
    "scanlog":   ("Scan Log",  page_scan),
    "portfolio": ("Portfolio", page_portfolio),
}

app = Dash(
    __name__, title="StockBot",
    suppress_callback_exceptions=True,
    external_stylesheets=[
        "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap"
    ],
)

_SHOW_LOGIN = {"display": "flex", "alignItems": "center",
               "justifyContent": "center", "minHeight": "100vh"}
_HIDE       = {"display": "none"}
_SHOW_DASH  = {"display": "block"}

app.layout = html.Div(
    style={"backgroundColor": BG, "fontFamily": "Inter, system-ui, sans-serif",
           "color": TEXT, "minHeight": "100vh"},
    children=[
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="auth", storage_type="local"),
        dcc.Interval(id="tick", interval=30_000),

        html.Div(id="login-section", style=_SHOW_LOGIN, children=[
            html.Div(style={
                "width": "380px", "background": SURFACE,
                "border": f"1px solid {BORDER}",
                "borderRadius": "16px", "padding": "40px",
            }, children=[
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
                    html.P("Sign in to your dashboard",
                           style={"color": MUTED, "margin": 0, "fontSize": "13px"}),
                ]),
                html.Label("Username", style={"color": MUTED, "fontSize": "10px", "fontWeight": "700",
                    "letterSpacing": "1px", "textTransform": "uppercase",
                    "display": "block", "marginBottom": "6px"}),
                dcc.Input(id="login-user", type="text", placeholder="Your Telegram name",
                          debounce=False, n_submit=0, style=_INPUT),
                html.Label("PIN", style={"color": MUTED, "fontSize": "10px", "fontWeight": "700",
                    "letterSpacing": "1px", "textTransform": "uppercase",
                    "display": "block", "marginBottom": "6px"}),
                dcc.Input(id="login-pin", type="password", placeholder="4-digit PIN",
                          debounce=False, n_submit=0, style=_INPUT),
                html.Button("Sign In", id="login-btn", n_clicks=0, style={
                    "width": "100%", "background": ACCENT, "color": WHITE,
                    "border": "none", "borderRadius": "8px", "padding": "13px",
                    "fontSize": "14px", "fontWeight": "700",
                    "cursor": "pointer", "letterSpacing": "0.5px", "marginBottom": "14px",
                }),
                html.Div(id="login-error", style={"color": RED, "fontSize": "12px",
                                                   "textAlign": "center", "minHeight": "18px"}),
                html.P("Default PIN: 1234  ·  Change with /setpin in Telegram",
                       style={"color": MUTED, "fontSize": "11px", "textAlign": "center",
                              "marginTop": "20px", "marginBottom": 0,
                              "borderTop": f"1px solid {BORDER}", "paddingTop": "16px"}),
            ]),
        ]),

        html.Div(id="dash-wrap", style=_HIDE, children=[
            html.Div(style={"display": "flex", "minHeight": "100vh"}, children=[
                html.Div(id="sidebar-wrap"),
                html.Div(style={"flex":"1","display":"flex",
                                 "flexDirection":"column","minWidth":0}, children=[
                    html.Div(style={
                        "background": SURFACE, "borderBottom": f"1px solid {BORDER}",
                        "padding": "13px 28px", "display": "flex",
                        "alignItems": "center", "justifyContent": "space-between",
                    }, children=[
                        html.H2(id="page-title", style={"color": WHITE, "margin": 0,
                                                         "fontSize": "15px", "fontWeight": "700"}),
                        html.Div(style={"display":"flex","alignItems":"center","gap":"18px"}, children=[
                            html.Div(id="mkt-badge"),
                            html.Div(id="user-badge"),
                            html.Button("Sign out", id="logout-btn", n_clicks=0, style={
                                "background": "transparent",
                                "border": f"1px solid {BORDER}",
                                "borderRadius": "6px", "padding": "6px 14px",
                                "color": MUTED, "fontSize": "11px", "cursor": "pointer",
                            }),
                        ]),
                    ]),
                    html.Div(id="page-content", style={"flex":"1","padding":"24px 28px","overflowY":"auto"}),
                ]),
            ]),
        ]),
    ]
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CALLBACKS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.callback(
    Output("trades-body",         "children"),
    Output("trades-filter-day",   "style"),
    Output("trades-filter-month", "style"),
    Input("trades-filter-type",   "value"),
    Input("trades-filter-day",    "value"),
    Input("trades-filter-month",  "value"),
    Input("auth", "data"),
    prevent_initial_call=False,
)
def update_trades_content(filter_type, sel_day, sel_month, auth):
    if not auth or not auth.get("chat_id"):
        return no_update, no_update, no_update

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

    return _render_trades_content(df, is_admin), day_style, month_style


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
    Input("tick",  "n_intervals"),
)
def route(pathname, auth, _):
    if not auth or not auth.get("chat_id"):
        return _SHOW_LOGIN, _HIDE, [], "", "", "", ""

    page = (pathname or "/").strip("/") or "overview"
    if page not in PAGE_MAP:
        page = "overview"

    title, fn = PAGE_MAP[page]
    content   = fn(auth)
    sb        = sidebar(page, auth)

    mkt, mkt_c = market_status()
    mkt_el = html.Div(style={"display":"flex","alignItems":"center","gap":"6px"}, children=[
        html.Div(style={"width":"7px","height":"7px","borderRadius":"50%","background":mkt_c}),
        html.Span(mkt, style={"color":mkt_c,"fontSize":"11px","fontWeight":"600"}),
    ])
    user_el = html.Div(style={"display":"flex","alignItems":"center","gap":"6px"}, children=[
        html.Span("👑 " if auth["is_admin"] else "", style={"fontSize":"12px"}),
        html.Span(auth["name"], style={"color":TEXT,"fontSize":"12px","fontWeight":"600"}),
    ])
    return _HIDE, _SHOW_DASH, sb, title, mkt_el, user_el, content


@app.callback(
    Output("auth",        "data"),
    Output("login-error", "children"),
    Input("login-btn",    "n_clicks"),
    Input("login-pin",    "n_submit"),
    State("login-user",   "value"),
    State("login-pin",    "value"),
    prevent_initial_call=True,
)
def do_login(n_btn, n_enter, username, pin):
    user = check_login(username or "", pin or "")
    if user:
        return user, ""
    return no_update, "Incorrect name or PIN. Check your Telegram name and try again."


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
    print(f"StockBot Dashboard  ->  http://0.0.0.0:8050")
    print("Login: Admin / 1234")
    app.run(debug=False, host="0.0.0.0", port=8050)
