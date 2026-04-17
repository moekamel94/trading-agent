"""
Telegram bot — Claude-powered assistant with full tool access and phone approvals.
1. send()  : push alerts from anywhere in the agent
2. run_bot(): interactive Claude assistant with approval buttons
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import config

logging.basicConfig(level=logging.WARNING)

# Conversation history per chat
_history: dict[int, list] = {}

# Pending approval requests: approval_id -> {"event": Event, "approved": bool}
_pending: dict[str, dict] = {}

_SYSTEM = (
    "You are Mohammed's personal AI assistant — like Claude Code but running on his phone via Telegram.\n"
    "You can do anything: answer questions, write code, run shell commands, read/write files, manage his trading agent.\n"
    "You have tools for:\n"
    "- Shell commands (requires phone approval for anything that changes state)\n"
    "- Reading and listing files\n"
    "- Writing/editing files (requires phone approval)\n"
    "- Trading: portfolio, positions, BTC, trade history, run cycle\n\n"
    "For risky actions, always use the approval tool first — Mohammed will tap Approve or Deny on his phone.\n"
    "Keep replies short and clear — this is a phone chat.\n"
    f"Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC."
)

_TOOLS = [
    # ── Trading ──────────────────────────────────────────────────────────────
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
        "name": "get_btc",
        "description": "Get BTC/USD price, RSI, and any open BTC position.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_trade_history",
        "description": "Get recent trade history from the database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of trades (default 10)"}
            },
            "required": [],
        },
    },
    {
        "name": "run_trading_cycle",
        "description": "Trigger a full trading cycle (scans watchlist, places paper trades). Requires approval.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    # ── System / Files ────────────────────────────────────────────────────────
    {
        "name": "request_approval",
        "description": (
            "Ask Mohammed to approve an action on his phone before doing it. "
            "Use this before running shell commands that change state, writing files, or anything risky. "
            "Returns 'approved' or 'denied'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_description": {
                    "type": "string",
                    "description": "Clear one-line description of what you are about to do."
                }
            },
            "required": ["action_description"],
        },
    },
    {
        "name": "run_shell_command",
        "description": "Run a shell command and return stdout+stderr. Always request_approval first for state-changing commands.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
                "working_dir": {"type": "string", "description": "Working directory (optional)."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Always request_approval first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "File path to write."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and folders in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: trading-agent folder)."}
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Approval helpers
# ---------------------------------------------------------------------------

async def _send_approval_request(bot, chat_id: int, description: str) -> bool:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    aid = uuid.uuid4().hex[:8]
    event = asyncio.Event()
    _pending[aid] = {"event": event, "approved": False}

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{aid}"),
        InlineKeyboardButton("❌ Deny",    callback_data=f"deny_{aid}"),
    ]])

    await bot.send_message(
        chat_id=chat_id,
        text=f"⚠️ <b>Approval needed</b>\n\n<code>{description}</code>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    try:
        await asyncio.wait_for(event.wait(), timeout=120.0)
        return _pending[aid]["approved"]
    except asyncio.TimeoutError:
        return False
    finally:
        _pending.pop(aid, None)


# ---------------------------------------------------------------------------
# Tool execution (async so it can await approvals)
# ---------------------------------------------------------------------------

async def _exec_tool(name: str, inputs: dict, bot=None, chat_id: int = 0) -> str:
    try:
        # ── Trading tools ────────────────────────────────────────────────────
        if name == "get_portfolio":
            from broker import alpaca
            return json.dumps(alpaca.get_portfolio())

        elif name == "get_positions":
            from broker import alpaca
            positions = alpaca.get_positions()
            return json.dumps(positions) if positions else "No open positions."

        elif name == "get_btc":
            from broker import alpaca
            from signals import technical
            bars = alpaca.get_crypto_bars("BTC/USD")
            tech = technical.compute(bars)
            positions = alpaca.get_positions()
            btc = next((p for p in positions if "BTC" in p["symbol"]), None)
            return json.dumps({"price": tech.get("price"), "rsi": tech.get("rsi"), "position": btc})

        elif name == "get_trade_history":
            import database.db as db
            return json.dumps(db.get_recent_trades(inputs.get("limit", 10)), default=str)

        elif name == "run_trading_cycle":
            subprocess.Popen([sys.executable, "main.py"])
            return "Trading cycle started. You'll receive alerts as trades execute."

        # ── Approval ─────────────────────────────────────────────────────────
        elif name == "request_approval":
            if bot and chat_id:
                approved = await _send_approval_request(bot, chat_id, inputs["action_description"])
                return "approved" if approved else "denied"
            return "approved"  # fallback if no bot context (e.g. tests)

        # ── System / Files ────────────────────────────────────────────────────
        elif name == "run_shell_command":
            command = inputs.get("command", "")
            cwd = inputs.get("working_dir") or os.path.dirname(os.path.abspath(__file__ + "/.."))
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=60, cwd=cwd,
            )
            output = (result.stdout + result.stderr).strip()
            return output[:3000] if output else "(no output)"

        elif name == "read_file":
            path = inputs.get("path", "")
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return content[:4000] if len(content) > 4000 else content

        elif name == "write_file":
            path = inputs.get("path", "")
            content = inputs.get("content", "")
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Written: {path}"

        elif name == "list_directory":
            path = inputs.get("path") or os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..")
            )
            items = sorted(os.listdir(path))
            return "\n".join(items)

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Tool error ({name}): {e}"


# ---------------------------------------------------------------------------
# Claude conversation loop
# ---------------------------------------------------------------------------

async def _ask_claude(chat_id: int, user_text: str, bot=None) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    history = _history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    if len(history) > 20:
        history[:] = history[-20:]

    messages = list(history)

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=_SYSTEM,
            tools=_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await _exec_tool(block.name, block.input, bot=bot, chat_id=chat_id)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            reply = "".join(getattr(b, "text", "") for b in response.content).strip()
            history.append({"role": "assistant", "content": reply})
            return reply or "..."


# ---------------------------------------------------------------------------
# Push a message (called from anywhere in the agent)
# ---------------------------------------------------------------------------

def send(text: str):
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        print(f"  [Telegram] Not configured — message not sent:\n{text[:80]}")
        return
    try:
        from telegram import Bot
        async def _send():
            async with Bot(config.TELEGRAM_TOKEN) as bot:
                await bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=text,
                    parse_mode="HTML",
                )
        asyncio.run(_send())
        print("  [Telegram] Message sent.")
    except Exception as e:
        print(f"  [Telegram] Send failed: {e}")


# ---------------------------------------------------------------------------
# Interactive bot
# ---------------------------------------------------------------------------

def run_bot():
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[Telegram] TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set.")
        return

    from telegram import Update
    from telegram.ext import (
        Application, CallbackQueryHandler, CommandHandler,
        ContextTypes, MessageHandler, filters,
    )

    CHAT = int(config.TELEGRAM_CHAT_ID)

    def _auth(update: Update) -> bool:
        return update.effective_chat.id == CHAT

    # ── Approval callback ────────────────────────────────────────────────────
    async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data or ""

        if data.startswith(("approve_", "deny_")):
            action, aid = data.split("_", 1)
            if aid in _pending:
                _pending[aid]["approved"] = (action == "approve")
                _pending[aid]["event"].set()
                label = "✅ Approved" if action == "approve" else "❌ Denied"
                await query.edit_message_text(
                    query.message.text + f"\n\n{label}",
                    parse_mode="HTML",
                )

    # ── Commands ─────────────────────────────────────────────────────────────
    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        _history.pop(update.effective_chat.id, None)
        await update.message.reply_text(
            "Hey Mohammed! I'm Jarvis — your AI assistant.\n\n"
            "Ask me anything or give me tasks. I can:\n"
            "• Answer any question\n"
            "• Run shell commands on your PC\n"
            "• Read and write files\n"
            "• Manage your trading agent\n\n"
            "For risky actions I'll ask for your approval first.\n\n"
            "Quick commands:\n"
            "/portfolio /positions /btc /run /status /clear"
        )

    async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        _history.pop(update.effective_chat.id, None)
        await update.message.reply_text("Conversation cleared.")

    async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        try:
            from broker import alpaca
            p = alpaca.get_portfolio()
            await update.message.reply_text(
                f"Portfolio\nEquity:       ${p['equity']:,.2f}\n"
                f"Cash:         ${p['cash']:,.2f}\n"
                f"Buying power: ${p['buying_power']:,.2f}"
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        try:
            from broker import alpaca
            positions = alpaca.get_positions()
            if not positions:
                await update.message.reply_text("No open positions.")
                return
            lines = ["Open Positions:"]
            for p in positions:
                sign = "+" if p["unrealized_plpc"] >= 0 else ""
                lines.append(
                    f"  {p['symbol']}: {p['qty']:.4f} @ ${p['avg_entry']:.2f}"
                    f" | ${p['current_price']:.2f} | {sign}{p['unrealized_plpc']:.2f}%"
                )
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_btc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        try:
            from broker import alpaca
            from signals import technical
            bars = alpaca.get_crypto_bars("BTC/USD")
            tech = technical.compute(bars)
            positions = alpaca.get_positions()
            btc = next((p for p in positions if "BTC" in p["symbol"]), None)
            price = tech.get("price", 0)
            msg = f"BTC/USD\nPrice: ${price:,.2f}\nRSI: {tech.get('rsi','N/A')}"
            if btc:
                sign = "+" if btc["unrealized_plpc"] >= 0 else ""
                msg += f"\nP&L: {sign}{btc['unrealized_plpc']:.2f}%"
            else:
                msg += "\nNo BTC position."
            await update.message.reply_text(msg)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        await update.message.reply_text("Starting trading cycle...")
        try:
            subprocess.Popen([sys.executable, "main.py"])
            await update.message.reply_text("Cycle started. Alerts will follow.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        await update.message.reply_text(
            f"Jarvis online\n"
            f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"
            f"All systems running."
        )

    # ── Free text → Claude ───────────────────────────────────────────────────
    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        try:
            reply = await _ask_claude(
                update.effective_chat.id,
                update.message.text,
                bot=ctx.bot,
            )
            for i in range(0, len(reply), 4096):
                await update.message.reply_text(reply[i:i + 4096])
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("btc",       cmd_btc))
    app.add_handler(CommandHandler("run",       cmd_run))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("[Telegram] Jarvis online — listening for commands...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
