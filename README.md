# StockBot

Small-cap momentum scanner for US pre-market / open / after-hours, with a
Telegram bot and a bilingual (EN/AR) web dashboard — all in **one file**.

## Run it

Everything lives in `main.py` (bot + SQLite database + web dashboard).

```bash
pip install -r requirements.txt
python main.py
```

That single command:
1. creates the SQLite tables (`stockbot.db`) if missing,
2. starts the web dashboard in a background thread (binds `$PORT`, else `8050`),
3. runs the Telegram scanner in the foreground.

## Configuration (environment variables)

| Variable            | Required | Purpose                                              |
|---------------------|----------|------------------------------------------------------|
| `BOT_TOKEN`         | yes      | Telegram bot token                                   |
| `ANTHROPIC_API_KEY` | optional | Enables Claude pump-and-dump checks on alerts        |
| `PORT`              | optional | Dashboard port (Railway sets this automatically)     |
| `DB_PATH`           | optional | SQLite path; point at a mounted volume to persist    |

## Deploy on Railway

The repo is wired for Railway via the `Procfile`:

```
web: python main.py
```

Push to `main` and Railway redeploys. Set `BOT_TOKEN` (and optionally
`ANTHROPIC_API_KEY`) in the Railway service **Variables**.

> **Data persistence:** Railway's filesystem is ephemeral, so `stockbot.db`
> resets on each deploy unless you attach a **Volume** and set `DB_PATH` to a
> path inside it (e.g. `/data/stockbot.db`).

## Run on your own Linux server

```bash
pip install -r requirements.txt
python main.py
# or as an always-on service, point a systemd unit's ExecStart at `python main.py`
```

## Dashboard

Open the dashboard URL and log in with your Telegram name + PIN (default `1234`,
change with `/setpin` in Telegram). Use the top-bar toggle to switch between
**English** and **العربية** (full right-to-left). Works on desktop, iPad and mobile.

## Archive

The previous multi-file layout (`stock_scanner.py`, `dashboard.py`, `db.py`,
the `linux/` deploy scripts and `start_railway.sh`) was merged into `main.py`
and moved to [`archive/`](archive/) for reference.
