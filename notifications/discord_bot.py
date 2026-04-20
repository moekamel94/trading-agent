"""
Kimmy — Mohammed's personal AI assistant on Discord.
Powered by Claude. Capabilities mirror Claude Code:
  - Answer any question
  - Web search
  - Fetch URLs / read web pages
  - Run shell commands (with approval)
  - Read / write / list files (write needs approval)
  - Execute Python code
  - Manage the trading agent
  - Persistent conversation memory per channel (SQLite)

Channel personas (based on channel name):
  #trading-*  → Trading/markets expert
  #code-*     → Senior software engineer
  #research-* → Research analyst
  anything    → Full general assistant

Trade alerts from the trading cycle post to DISCORD_ALERT_CHANNEL_ID.
Start: python main.py --discord
"""
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config

# ---------------------------------------------------------------------------
# Persistent memory — stores conversation history in SQLite
# ---------------------------------------------------------------------------

_MEM_DB = os.path.join(os.path.dirname(__file__), "..", "kimmy_memory.db")

def _mem_init():
    with sqlite3.connect(_MEM_DB) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                channel   INTEGER NOT NULL,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                ts        TEXT NOT NULL
            )
        """)

def _mem_load(channel_id: int, limit: int = 20) -> list:
    with sqlite3.connect(_MEM_DB) as c:
        rows = c.execute(
            "SELECT role, content FROM history WHERE channel=? ORDER BY id DESC LIMIT ?",
            (channel_id, limit)
        ).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

def _mem_save(channel_id: int, role: str, content: str):
    with sqlite3.connect(_MEM_DB) as c:
        c.execute(
            "INSERT INTO history (channel, role, content, ts) VALUES (?,?,?,?)",
            (channel_id, role, content, datetime.now(timezone.utc).isoformat())
        )

def _mem_clear(channel_id: int):
    with sqlite3.connect(_MEM_DB) as c:
        c.execute("DELETE FROM history WHERE channel=?", (channel_id,))

_mem_init()

# ---------------------------------------------------------------------------
# Pending approvals
# ---------------------------------------------------------------------------

_pending: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Concurrency guards
# ---------------------------------------------------------------------------

# Per-channel lock: only ONE message processed at a time per channel.
# This prevents duplicate approval requests and duplicate code execution
# when messages arrive quickly or Discord re-delivers a message.
_channel_locks: dict[int, asyncio.Lock] = {}

# Deduplication set: tracks message IDs we've already handled.
# Discord occasionally fires on_message twice for the same message.
_processed_msg_ids: set[int] = set()

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_BASE = (
    "You are Kimmy — Mohammed's personal AI assistant, exactly like Claude Code but running on his Discord server.\n"
    "Mohammed travels and relies on you as his primary AI assistant. Treat every request seriously and completely.\n\n"
    "You can do EVERYTHING:\n"
    "- Answer any question on any topic (science, math, finance, history, coding, etc.)\n"
    "- Search the web for up-to-date information\n"
    "- Read web pages and URLs\n"
    "- Write, debug, and explain code in any language\n"
    "- Run shell commands on the cloud server\n"
    "- Read and write files on the server\n"
    "- Execute Python code snippets\n"
    "- Manage his trading agent (portfolio, positions, trades, BTC)\n"
    "- Help with any task, plan, or decision\n\n"
    "Rules:\n"
    "- For shell commands or file writes that change state: use request_approval first\n"
    "- For read-only operations (reading files, listing dirs, running safe scripts): no approval needed\n"
    "- Always use web_search when you need current information you might not have\n"
    "- Be thorough — if a task needs multiple steps, do all of them\n"
    "- Keep responses clear and concise (this is Discord, not a document editor)\n"
    "- NEVER paste code in your replies. Mohammed does not want to see code in Discord. "
    "Write and run code silently using tools (run_python, write_file, run_shell_command), "
    "then tell him the RESULT in plain English. If he asks what code you wrote, give a "
    "one-line summary — not the code itself.\n"
    "- IMPORTANT: When a task is fully complete, always end your final message with '✅ Done.' "
    "so Mohammed knows everything finished successfully. If there were errors you could not fix, "
    "end with '⛔ Stopped — [reason]. Please check and tell me how to proceed.' Never silently fail.\n"
    f"Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC."
)

_PERSONAS = {
    "trading": _BASE + "\n\nThis is the #trading channel — focus on markets, portfolio, and trading strategy.",
    "code":    _BASE + "\n\nThis is the #code channel — focus on software engineering, debugging, and architecture.",
    "research":_BASE + "\n\nThis is the #research channel — focus on research, analysis, and deep dives.",
    "default": _BASE,
}

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_TOOLS = [
    # ── Knowledge & Web ───────────────────────────────────────────────────────
    {
        "name": "web_search",
        "description": "Search the web for current information. Use this whenever you need up-to-date facts, prices, news, or anything that may have changed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string", "description": "Search query."},
                "max_results": {"type": "integer", "description": "Number of results (default 5)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Fetch and read the content of any URL (web page, API, docs, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch."}
            },
            "required": ["url"],
        },
    },
    # ── Code execution ────────────────────────────────────────────────────────
    {
        "name": "run_python",
        "description": "Execute a Python code snippet on the server and return the output. Great for calculations, data processing, quick scripts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to run."}
            },
            "required": ["code"],
        },
    },
    # ── Shell & Files ─────────────────────────────────────────────────────────
    {
        "name": "request_approval",
        "description": "Ask Mohammed to approve an action before doing it. Use before any shell command that changes state or any file write.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_description": {"type": "string", "description": "Clear description of what you are about to do."}
            },
            "required": ["action_description"],
        },
    },
    {
        "name": "run_shell_command",
        "description": "Run a shell command on the server and return output. Use request_approval first for state-changing commands.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command":     {"type": "string", "description": "Shell command to run."},
                "working_dir": {"type": "string", "description": "Working directory (optional, defaults to /opt/trading-agent)."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file on the server.",
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
        "description": "Write content to a file on the server. Use request_approval first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "File path."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and folders in a directory on the server.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: /opt/trading-agent)."}
            },
            "required": [],
        },
    },
    # ── Environment / secrets ─────────────────────────────────────────────────
    {
        "name": "set_env_var",
        "description": (
            "Add or update an environment variable (API key, token, etc.) in the server's .env file. "
            "Use this when Mohammed shares a new API key or credential. "
            "Always request_approval first since this changes server config."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key":   {"type": "string", "description": "Variable name e.g. OPENAI_API_KEY"},
                "value": {"type": "string", "description": "The value/key to store"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "list_env_vars",
        "description": "List all environment variable names in the .env file (values are hidden for security).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    # ── Discord management ────────────────────────────────────────────────────
    {
        "name": "create_discord_channel",
        "description": (
            "Create a new text channel in the Discord server. "
            "The channel name determines its agent persona automatically: "
            "trading-* = trading expert, code-* = engineer, research-* = analyst, other = general."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_name": {
                    "type": "string",
                    "description": "Channel name (lowercase, hyphens instead of spaces, e.g. 'crypto-research')."
                },
                "topic": {
                    "type": "string",
                    "description": "Optional channel topic/description shown at the top."
                },
            },
            "required": ["channel_name"],
        },
    },
    {
        "name": "list_discord_channels",
        "description": "List all text channels in the Discord server.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "delete_discord_channel",
        "description": "Delete a Discord channel by name. Always request_approval first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_name": {"type": "string", "description": "Exact channel name to delete."}
            },
            "required": ["channel_name"],
        },
    },
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
        "description": "Get BTC/USD current price, RSI, and open BTC position.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_trade_history",
        "description": "Get recent trade history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of trades (default 10)."}
            },
            "required": [],
        },
    },
    {
        "name": "run_trading_cycle",
        "description": "Trigger a full trading cycle. Use request_approval first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
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
            content=interaction.message.content + "\n\n✅ **Approved — executing now...**", view=None
        )

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.aid in _pending:
            _pending[self.aid]["approved"] = False
            _pending[self.aid]["event"].set()
        await interaction.response.edit_message(
            content=interaction.message.content + "\n\n❌ **Denied — stopping.**", view=None
        )


async def _send_approval(channel, description: str) -> bool:
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
        # Timed out waiting — notify and stop
        await channel.send(
            "⏰ **Approval timed out** — I've stopped. Send your request again when ready."
        )
        return False
    finally:
        _pending.pop(aid, None)

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

_DEFAULT_DIR = "/opt/trading-agent"

async def _exec_tool(name: str, inputs: dict, channel=None) -> str:
    try:
        # ── Web ───────────────────────────────────────────────────────────────
        if name == "web_search":
            from duckduckgo_search import DDGS
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(inputs["query"], max_results=inputs.get("max_results", 5)):
                    results.append(f"**{r['title']}**\n{r['href']}\n{r['body']}\n")
            return "\n---\n".join(results) if results else "No results found."

        elif name == "fetch_url":
            import urllib.request
            req = urllib.request.Request(inputs["url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            # Strip HTML tags for readability
            clean = re.sub(r"<[^>]+>", " ", raw)
            clean = re.sub(r"\s+", " ", clean).strip()
            return clean[:4000]

        # ── Code execution ────────────────────────────────────────────────────
        elif name == "run_python":
            code = inputs["code"]
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=30,
                cwd=_DEFAULT_DIR,
            )
            out = (result.stdout + result.stderr).strip()
            return out[:3000] if out else "(no output)"

        # ── Approval ──────────────────────────────────────────────────────────
        elif name == "request_approval":
            if channel:
                approved = await _send_approval(channel, inputs["action_description"])
                return "approved" if approved else "denied"
            return "approved"

        # ── Shell & Files ─────────────────────────────────────────────────────
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
                content = f.read()
            return content[:4000]

        elif name == "write_file":
            os.makedirs(os.path.dirname(os.path.abspath(inputs["path"])), exist_ok=True)
            with open(inputs["path"], "w", encoding="utf-8") as f:
                f.write(inputs["content"])
            return f"Written: {inputs['path']}"

        elif name == "list_directory":
            path = inputs.get("path") or _DEFAULT_DIR
            items = sorted(os.listdir(path))
            return "\n".join(items)

        # ── Environment / secrets ─────────────────────────────────────────────
        elif name == "set_env_var":
            env_path = os.path.join(_DEFAULT_DIR, ".env")
            key   = inputs["key"].strip().upper()
            value = inputs["value"].strip()
            if os.path.exists(env_path):
                with open(env_path, "r") as f:
                    lines = f.readlines()
            else:
                lines = []
            # Replace existing or append
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
            # Restart self to pick up new env
            subprocess.Popen(["systemctl", "restart", "kimmy"])
            return f"Set {key} in .env and restarting to apply it."

        elif name == "list_env_vars":
            env_path = os.path.join(_DEFAULT_DIR, ".env")
            if not os.path.exists(env_path):
                return "No .env file found."
            with open(env_path, "r") as f:
                lines = f.readlines()
            keys = [l.split("=")[0] for l in lines if "=" in l and not l.startswith("#")]
            return "Variables set:\n" + "\n".join(f"• {k}" for k in keys)

        # ── Discord management ────────────────────────────────────────────────
        elif name == "create_discord_channel":
            if not _bot_instance:
                return "Bot not ready."
            guild = next((g for g in _bot_instance.guilds), None)
            if not guild:
                return "No Discord server found."
            ch_name = inputs["channel_name"].lower().replace(" ", "-")
            topic = inputs.get("topic", "")
            existing = discord.utils.get(guild.text_channels, name=ch_name)
            if existing:
                return f"Channel #{ch_name} already exists."
            new_ch = await guild.create_text_channel(ch_name, topic=topic)
            await new_ch.send(
                f"👋 **New channel created!**\n"
                f"I'm Kimmy — your AI assistant for this channel.\n"
                f"Just type anything to get started."
            )
            return f"Created #{ch_name} and sent a welcome message."

        elif name == "list_discord_channels":
            if not _bot_instance:
                return "Bot not ready."
            guild = next((g for g in _bot_instance.guilds), None)
            if not guild:
                return "No server found."
            channels = [f"#{c.name}" for c in guild.text_channels]
            return "\n".join(channels)

        elif name == "delete_discord_channel":
            if not _bot_instance:
                return "Bot not ready."
            guild = next((g for g in _bot_instance.guilds), None)
            if not guild:
                return "No server found."
            ch = discord.utils.get(guild.text_channels, name=inputs["channel_name"])
            if not ch:
                return f"Channel #{inputs['channel_name']} not found."
            await ch.delete(reason="Deleted by Kimmy on user request")
            return f"Deleted #{inputs['channel_name']}."

        # ── Trading ───────────────────────────────────────────────────────────
        elif name == "get_portfolio":
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
            subprocess.Popen([sys.executable, "main.py"], cwd=_DEFAULT_DIR)
            return "Trading cycle started."

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Error ({name}): {e}"

# ---------------------------------------------------------------------------
# Claude conversation
# ---------------------------------------------------------------------------

def _get_system(channel_name: str) -> str:
    name = (channel_name or "").lower()
    for key in ("trading", "code", "research"):
        if key in name:
            return _PERSONAS[key]
    return _PERSONAS["default"]


# Safety cap: if Claude loops more than this many tool rounds without finishing,
# stop and notify Mohammed rather than burning more API credits.
_MAX_TOOL_ROUNDS = 15


async def _ask_claude(channel_id: int, channel_name: str, user_text: str, channel=None) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    # Save user message and load history
    _mem_save(channel_id, "user", user_text)
    messages = _mem_load(channel_id, limit=20)
    system = _get_system(channel_name)

    rounds = 0
    while True:
        rounds += 1
        if rounds > _MAX_TOOL_ROUNDS:
            stop_msg = (
                "⛔ **Stopped — too many steps without finishing.**\n"
                "I've hit the safety limit to avoid wasting API credits. "
                "Please check what happened and tell me how to continue."
            )
            _mem_save(channel_id, "assistant", stop_msg)
            return stop_msg

        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
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

                    # If Mohammed denied an approval, stop immediately — don't retry or loop.
                    if block.name == "request_approval" and result == "denied":
                        stop_msg = "⛔ **Stopped** — you denied the action. Let me know if you want to try a different approach."
                        _mem_save(channel_id, "assistant", stop_msg)
                        return stop_msg

                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})

        else:
            reply = "".join(getattr(b, "text", "") for b in response.content).strip()
            _mem_save(channel_id, "assistant", reply)
            return reply or "..."

# ---------------------------------------------------------------------------
# Send trade alert to Discord
# ---------------------------------------------------------------------------

_bot_instance = None


def _send_webhook(text: str):
    """Send via Discord webhook — works without a running event loop."""
    import requests
    clean = re.sub(r"<[^>]+>", "", text)
    try:
        r = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json={"content": f"```\n{clean[:1900]}\n```"},
            timeout=10,
        )
        if r.status_code not in (200, 204):
            print(f"  [Discord] Webhook error {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"  [Discord] Webhook failed: {e}")


def send(text: str):
    # Webhook path: works from any thread, no event loop needed (used for standalone cycles)
    if config.DISCORD_WEBHOOK_URL:
        _send_webhook(text)
        return

    if not config.DISCORD_TOKEN or not config.DISCORD_ALERT_CHANNEL_ID:
        print(f"  [Discord] Not configured — alert dropped:\n{text[:80]}")
        return

    # Bot path: requires --discord mode with a running event loop
    async def _send():
        if _bot_instance and not _bot_instance.is_closed():
            ch = _bot_instance.get_channel(config.DISCORD_ALERT_CHANNEL_ID)
            if ch:
                clean = re.sub(r"<[^>]+>", "", text)
                await ch.send(f"```\n{clean[:1900]}\n```")
                return
        print(f"  [Discord] Bot not ready — alert dropped:\n{text[:80]}")

    try:
        loop = asyncio.get_running_loop()
        asyncio.ensure_future(_send())
    except RuntimeError:
        try:
            asyncio.run(_send())
        except Exception as e:
            print(f"  [Discord] Send failed: {e}")

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class KimmyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        try:
            await asyncio.wait_for(self.tree.sync(), timeout=15)
            print("[Discord] Slash commands synced.")
        except Exception as e:
            print(f"[Discord] Slash sync skipped: {e}")

    async def on_ready(self):
        global _bot_instance
        _bot_instance = self
        print(f"[Discord] Kimmy online as {self.user}")
        for g in self.guilds:
            print(f"[Discord] Server: {g.name}")
        print("[Discord] Ready.")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Dedup: Discord can fire on_message twice for the same message ID.
        # Discard if we've already started processing it.
        if message.id in _processed_msg_ids:
            return
        _processed_msg_ids.add(message.id)
        # Keep the set bounded — discard old IDs once it grows large
        if len(_processed_msg_ids) > 2000:
            for mid in list(_processed_msg_ids)[:1000]:
                _processed_msg_ids.discard(mid)

        content = message.content
        if self.user:
            content = content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()

        if not content:
            return

        # Per-channel lock: only one request processed at a time per channel.
        # If a previous request is still running, tell Mohammed and drop this one.
        if message.channel.id not in _channel_locks:
            _channel_locks[message.channel.id] = asyncio.Lock()
        lock = _channel_locks[message.channel.id]

        if lock.locked():
            await message.reply(
                "⏳ **Still working on your previous request.** "
                "Please wait for it to finish before sending a new one."
            )
            return

        channel_name = getattr(message.channel, "name", "dm")
        async with lock:
            async with message.channel.typing():
                try:
                    reply = await _ask_claude(
                        message.channel.id, channel_name, content, channel=message.channel
                    )
                    # Split into ≤1900-char chunks (leave room for code block markers)
                    for chunk in textwrap.wrap(reply, 1900, break_long_words=False, replace_whitespace=False):
                        await message.reply(chunk)
                except Exception as e:
                    await message.reply(
                        f"⛔ **I hit an error and stopped.**\n```\n{e}\n```\n"
                        f"Fix it here and tell me to continue when ready."
                    )

        await self.process_commands(message)


def _setup_slash_commands(bot: KimmyBot):
    tree = bot.tree

    @tree.command(name="clear", description="Clear this channel's conversation history")
    async def slash_clear(interaction: discord.Interaction):
        _mem_clear(interaction.channel_id)
        await interaction.response.send_message("Memory cleared for this channel.", ephemeral=True)

    @tree.command(name="status", description="Kimmy status")
    async def slash_status(interaction: discord.Interaction):
        await interaction.response.send_message(
            f"**Kimmy online** | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"Running on DigitalOcean cloud server.",
            ephemeral=True,
        )

    @tree.command(name="help", description="What Kimmy can do")
    async def slash_help(interaction: discord.Interaction):
        await interaction.response.send_message(
            "**Kimmy — Your Personal AI Assistant**\n\n"
            "Just type anything — no need to mention me.\n\n"
            "**I can:**\n"
            "• Answer any question on any topic\n"
            "• Search the web for current info\n"
            "• Read web pages and URLs\n"
            "• Write and run code (Python, bash, etc.)\n"
            "• Read and edit files on the server\n"
            "• Manage your trading agent\n"
            "• Remember our conversation history\n\n"
            "**Channel modes:**\n"
            "`#trading-*` → Markets & trading expert\n"
            "`#code-*` → Software engineering\n"
            "`#research-*` → Research & analysis\n"
            "Anything else → General assistant\n\n"
            "**Commands:** `/clear` `/status` `/help`",
            ephemeral=True,
        )


def run_bot():
    if not config.DISCORD_TOKEN:
        print("[Discord] DISCORD_TOKEN not set.")
        return

    bot = KimmyBot()
    _setup_slash_commands(bot)
    print("[Discord] Starting Kimmy...")
    bot.run(config.DISCORD_TOKEN)
