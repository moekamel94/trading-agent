"""
Telegram bot — two responsibilities:
1. send()  : push messages to the user (trade alerts, summaries)
2. run_bot(): Claude-powered assistant + trading commands
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import config

logging.basicConfig(level=logging.WARNING)

# Per-chat conversation history for Claude (in-memory)
_history: dict[int, list] = {}

_SYSTEM = """You are Mohammed's personal AI assistant running inside his trading agent.
You have two roles:
1. General assistant — answer any question, help with any topic, have natural conversations.
2. Trading assistant — you have tools to check the portfolio, positions, BTC, trade history, and trigger a trading cycle.

When Mohammed asks about his portfolio, positions, or trades, use the available tools to get live data before answering.
For everything else, just respond naturally as a helpful assistant.
Keep replies concise and clear — this is a phone chat interface.
Today's date: """ + datetime.now(timezone.utc).strftime("%Y-%m-%d") + "."

_TOOLS = [
    {
        "name": "get_portfolio",
        "description": "Get current portfolio equity, cash, and buying power.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_positions",
        "description": "Get all open positions with P&L.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_btc",
        "description": "Get BTC/USD current price, RSI, and any open BTC position.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_trade_history",
        "description": "Get recent trade history from the database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of trades to return (default 10)"}
            },
            "required": [],
        },
    },
    {
        "name": "run_trading_cycle",
        "description": "Trigger a full trading cycle now (scans watchlist and places paper trades).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _exec_tool(name: str, inputs: dict) -> str:
    try:
        if name == "get_portfolio":
            from broker import alpaca
            p = alpaca.get_portfolio()
            return json.dumps(p)

        elif name == "get_positions":
            from broker import alpaca
            positions = alpaca.get_positions()
            if not positions:
                return "No open positions."
            return json.dumps(positions)

        elif name == "get_btc":
            from broker import alpaca
            from signals import technical
            bars = alpaca.get_crypto_bars("BTC/USD")
            tech = technical.compute(bars)
            positions = alpaca.get_positions()
            btc = next((p for p in positions if "BTC" in p["symbol"]), None)
            result = {"price": tech.get("price"), "rsi": tech.get("rsi"), "position": btc}
            return json.dumps(result)

        elif name == "get_trade_history":
            import database.db as db
            limit = inputs.get("limit", 10)
            trades = db.get_recent_trades(limit)
            return json.dumps(trades, default=str)

        elif name == "run_trading_cycle":
            import subprocess, sys
            subprocess.Popen([sys.executable, "main.py"])
            return "Trading cycle started. You'll receive alerts as trades execute."

        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        return f"Tool error: {e}"


async def _ask_claude(chat_id: int, user_text: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    history = _history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})

    # Limit history to last 20 messages to control token cost
    if len(history) > 20:
        history[:] = history[-20:]

    messages = list(history)

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_SYSTEM,
            tools=_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            # Add assistant's tool-use turn
            messages.append({"role": "assistant", "content": response.content})

            # Execute all tool calls and collect results
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _exec_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})

        else:
            # Final text response
            reply = ""
            for block in response.content:
                if hasattr(block, "text"):
                    reply += block.text
            reply = reply.strip()

            # Save assistant reply to history
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
        print("[Telegram] TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — bot not started.")
        return

    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

    CHAT = int(config.TELEGRAM_CHAT_ID)

    def _auth(update: Update) -> bool:
        return update.effective_chat.id == CHAT

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        _history.pop(update.effective_chat.id, None)  # fresh session
        await update.message.reply_text(
            "Hey Mohammed! I'm your AI assistant.\n\n"
            "You can talk to me about anything — ask questions, get advice, or manage your trading:\n\n"
            "/portfolio — equity & cash\n"
            "/positions — open positions\n"
            "/btc       — BTC price & P&L\n"
            "/run       — trigger a trading cycle\n"
            "/status    — agent health\n"
            "/clear     — reset conversation\n\n"
            "Or just message me anything!"
        )

    async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        _history.pop(update.effective_chat.id, None)
        await update.message.reply_text("Conversation cleared. Fresh start!")

    async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        try:
            from broker import alpaca
            p = alpaca.get_portfolio()
            await update.message.reply_text(
                f"Portfolio\n"
                f"Equity:       ${p['equity']:,.2f}\n"
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
                    f" | now ${p['current_price']:.2f} | {sign}{p['unrealized_plpc']:.2f}%"
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
            rsi   = tech.get("rsi", "N/A")
            msg   = f"BTC/USD\nPrice: ${price:,.2f}\nRSI:   {rsi}"
            if btc:
                sign = "+" if btc["unrealized_plpc"] >= 0 else ""
                msg += f"\nPosition P&L: {sign}{btc['unrealized_plpc']:.2f}%"
            else:
                msg += "\nNo BTC position held."
            await update.message.reply_text(msg)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        await update.message.reply_text("Starting trading cycle now...")
        try:
            import subprocess, sys
            subprocess.Popen([sys.executable, "main.py"])
            await update.message.reply_text("Cycle started. You'll get alerts as trades execute.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        await update.message.reply_text(
            f"Agent online\n"
            f"Time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"
            f"All systems running."
        )

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        user_text = update.message.text
        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        try:
            reply = await _ask_claude(update.effective_chat.id, user_text)
            # Telegram max message length is 4096 chars
            if len(reply) > 4096:
                for i in range(0, len(reply), 4096):
                    await update.message.reply_text(reply[i:i+4096])
            else:
                await update.message.reply_text(reply)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("btc",       cmd_btc))
    app.add_handler(CommandHandler("run",       cmd_run))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("[Telegram] Bot listening for commands...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
