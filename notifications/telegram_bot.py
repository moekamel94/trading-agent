"""
Telegram bot — two responsibilities:
1. send()  : push messages to the user (trade alerts, summaries)
2. run_bot(): listen for commands from the user's phone
"""
import asyncio
import logging
import config

logging.basicConfig(level=logging.WARNING)


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
# Interactive bot — run as a long-lived process on the server
# ---------------------------------------------------------------------------

def run_bot():
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[Telegram] TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — bot not started.")
        return

    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    CHAT = int(config.TELEGRAM_CHAT_ID)

    def _auth(update: Update) -> bool:
        return update.effective_chat.id == CHAT

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        await update.message.reply_text(
            "Trading Agent online.\n\n"
            "Commands:\n"
            "/portfolio — current equity & cash\n"
            "/positions — open positions\n"
            "/btc       — BTC price & P&L\n"
            "/run       — trigger a full trading cycle now\n"
            "/status    — agent health check\n"
        )

    async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        try:
            from broker import alpaca
            p = alpaca.get_portfolio()
            await update.message.reply_text(
                f"Portfolio\n"
                f"Equity:  ${p['equity']:,.2f}\n"
                f"Cash:    ${p['cash']:,.2f}\n"
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
                    f"  {p['symbol']}: {p['qty']:.4f} shares @ ${p['avg_entry']:.2f}"
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

            price = tech.get("price", "N/A")
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
            await update.message.reply_text("Cycle started. Results will follow shortly.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _auth(update): return
        from datetime import datetime, timezone
        await update.message.reply_text(
            f"Agent online\n"
            f"Time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"
            f"All systems running."
        )

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("btc",       cmd_btc))
    app.add_handler(CommandHandler("run",       cmd_run))
    app.add_handler(CommandHandler("status",    cmd_status))

    print("[Telegram] Bot listening for commands...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
