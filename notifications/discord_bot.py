"""
Discord bot — multiple Claude agents, one per channel.
Each text channel is its own agent with its own conversation history.
Channel name determines the agent's persona:
  #trading-*  → Trading agent (portfolio tools + strict trading persona)
  #code-*     → Code-focused assistant
  #research-* → Research & analysis assistant
  anything else → General assistant (all tools available)

Trade alerts from the trading cycle are posted to DISCORD_ALERT_CHANNEL_ID.

Start with:  python main.py --discord
"""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config

# Per-channel conversation history
_history: dict[int, list] = {}

# Pending approval requests: approval_id -> {"event": Event, "approved": bool}
_pending: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Agent personas — keyed by channel name prefix
# ---------------------------------------------------------------------------

_PERSONAS: dict[str, str] = {
    "trading": (
        "You are a trading assistant for Mohammed's paper trading agent. "
        "You have deep knowledge of markets, technical analysis, and his Alpaca portfolio. "
        "Use trading tools to give live data. Be concise — this is a Discord channel. "
        f"Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC."
    ),
    "code": (
        "You are a senior software engineer assistant for Mohammed. "
        "Help with code, debugging, architecture, and shell commands. "
        "Use file and shell tools freely (with approval for destructive actions). "
        f"Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC."
    ),
    "research": (
        "You are a research analyst for Mohammed. "
        "Help with market research, analysis, summarizing information, and strategy. "
        "Be thorough and data-driven. "
        f"Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC."
    ),
    "default": (
        "You are Jarvis — Mohammed's personal AI assistant running on his Discord server. "
        "You can do anything: answer questions, run commands, manage files, help with trading. "
        "For risky or destructive actions, always ask for approval first via the approval tool. "
        "Be concise and helpful. "
        f"Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC."
    ),
}

_TOOLS = [
    # ── Trading ───────────────────────────────────────────────────────────────
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
        "description": "Get recent trade history.",
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
        "description": "Trigger a full trading cycle. Always request_approval first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    # ── Approval ──────────────────────────────────────────────────────────────
    {
        "name": "request_approval",
        "description": (
            "Send Mohammed an approval request with Approve/Deny buttons in Discord "
            "before doing something risky (shell commands that change state, writing files, "
            "running trades). Returns 'approved' or 'denied'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_description": {
                    "type": "string",
                    "description": "One-line description of the action needing approval."
                }
            },
            "required": ["action_description"],
        },
    },
    # ── System / Files ────────────────────────────────────────────────────────
    {
        "name": "run_shell_command",
        "description": "Run a shell command. request_approval first for state-changing commands.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command":     {"type": "string", "description": "Shell command to run."},
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
                "path": {"type": "string", "description": "File path to read."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. request_approval first.",
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
        "description": "List files and folders in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path."}
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Approval UI
# ---------------------------------------------------------------------------

class ApprovalView(discord.ui.View):
    def __init__(self, aid: str):
        super().__init__(timeout=120)
        self.aid = aid

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.aid in _pending:
            _pending[self.aid]["approved"] = True
            _pending[self.aid]["event"].set()
        await interaction.response.edit_message(
            content=interaction.message.content + "\n\n✅ **Approved**", view=None
        )

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.aid in _pending:
            _pending[self.aid]["approved"] = False
            _pending[self.aid]["event"].set()
        await interaction.response.edit_message(
            content=interaction.message.content + "\n\n❌ **Denied**", view=None
        )


async def _send_approval(channel: discord.TextChannel, description: str) -> bool:
    aid = uuid.uuid4().hex[:8]
    event = asyncio.Event()
    _pending[aid] = {"event": event, "approved": False}

    await channel.send(
        f"⚠️ **Approval needed**\n```\n{description}\n```",
        view=ApprovalView(aid),
    )

    try:
        await asyncio.wait_for(event.wait(), timeout=120.0)
        return _pending[aid]["approved"]
    except asyncio.TimeoutError:
        return False
    finally:
        _pending.pop(aid, None)


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def _exec_tool(name: str, inputs: dict, channel=None) -> str:
    try:
        if name == "get_portfolio":
            from broker import alpaca
            return json.dumps(alpaca.get_portfolio())

        elif name == "get_positions":
            from broker import alpaca
            pos = alpaca.get_positions()
            return json.dumps(pos) if pos else "No open positions."

        elif name == "get_btc":
            from broker import alpaca
            from signals import technical
            bars = alpaca.get_crypto_bars("BTC/USD")
            tech = technical.compute(bars)
            pos = alpaca.get_positions()
            btc = next((p for p in pos if "BTC" in p["symbol"]), None)
            return json.dumps({"price": tech.get("price"), "rsi": tech.get("rsi"), "position": btc})

        elif name == "get_trade_history":
            import database.db as db
            return json.dumps(db.get_recent_trades(inputs.get("limit", 10)), default=str)

        elif name == "run_trading_cycle":
            subprocess.Popen([sys.executable, "main.py"])
            return "Trading cycle started."

        elif name == "request_approval":
            if channel:
                approved = await _send_approval(channel, inputs["action_description"])
                return "approved" if approved else "denied"
            return "approved"

        elif name == "run_shell_command":
            cwd = inputs.get("working_dir") or os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..")
            )
            result = subprocess.run(
                inputs.get("command", ""), shell=True, capture_output=True,
                text=True, timeout=60, cwd=cwd,
            )
            out = (result.stdout + result.stderr).strip()
            return out[:3000] if out else "(no output)"

        elif name == "read_file":
            with open(inputs["path"], "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return content[:4000]

        elif name == "write_file":
            os.makedirs(os.path.dirname(os.path.abspath(inputs["path"])), exist_ok=True)
            with open(inputs["path"], "w", encoding="utf-8") as f:
                f.write(inputs["content"])
            return f"Written: {inputs['path']}"

        elif name == "list_directory":
            path = inputs.get("path") or os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..")
            )
            return "\n".join(sorted(os.listdir(path)))

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Tool error ({name}): {e}"


# ---------------------------------------------------------------------------
# Claude conversation
# ---------------------------------------------------------------------------

def _get_system(channel_name: str) -> str:
    name = (channel_name or "").lower()
    for key in ("trading", "code", "research"):
        if key in name:
            return _PERSONAS[key]
    return _PERSONAS["default"]


async def _ask_claude(channel_id: int, channel_name: str, user_text: str, channel=None) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    history = _history.setdefault(channel_id, [])
    history.append({"role": "user", "content": user_text})
    if len(history) > 20:
        history[:] = history[-20:]

    messages = list(history)
    system = _get_system(channel_name)

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system,
            tools=_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await _exec_tool(block.name, block.input, channel=channel)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})
        else:
            reply = "".join(getattr(b, "text", "") for b in response.content).strip()
            history.append({"role": "assistant", "content": reply})
            return reply or "..."


# ---------------------------------------------------------------------------
# Send trade alert to Discord (replaces Telegram send)
# ---------------------------------------------------------------------------

_bot_instance: "JarvisBot | None" = None


def send(text: str):
    """Push a trade alert to the Discord alert channel."""
    if not config.DISCORD_TOKEN or not config.DISCORD_ALERT_CHANNEL_ID:
        print(f"  [Discord] Not configured — alert not sent:\n{text[:80]}")
        return

    async def _send():
        if _bot_instance and not _bot_instance.is_closed():
            ch = _bot_instance.get_channel(config.DISCORD_ALERT_CHANNEL_ID)
            if ch:
                # Strip HTML tags from Telegram-style messages
                import re
                clean = re.sub(r"<[^>]+>", "", text)
                await ch.send(f"```\n{clean}\n```")
                return
        print(f"  [Discord] Bot not ready — alert not sent:\n{text[:80]}")

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_send())
        else:
            loop.run_until_complete(_send())
    except Exception as e:
        print(f"  [Discord] Send failed: {e}")


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class JarvisBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("[Discord] Slash commands synced.")

    async def on_ready(self):
        global _bot_instance
        _bot_instance = self
        print(f"[Discord] Jarvis online as {self.user} ({self.user.id})")
        print(f"[Discord] Serving {len(self.guilds)} server(s)")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Respond if: mentioned, DM, or bot's name appears in message
        is_dm = isinstance(message.channel, discord.DMChannel)
        mentioned = self.user in message.mentions
        named = self.user.name.lower() in message.content.lower() if self.user else False

        if not (is_dm or mentioned or named):
            return

        # Strip the mention from the content
        content = message.content
        if self.user:
            content = content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()

        if not content:
            return

        channel_name = getattr(message.channel, "name", "dm")
        async with message.channel.typing():
            try:
                reply = await _ask_claude(
                    message.channel.id, channel_name, content, channel=message.channel
                )
                # Discord max message length is 2000 chars
                for i in range(0, len(reply), 1900):
                    await message.reply(reply[i:i + 1900])
            except Exception as e:
                await message.reply(f"Error: {e}")

        await self.process_commands(message)


def _setup_slash_commands(bot: JarvisBot):
    tree = bot.tree

    @tree.command(name="clear", description="Clear this channel's conversation history")
    async def slash_clear(interaction: discord.Interaction):
        _history.pop(interaction.channel_id, None)
        await interaction.response.send_message("Conversation cleared.", ephemeral=True)

    @tree.command(name="status", description="Check Jarvis status")
    async def slash_status(interaction: discord.Interaction):
        await interaction.response.send_message(
            f"**Jarvis online** | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"Channels active: {len(_history)} | Pending approvals: {len(_pending)}",
            ephemeral=True,
        )

    @tree.command(name="help", description="Show what Jarvis can do")
    async def slash_help(interaction: discord.Interaction):
        await interaction.response.send_message(
            "**Jarvis — Claude AI Agent**\n\n"
            "Just mention me or use my name in any message.\n\n"
            "**What I can do:**\n"
            "• Answer any question\n"
            "• Run shell commands on the trading PC\n"
            "• Read and write files\n"
            "• Check portfolio, positions, BTC\n"
            "• Trigger trading cycles\n"
            "• Approve/deny actions with buttons\n\n"
            "**Channel personas:**\n"
            "`#trading-*` → Trading expert\n"
            "`#code-*` → Senior engineer\n"
            "`#research-*` → Research analyst\n"
            "Anything else → General assistant\n\n"
            "**Slash commands:** `/clear` `/status` `/help`",
            ephemeral=True,
        )


def run_bot():
    if not config.DISCORD_TOKEN:
        print("[Discord] DISCORD_TOKEN not set — bot not started.")
        return

    bot = JarvisBot()
    _setup_slash_commands(bot)
    print("[Discord] Starting Jarvis...")
    bot.run(config.DISCORD_TOKEN)
