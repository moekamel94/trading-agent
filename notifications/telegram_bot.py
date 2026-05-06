"""
Kimmy — Telegram bot (replaces Discord).

Why Telegram instead of Discord:
  - Same alert delivery, zero per-message infrastructure cost
  - Commands (/portfolio, /positions, etc.) bypass Claude entirely
  - AI chat uses Haiku instead of Sonnet — ~75% cheaper per interaction
  - Single unified chat, no channel-routing overhead

Cost profile vs Discord:
  - Alerts (trade signals, reports):  FREE  (direct HTTP to Telegram API)
  - Simple commands:                  FREE  (no Claude call)
  - AI chat:                          Haiku (~$0.01-0.04/interaction vs $0.10-0.30 on Sonnet)

Start: python main.py --telegram
"""
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import requests

import config

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_TOKEN   = config.TELEGRAM_BOT_TOKEN
_CHAT_ID = config.TELEGRAM_CHAT_ID
_API     = f"https://api.telegram.org/bot{_TOKEN}"

# ---------------------------------------------------------------------------
# send() — works from any thread, no event loop required (used by all cycles)
# ---------------------------------------------------------------------------

def send(text: str):
    if not _TOKEN or not _CHAT_ID:
        print(f"  [Telegram] Not configured — alert dropped:\n{text[:80]}")
        return
    clean = re.sub(r"<[^>]+>", " ", text).strip()
    chunks = [clean[i:i + 4000] for i in range(0, len(clean), 4000)]
    for chunk in chunks:
        try:
            r = requests.post(
                f"{_API}/sendMessage",
                json={"chat_id": _CHAT_ID, "text": chunk},
                timeout=10,
            )
            if r.status_code not in (200, 201):
                print(f"  [Telegram] Error {r.status_code}: {r.text[:100]}")
                try:
                    from monitoring.self_healer import queue_discord_message
                    queue_discord_message(chunk)
                except Exception:
                    pass
        except Exception as e:
            print(f"  [Telegram] send() failed: {e}")
            try:
                from monitoring.self_healer import queue_discord_message
                queue_discord_message(chunk)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Persistent memory — same SQLite schema as discord_bot (shared DB)
# ---------------------------------------------------------------------------

_MEM_DB = os.path.join(os.path.dirname(__file__), "..", "kimmy_memory.db")


def _mem_init():
    with sqlite3.connect(_MEM_DB) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                channel INTEGER NOT NULL,
                role    TEXT    NOT NULL,
                content TEXT    NOT NULL,
                ts      TEXT    NOT NULL
            )
        """)


def _mem_load(chat_id: int, limit: int = 20) -> list:
    with sqlite3.connect(_MEM_DB) as c:
        rows = c.execute(
            "SELECT role, content FROM history WHERE channel=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def _mem_save(chat_id: int, role: str, content: str):
    with sqlite3.connect(_MEM_DB) as c:
        c.execute(
            "INSERT INTO history (channel, role, content, ts) VALUES (?,?,?,?)",
            (chat_id, role, content, datetime.now(timezone.utc).isoformat()),
        )


def _mem_clear(chat_id: int):
    with sqlite3.connect(_MEM_DB) as c:
        c.execute("DELETE FROM history WHERE channel=?", (chat_id,))


_mem_init()

# ---------------------------------------------------------------------------
# Concurrency guards
# ---------------------------------------------------------------------------

_chat_locks: dict[int, asyncio.Lock] = {}
_processed_msg_ids: set[int] = set()

# ---------------------------------------------------------------------------
# Pending approvals
# ---------------------------------------------------------------------------

_pending: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are Kimmy — Mohammed's personal AI assistant, running on his Telegram.\n"
    "Mohammed travels and relies on you as his primary AI assistant. Treat every request seriously.\n\n"
    "You can do EVERYTHING:\n"
    "- Answer any question on any topic\n"
    "- Search the web for up-to-date information\n"
    "- Read web pages and URLs\n"
    "- Write, debug, and explain code in any language\n"
    "- Run shell commands on the cloud server\n"
    "- Read and write files on the server\n"
    "- Execute Python code snippets\n"
    "- Manage his trading agent (portfolio, positions, trades)\n"
    "- Help with any task, plan, or decision\n\n"
    "Rules:\n"
    "- For shell commands or file writes: use request_approval first\n"
    "- For read-only operations: no approval needed\n"
    "- Always use web_search when you need current information\n"
    "- Be thorough — if a task needs multiple steps, do all of them\n"
    "- Keep responses concise (this is Telegram, not a document editor)\n"
    "- NEVER paste code in replies. Run it silently with tools, then report the RESULT.\n"
    "- End with '✅ Done.' when fully complete. End with '⛔ Stopped — [reason].' on failure.\n"
    f"Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC."
)

# ---------------------------------------------------------------------------
# Tools (same set as discord_bot)
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web for current information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Fetch and read the content of any URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "run_python",
        "description": "Execute a Python code snippet on the server.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "request_approval",
        "description": "Ask Mohammed to approve an action before executing it.",
        "input_schema": {
            "type": "object",
            "properties": {"action_description": {"type": "string"}},
            "required": ["action_description"],
        },
    },
    {
        "name": "run_shell_command",
        "description": "Run a shell command on the server.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command":     {"type": "string"},
                "working_dir": {"type": "string"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file on the server.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file on the server. Use request_approval first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files in a directory on the server.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "set_env_var",
        "description": "Add/update an environment variable in .env. Use request_approval first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key":   {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "list_env_vars",
        "description": "List all environment variable names in .env (values hidden).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_portfolio",
        "description": "Get current portfolio equity, cash, and buying power.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_positions",
        "description": "Get all open positions with entry price and P&L.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_trade_history",
        "description": "Get recent trade history.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": [],
        },
    },
    {
        "name": "run_trading_cycle",
        "description": "Trigger a full trading cycle. Always use request_approval first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "trade_option",
        "description": (
            "Place a manual option trade with auto-sell target. "
            "Always use request_approval first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol":       {"type": "string"},
                "direction":    {"type": "string"},
                "expiry":       {"type": "string"},
                "strike":       {"type": "number"},
                "entry_price":  {"type": "number"},
                "target_price": {"type": "number"},
                "qty":          {"type": "integer"},
            },
            "required": ["symbol", "direction", "expiry", "strike", "entry_price", "target_price"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool execution (same logic as discord_bot)
# ---------------------------------------------------------------------------

_DEFAULT_DIR = "/opt/trading-agent"


async def _exec_tool(name: str, inputs: dict, send_msg=None) -> str:
    try:
        if name == "web_search":
            from duckduckgo_search import DDGS
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(inputs["query"], max_results=inputs.get("max_results", 5)):
                    results.append(f"{r['title']}\n{r['href']}\n{r['body']}")
            return "\n---\n".join(results) if results else "No results."

        elif name == "fetch_url":
            import urllib.request
            req = urllib.request.Request(inputs["url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            clean = re.sub(r"<[^>]+>", " ", raw)
            return re.sub(r"\s+", " ", clean).strip()[:4000]

        elif name == "run_python":
            result = subprocess.run(
                [sys.executable, "-c", inputs["code"]],
                capture_output=True, text=True, timeout=30, cwd=_DEFAULT_DIR,
            )
            out = (result.stdout + result.stderr).strip()
            return out[:3000] if out else "(no output)"

        elif name == "request_approval":
            if send_msg:
                return await _send_approval(send_msg, inputs["action_description"])
            return "approved"

        elif name == "run_shell_command":
            cwd = inputs.get("working_dir") or _DEFAULT_DIR
            result = subprocess.run(
                inputs.get("command", ""), shell=True,
                capture_output=True, text=True, timeout=60, cwd=cwd,
            )
            out = (result.stdout + result.stderr).strip()
            return out[:3000] if out else "(no output)"

        elif name == "read_file":
            with open(inputs["path"], "r", encoding="utf-8", errors="replace") as f:
                return f.read()[:4000]

        elif name == "write_file":
            os.makedirs(os.path.dirname(os.path.abspath(inputs["path"])), exist_ok=True)
            with open(inputs["path"], "w", encoding="utf-8") as f:
                f.write(inputs["content"])
            return f"Written: {inputs['path']}"

        elif name == "list_directory":
            path = inputs.get("path") or _DEFAULT_DIR
            return "\n".join(sorted(os.listdir(path)))

        elif name == "set_env_var":
            env_path = os.path.join(_DEFAULT_DIR, ".env")
            key, value = inputs["key"].strip().upper(), inputs["value"].strip()
            lines = open(env_path).readlines() if os.path.exists(env_path) else []
            found = False
            for i, line in enumerate(lines):
                if line.startswith(f"{key}="):
                    lines[i] = f"{key}={value}\n"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={value}\n")
            with open(env_path, "w") as f:
                f.writelines(lines)
            subprocess.Popen(["systemctl", "restart", "kimmy"])
            return f"Set {key} and restarting."

        elif name == "list_env_vars":
            env_path = os.path.join(_DEFAULT_DIR, ".env")
            if not os.path.exists(env_path):
                return "No .env file."
            keys = [l.split("=")[0] for l in open(env_path) if "=" in l and not l.startswith("#")]
            return "Variables:\n" + "\n".join(f"• {k}" for k in keys)

        elif name == "get_portfolio":
            from broker import alpaca
            return json.dumps(alpaca.get_portfolio())

        elif name == "get_positions":
            from broker import alpaca
            pos = alpaca.get_positions()
            return json.dumps(pos) if pos else "No open positions."

        elif name == "get_trade_history":
            import database.db as db
            return json.dumps(db.get_recent_trades(inputs.get("limit", 10)), default=str)

        elif name == "run_trading_cycle":
            subprocess.Popen([sys.executable, "main.py", "--force-opus"], cwd=_DEFAULT_DIR)
            return "Trading cycle started with Opus (manual run)."

        elif name == "trade_option":
            import sys as _sys
            _sys.path.insert(0, _DEFAULT_DIR)
            from broker import alpaca as _alp
            from database import options_positions as _op_db
            from datetime import datetime as _dt

            sym      = inputs["symbol"].upper().strip()
            dirn     = inputs["direction"].lower().strip()
            expiry_r = str(inputs["expiry"]).strip()
            strike   = float(inputs["strike"])
            entry_p  = float(inputs["entry_price"])
            target_p = float(inputs["target_price"])
            qty      = int(inputs.get("qty") or 1)

            expiry_dt = None
            for fmt in ("%Y-%m-%d", "%b %d %Y", "%B %d %Y", "%b %d", "%B %d", "%m/%d/%Y", "%m/%d"):
                try:
                    parsed = _dt.strptime(expiry_r, fmt)
                    if parsed.year == 1900:
                        parsed = parsed.replace(year=_dt.now().year)
                        if parsed.date() < _dt.now().date():
                            parsed = parsed.replace(year=_dt.now().year + 1)
                    expiry_dt = parsed.date()
                    break
                except ValueError:
                    continue
            if expiry_dt is None:
                return f"Could not parse expiry: '{expiry_r}'"

            contract = _alp.build_occ_symbol(sym, expiry_dt, dirn, strike)
            upside   = round((target_p - entry_p) / entry_p * 100, 1)
            cost     = round(entry_p * qty * 100, 2)

            try:
                order = _alp.place_option_limit_order(contract, qty, "BUY", entry_p)
                order_id = str(getattr(order, "id", "n/a"))
            except Exception as e:
                return f"Order failed: {e}\nContract: {contract}"

            trade_id = _op_db.log_live_trade(
                contract_symbol=contract, symbol=sym, direction=dirn,
                expiry=str(expiry_dt), strike=strike,
                entry_price=entry_p, target_price=target_p, qty=qty,
            )
            return (
                f"✅ Option order placed!\n"
                f"Contract: {contract}\n"
                f"{sym} {dirn.upper()} ${strike:.2f} exp {expiry_dt}\n"
                f"Limit buy: ${entry_p:.2f} × {qty} = ${cost:,.2f}\n"
                f"Auto-sell at: ${target_p:.2f} (+{upside:.1f}%)\n"
                f"Order: {order_id} | Trade #{trade_id}"
            )

        return f"Unknown tool: {name}"

    except Exception as e:
        return f"Error ({name}): {e}"


# ---------------------------------------------------------------------------
# Approval via inline keyboard
# ---------------------------------------------------------------------------

async def _send_approval(send_msg, description: str) -> str:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    aid = uuid.uuid4().hex[:8]
    event = asyncio.Event()
    _pending[aid] = {"event": event, "approved": False}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{aid}"),
        InlineKeyboardButton("❌ Deny",    callback_data=f"deny:{aid}"),
    ]])
    await send_msg(f"⚠️ Approval needed\n\n{description}", reply_markup=keyboard)
    try:
        await asyncio.wait_for(event.wait(), timeout=120.0)
        return "approved" if _pending[aid]["approved"] else "denied"
    except asyncio.TimeoutError:
        await send_msg("⏰ Approval timed out — stopped.")
        return "denied"
    finally:
        _pending.pop(aid, None)


# ---------------------------------------------------------------------------
# Claude conversation (Haiku — ~75% cheaper than Sonnet)
# ---------------------------------------------------------------------------

_MAX_TOOL_ROUNDS = 15


async def _ask_claude(chat_id: int, user_text: str, send_msg=None) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    _mem_save(chat_id, "user", user_text)
    messages = _mem_load(chat_id, limit=20)

    rounds = 0
    while True:
        rounds += 1
        if rounds > _MAX_TOOL_ROUNDS:
            stop = (
                "⛔ Stopped — too many steps without finishing. "
                "Please check and tell me how to continue."
            )
            _mem_save(chat_id, "assistant", stop)
            return stop

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await _exec_tool(block.name, block.input, send_msg=send_msg)
                    if block.name == "request_approval" and result == "denied":
                        stop = "⛔ Stopped — you denied the action."
                        _mem_save(chat_id, "assistant", stop)
                        return stop
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})

        else:
            reply = "".join(getattr(b, "text", "") for b in response.content).strip()
            _mem_save(chat_id, "assistant", reply)
            return reply or "..."


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------

async def _handle_message(update, context):
    """Handle all non-command messages — calls Claude Haiku."""
    msg = update.message
    if not msg or not msg.text:
        return

    # Security: only respond to the configured chat
    if str(msg.chat_id) != str(_CHAT_ID):
        return

    # Dedup
    if msg.message_id in _processed_msg_ids:
        return
    _processed_msg_ids.add(msg.message_id)
    if len(_processed_msg_ids) > 2000:
        for mid in list(_processed_msg_ids)[:1000]:
            _processed_msg_ids.discard(mid)

    chat_id = msg.chat_id
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    lock = _chat_locks[chat_id]

    if lock.locked():
        await msg.reply_text("⏳ Still working on your previous request — please wait.")
        return

    async def _send(text, **kwargs):
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)

    async with lock:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        try:
            reply = await _ask_claude(chat_id, msg.text, send_msg=_send)
            for chunk in [reply[i:i+4000] for i in range(0, len(reply), 4000)]:
                await msg.reply_text(chunk)
        except Exception as e:
            await msg.reply_text(f"⛔ Error: {e}\nPlease check and tell me how to continue.")


async def _handle_approval_callback(update, context):
    """Handle Approve/Deny button taps."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if ":" not in data:
        return
    action, aid = data.split(":", 1)
    if aid not in _pending:
        await query.edit_message_text(query.message.text + "\n\n(Expired — action already handled.)")
        return
    approved = action == "approve"
    _pending[aid]["approved"] = approved
    _pending[aid]["event"].set()
    label = "✅ Approved — executing..." if approved else "❌ Denied — stopped."
    await query.edit_message_text(query.message.text + f"\n\n{label}")


# ---------------------------------------------------------------------------
# Command handlers (no Claude — free to run)
# ---------------------------------------------------------------------------

async def _cmd_portfolio(update, context):
    if str(update.message.chat_id) != str(_CHAT_ID):
        return
    try:
        from broker import alpaca
        p = alpaca.get_portfolio()
        eq   = p.get("equity", 0)
        cash = p.get("cash", 0)
        bp   = p.get("buying_power", 0)
        await update.message.reply_text(
            f"💼 Portfolio\n"
            f"Equity:        ${eq:,.2f}\n"
            f"Cash:          ${cash:,.2f}\n"
            f"Buying power:  ${bp:,.2f}\n"
            f"Deployed:      {(1 - cash/eq)*100:.1f}%" if eq else "N/A"
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def _cmd_positions(update, context):
    if str(update.message.chat_id) != str(_CHAT_ID):
        return
    try:
        from broker import alpaca
        positions = alpaca.get_positions()
        if not positions:
            await update.message.reply_text("No open positions.")
            return
        lines = ["📊 Open Positions\n"]
        for p in positions:
            sym  = p.get("symbol", "?")
            pct  = p.get("pct", 0)
            pl   = p.get("pl_pct", 0)
            tier = p.get("tier", "?")
            lines.append(f"{sym:<6}  {pct:.1f}%  P&L: {pl:+.1f}%  [{tier}]")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def _cmd_status(update, context):
    if str(update.message.chat_id) != str(_CHAT_ID):
        return
    await update.message.reply_text(
        f"✅ Kimmy online\n"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"Running on DigitalOcean"
    )


async def _cmd_clear(update, context):
    if str(update.message.chat_id) != str(_CHAT_ID):
        return
    _mem_clear(update.message.chat_id)
    await update.message.reply_text("Memory cleared.")


async def _cmd_help(update, context):
    if str(update.message.chat_id) != str(_CHAT_ID):
        return
    await update.message.reply_text(
        "Kimmy — Your Personal AI Assistant\n\n"
        "Just type anything to chat.\n\n"
        "Commands (instant, no AI cost):\n"
        "/portfolio — equity, cash, buying power\n"
        "/positions — open positions and P&L\n"
        "/status — bot health check\n"
        "/clear — reset conversation memory\n"
        "/veto_removal TICKER — cancel a pending basket removal\n"
        "/pending_removals — show positions flagged for removal\n"
        "/help — this message\n\n"
        "AI chat → Claude Haiku (fast, low cost)"
    )


async def _cmd_veto(update, context):
    if str(update.message.chat_id) != str(_CHAT_ID):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /veto_removal TICKER")
        return
    sym = args[0].upper().strip()
    try:
        from basket.pending_removals import cancel, get_all
        pending = get_all()
        if sym not in pending:
            await update.message.reply_text(
                f"{sym} is not in pending removals.\n"
                f"Current pending: {', '.join(pending.keys()) or 'none'}"
            )
        else:
            cancel(sym)
            await update.message.reply_text(
                f"✅ Veto applied — {sym} will be kept in the MT basket."
            )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def _cmd_pending(update, context):
    if str(update.message.chat_id) != str(_CHAT_ID):
        return
    try:
        from basket.pending_removals import summary_lines
        lines = summary_lines()
        if not lines:
            await update.message.reply_text("No pending MT basket removals.")
        else:
            await update.message.reply_text(
                "Pending MT basket removals (24h veto window):\n" + "\n".join(lines)
            )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


# ---------------------------------------------------------------------------
# run_bot()
# ---------------------------------------------------------------------------

def run_bot():
    if not _TOKEN:
        print("[Telegram] TELEGRAM_BOT_TOKEN not set.")
        return

    from telegram.ext import (
        Application, CommandHandler, MessageHandler,
        CallbackQueryHandler, filters,
    )

    app = Application.builder().token(_TOKEN).build()

    app.add_handler(CommandHandler("portfolio",        _cmd_portfolio))
    app.add_handler(CommandHandler("positions",        _cmd_positions))
    app.add_handler(CommandHandler("status",           _cmd_status))
    app.add_handler(CommandHandler("clear",            _cmd_clear))
    app.add_handler(CommandHandler("help",             _cmd_help))
    app.add_handler(CommandHandler("veto_removal",     _cmd_veto))
    app.add_handler(CommandHandler("pending_removals", _cmd_pending))
    app.add_handler(CallbackQueryHandler(_handle_approval_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))

    print(f"[Telegram] Kimmy starting (chat_id={_CHAT_ID})...")
    app.run_polling(drop_pending_updates=True)
