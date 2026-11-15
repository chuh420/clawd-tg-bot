import asyncio
import html
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    list_sessions,
)

CONFIG_FILE = Path.home() / ".config/clawd-tg-bot/env"
STATE_DIR = Path.home() / ".local/state/clawd-tg-bot"
STATE_FILE = STATE_DIR / "state.json"
CREDENTIALS = Path.home() / ".claude/.credentials.json"
INBOX = STATE_DIR / "inbox"
MODES = ["default", "acceptEdits", "plan", "bypassPermissions"]
MODE_LABELS = {
    "default": "default (asks for permissions)",
    "acceptEdits": "acceptEdits (auto-accepts file edits)",
    "plan": "plan (read-only planning)",
    "bypassPermissions": "bypassPermissions (no prompts)",
    "auto": "auto (classifier decides)",
    "dontAsk": "dontAsk (denies unapproved tools)",
}
MODELS = [
    ("haiku 5.5", "claude-haiku-5-5"),
    ("sonnet 5.5", "claude-sonnet-5-5"),
    ("opus 5.5", "claude-opus-5-5"),
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
APPROVAL_TIMEOUT = 600
EDIT_INTERVAL = 1.5
CHUNK = 3500
SYSTEM_APPEND = (
    "The user is talking to you through a Telegram bot, usually from a phone. "
    "Keep replies short and readable on a small screen. "
    "Telegram renders only basic markdown: bold, italic, inline code, code blocks, links. "
    "Avoid tables and deep nesting. "
    "To send a local file or image to the user, put [[send: /absolute/path]] on its own line in your reply; "
    "the bot uploads it to the chat (images up to 10 MB as photos, other files up to 50 MB as documents)."
)
SEND_RE = re.compile(r"^[ \t]*\[\[send:\s*(.+?)\s*\]\][ \t]*$", re.M)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
PHOTO_LIMIT = 10 * 1024 * 1024
DOCUMENT_LIMIT = 50 * 1024 * 1024

log = logging.getLogger("clawd-tg-bot")


def load_env() -> None:
    if not CONFIG_FILE.exists():
        return
    for line in CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "")) if x}
PROXY = os.environ.get("TELEGRAM_PROXY") or None
DEFAULT_CWD = os.path.expanduser(os.environ.get("DEFAULT_CWD", "~"))
CLAUDE_PATH = os.environ.get("CLAUDE_PATH") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {str(cid): s.persist() for cid, s in sessions.items()}
    STATE_FILE.write_text(json.dumps(data, indent=2))


def md_to_html(text: str) -> str:
    out = []
    parts = re.split(r"```([^\n`]*)\n(.*?)(?:```|$)", text, flags=re.S)
    for i in range(0, len(parts), 3):
        out.append(inline_md(parts[i]))
        if i + 2 < len(parts):
            lang, code = parts[i + 1].strip(), parts[i + 2].rstrip("\n")
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(code)}</code></pre>")
    return "".join(out)


def inline_md(text: str) -> str:
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", stash, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def split_md(text: str, size: int = CHUNK) -> list[str]:
    chunks, cur, fence = [], "", None
    for line in text.split("\n"):
        while len(line) > size:
            head, line = line[:size], line[size:]
            if cur:
                chunks.append(cur + ("\n```" if fence is not None else ""))
                cur = f"```{fence}\n" if fence is not None else ""
            chunks.append(cur + head + ("\n```" if fence is not None else ""))
            cur = f"```{fence}\n" if fence is not None else ""
        if len(cur) + len(line) + 1 > size and cur.strip():
            chunks.append(cur + ("```" if fence is not None else ""))
            cur = f"```{fence}\n" if fence is not None else ""
        cur += line + "\n"
        if line.lstrip().startswith("```"):
            fence = None if fence is not None else line.lstrip()[3:].strip()
    if cur.strip():
        chunks.append(cur)
    return [c.rstrip("\n") for c in chunks if c.strip()]


async def send_md(chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> Message | None:
    msg = None
    pieces = split_md(text) or ["(empty)"]
    for n, piece in enumerate(pieces):
        markup = reply_markup if n == len(pieces) - 1 else None
        try:
            msg = await bot.send_message(chat_id, md_to_html(piece), parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramBadRequest:
            msg = await bot.send_message(chat_id, piece, reply_markup=markup)
    return msg


async def send_file(chat_id: int, raw: str) -> None:
    path = Path(raw).expanduser()
    if not path.is_file():
        await bot.send_message(chat_id, f"⚠️ file not found: {raw}")
        return
    size = path.stat().st_size
    try:
        if path.suffix.lower() in IMAGE_SUFFIXES and size <= PHOTO_LIMIT:
            await bot.send_photo(chat_id, FSInputFile(path))
        elif size <= DOCUMENT_LIMIT:
            await bot.send_document(chat_id, FSInputFile(path))
        else:
            await bot.send_message(chat_id, f"⚠️ file too large: {path.name} ({size // (1024 * 1024)} MB)")
    except Exception as e:
        log.exception("send_file failed")
        await bot.send_message(chat_id, f"⚠️ could not send {path.name}: {short(e, 200)}")


async def send_reply(chat_id: int, text: str) -> None:
    files = [m.group(1) for m in SEND_RE.finditer(text)]
    text = SEND_RE.sub("", text).strip()
    if text:
        await send_md(chat_id, text)
    for raw in files:
        await send_file(chat_id, raw)


def short(text: Any, n: int = 80) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_line(name: str, inp: dict[str, Any]) -> str:
    key = {
        "Bash": "command",
        "Read": "file_path",
        "Write": "file_path",
        "Edit": "file_path",
        "NotebookEdit": "notebook_path",
        "Glob": "pattern",
        "Grep": "pattern",
        "WebFetch": "url",
        "WebSearch": "query",
        "Agent": "description",
        "Task": "description",
        "Skill": "skill",
    }.get(name)
    arg = inp.get(key) if key else None
    if arg is None:
        arg = next((v for v in inp.values() if isinstance(v, str)), "")
    home = str(Path.home())
    arg = str(arg).replace(home, "~")
    return f"{name}: {short(arg)}" if arg else name


def tool_details(name: str, inp: dict[str, Any]) -> str:
    if name == "Bash":
        body = f"```bash\n{inp.get('command', '')[:1500]}\n```"
        if inp.get("description"):
            body = f"{inp['description']}\n{body}"
        return body
    if name == "Edit":
        old = inp.get("old_string", "")[:700]
        new = inp.get("new_string", "")[:700]
        diff = "\n".join([f"- {l}" for l in old.splitlines()] + [f"+ {l}" for l in new.splitlines()])
        return f"`{inp.get('file_path', '')}`\n```diff\n{diff}\n```"
    if name == "Write":
        return f"`{inp.get('file_path', '')}`\n```\n{inp.get('content', '')[:1200]}\n```"
    if name == "ExitPlanMode":
        return inp.get("plan", "")[:3000]
    return f"```json\n{json.dumps(inp, ensure_ascii=False, indent=1)[:1500]}\n```"


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


@dataclass
class Pending:
    future: asyncio.Future
    message: Message | None = None
    question: dict[str, Any] | None = None
    selected: set[str] = field(default_factory=set)


def model_label(value: str) -> str:
    base = value.split("[")[0].strip().lower()
    for label, mid in MODELS:
        family = label.split()[0]
        if base == mid or base == family or f"-{family}-" in base:
            return label
    return value


def model_id(value: str) -> str:
    label = model_label(value)
    for l, mid in MODELS:
        if l == label:
            return mid
    return value


def settings_model(cwd: str) -> str | None:
    paths = [Path(cwd) / ".claude/settings.local.json", Path(cwd) / ".claude/settings.json", Path.home() / ".claude/settings.json"]
    for path in paths:
        try:
            value = json.loads(path.read_text()).get("model")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, str) and value:
            return value
    return None


def effective_model(s: "ChatSession") -> str | None:
    return s.model or settings_model(s.cwd) or s.active_model


class ChatSession:
    def __init__(self, chat_id: int, saved: dict[str, Any]):
        self.chat_id = chat_id
        self.cwd = saved.get("cwd") or DEFAULT_CWD
        self.session_id: str | None = saved.get("session_id")
        self.mode = saved.get("mode") or "default"
        self.model: str | None = saved.get("model")
        self.effort: str | None = saved.get("effort")
        self.active_model: str | None = None
        self.active_mode: str | None = None
        self.restart_pending = False
        self.client: ClaudeSDKClient | None = None
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.busy = False
        self.always: set[str] = set()
        self.pending: dict[str, Pending] = {}
        self.progress: Message | None = None
        self.progress_lines: list[str] = []
        self.last_edit = 0.0

    def persist(self) -> dict[str, Any]:
        return {"cwd": self.cwd, "session_id": self.session_id, "mode": self.mode, "model": self.model, "effort": self.effort}

    def options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=self.cwd,
            resume=self.session_id,
            permission_mode=self.mode,
            model=self.model,
            effort=self.effort,
            can_use_tool=self.can_use_tool,
            setting_sources=["user", "project", "local"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM_APPEND},
            cli_path=CLAUDE_PATH,
            stderr=lambda line: log.debug("claude: %s", line),
        )

    async def ensure_client(self) -> ClaudeSDKClient:
        if self.client is None:
            client = ClaudeSDKClient(self.options())
            await client.connect()
            self.client = client
        return self.client

    async def close(self) -> None:
        self.deny_all("session closed")
        if self.client is not None:
            client, self.client = self.client, None
            try:
                await client.disconnect()
            except Exception:
                log.exception("disconnect failed")

    def submit(self, prompt: str) -> bool:
        queued = self.busy or not self.queue.empty()
        self.queue.put_nowait(prompt)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.run())
        return queued

    async def run(self) -> None:
        while not self.queue.empty():
            prompt = await self.queue.get()
            self.busy = True
            try:
                await self.turn(prompt)
            except Exception as e:
                log.exception("turn failed")
                await self.flush_progress(final=True)
                await bot.send_message(self.chat_id, f"⚠️ error: {short(e, 500)}")
                await self.close()
            finally:
                self.busy = False
            if self.restart_pending:
                self.restart_pending = False
                await self.close()

    async def turn(self, prompt: str) -> None:
        client = await self.ensure_client()
        self.progress, self.progress_lines = None, []
        await bot.send_chat_action(self.chat_id, "typing")
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                sid = msg.data.get("session_id")
                if sid:
                    self.session_id = sid
                    save_state()
                self.active_model = msg.data.get("model") or self.active_model
                self.active_mode = msg.data.get("permissionMode") or self.active_mode
            elif isinstance(msg, AssistantMessage):
                sub = msg.parent_tool_use_id is not None
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        await self.add_progress(("  ↳ " if sub else "") + tool_line(block.name, block.input))
                    elif isinstance(block, TextBlock) and not sub and block.text.strip():
                        await self.flush_progress(final=True)
                        await send_reply(self.chat_id, block.text)
            elif isinstance(msg, ResultMessage):
                await self.flush_progress(final=True)
                self.session_id = msg.session_id or self.session_id
                save_state()
                parts = [f"{msg.duration_ms / 1000:.0f}s", f"{msg.num_turns} turns"]
                if msg.total_cost_usd:
                    parts.append(f"~${msg.total_cost_usd:.2f}")
                status = "⚠️ " + msg.subtype if msg.is_error else "✓"
                await bot.send_message(self.chat_id, f"{status} " + " · ".join(parts), disable_notification=False)

    async def add_progress(self, line: str) -> None:
        self.progress_lines.append(line)
        await self.flush_progress()

    def progress_text(self, final: bool) -> str:
        lines = self.progress_lines[-25:]
        hidden = len(self.progress_lines) - len(lines)
        head = "🔧" if final else "⏳ working"
        if hidden:
            lines = [f"… {hidden} more"] + lines
        return head + "\n" + "\n".join(lines)

    async def flush_progress(self, final: bool = False) -> None:
        if not self.progress_lines:
            return
        now = time.monotonic()
        if not final and self.progress is not None and now - self.last_edit < EDIT_INTERVAL:
            return
        text = self.progress_text(final)
        try:
            if self.progress is None:
                self.progress = await bot.send_message(self.chat_id, text, disable_notification=True)
            else:
                await self.progress.edit_text(text)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.warning("progress update failed: %s", e)
        self.last_edit = now
        if final:
            self.progress, self.progress_lines = None, []

    async def can_use_tool(self, name: str, inp: dict[str, Any], ctx: ToolPermissionContext) -> Any:
        if name == "AskUserQuestion":
            return await self.ask_questions(inp)
        if name in self.always:
            return PermissionResultAllow()
        await self.flush_progress(final=True)
        pid = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        self.pending[pid] = Pending(fut)
        title = ctx.title or f"claude wants to use {name}"
        text = f"🔐 **{title}**\n{tool_details(name, inp)}"
        buttons = kb([[("✅ allow", f"p:{pid}:a"), ("❌ deny", f"p:{pid}:d"), ("♾ always", f"p:{pid}:A")]])
        self.pending[pid].message = await send_md(self.chat_id, text, buttons)
        try:
            choice = await asyncio.wait_for(fut, APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            choice = "timeout"
        finally:
            self.pending.pop(pid, None)
        if choice == "a":
            return PermissionResultAllow()
        if choice == "A":
            if not ctx.suggestions:
                self.always.add(name)
                return PermissionResultAllow()
            updates = [replace(u, destination="session") for u in ctx.suggestions]
            return PermissionResultAllow(updated_permissions=updates)
        reason = "approval timed out" if choice == "timeout" else "the user denied this from Telegram"
        if choice not in ("d", "timeout"):
            reason = str(choice)
        return PermissionResultDeny(message=reason)

    async def ask_questions(self, inp: dict[str, Any]) -> Any:
        await self.flush_progress(final=True)
        answers: dict[str, str] = {}
        for q in inp.get("questions", []):
            pid = uuid.uuid4().hex[:10]
            fut = asyncio.get_running_loop().create_future()
            pending = Pending(fut, question=q)
            self.pending[pid] = pending
            rows = [[(o["label"], f"q:{pid}:{i}")] for i, o in enumerate(q.get("options", []))]
            if q.get("multiSelect"):
                rows.append([("☑️ done", f"q:{pid}:done")])
            desc = "\n".join(f"• **{o['label']}** — {o.get('description', '')}" for o in q.get("options", []))
            text = f"❓ **{q.get('question', '')}**\n{desc}\n\n_or reply with your own answer_"
            pending.message = await send_md(self.chat_id, text, kb(rows))
            try:
                answers[q.get("question", "")] = await asyncio.wait_for(fut, APPROVAL_TIMEOUT)
            except asyncio.TimeoutError:
                return PermissionResultDeny(message="the user did not answer in time")
            finally:
                self.pending.pop(pid, None)
        return PermissionResultAllow(updated_input={**inp, "answers": answers})

    def text_answer(self, text: str) -> bool:
        for p in self.pending.values():
            if p.question is not None and not p.future.done():
                p.future.set_result(text)
                return True
        return False

    def deny_all(self, reason: str) -> None:
        for p in list(self.pending.values()):
            if not p.future.done():
                p.future.set_result(reason)
        self.pending.clear()

    async def stop(self) -> None:
        self.deny_all("stopped by the user")
        while not self.queue.empty():
            self.queue.get_nowait()
        if self.client is not None and self.busy:
            await self.client.interrupt()


bot: Bot
dp = Dispatcher()
sessions: dict[int, ChatSession] = {}
saved_state = load_state()


def session_for(chat_id: int) -> ChatSession:
    if chat_id not in sessions:
        sessions[chat_id] = ChatSession(chat_id, saved_state.get(str(chat_id), {}))
    return sessions[chat_id]


def allowed(event: Message | CallbackQuery) -> bool:
    user = event.from_user
    return user is not None and user.id in ALLOWED


async def reset(s: ChatSession, session_id: str | None = None) -> None:
    await s.close()
    s.session_id = session_id
    s.always.clear()
    save_state()


def busy_guard(s: ChatSession) -> str | None:
    return "busy — send /stop first" if s.busy else None


@dp.message(Command("start", "help"), allowed)
async def cmd_help(m: Message) -> None:
    s = session_for(m.chat.id)
    await send_md(
        m.chat.id,
        "**clawd-tg-bot** — Claude Code remote\n"
        "Send any text to talk to Claude. Photos and files are passed as paths.\n\n"
        "/new — new session\n/resume — pick a recent session\n/cd `path` — change folder\n"
        "/mode — permission mode\n/model — model\n/effort — reasoning effort\n/stop — interrupt\n/status — current state\n/off — shut down the bot\n\n"
        f"cwd: `{s.cwd}`",
    )


@dp.message(Command("status"), allowed)
async def cmd_status(m: Message) -> None:
    s = session_for(m.chat.id)
    context, limits = await asyncio.gather(context_line(s), limits_lines())
    mode = s.active_mode or s.mode or "unknown"
    model = effective_model(s)
    await send_md(
        m.chat.id,
        f"cwd: `{s.cwd}`\nsession: `{s.session_id or 'new'}`\nmode: `{MODE_LABELS.get(mode, mode)}`\n"
        f"model: `{model_label(model) if model else 'unknown'}`\neffort: `{s.effort or 'default'}`\nstate: {'busy' if s.busy else 'idle'}"
        + (f", {s.queue.qsize()} queued" if s.queue.qsize() else "")
        + f"\n\n{context}\n{limits}",
    )


def tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n < 1_000_000 else f"{n / 1_000_000:.2f}M"


async def context_line(s: ChatSession) -> str:
    if s.client is None and not s.session_id:
        return "context: new session, nothing used yet"
    try:
        client = await asyncio.wait_for(s.ensure_client(), 30)
        u = await asyncio.wait_for(client.get_context_usage(), 15)
    except Exception as e:
        log.warning("context usage failed: %s", e)
        return "context: unavailable"
    return f"context: **{tokens(u['totalTokens'])}** / {tokens(u['maxTokens'])} ({u['percentage']:.0f}%)"


def fetch_usage() -> dict[str, Any]:
    creds = json.loads(CREDENTIALS.read_text())["claudeAiOauth"]
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {creds['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "clawd-tg-bot",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def until(iso: str | None) -> str:
    if not iso:
        return ""
    at = datetime.fromisoformat(iso).astimezone()
    left = int((at - datetime.now().astimezone()).total_seconds())
    if left <= 0:
        return " · resets now"
    h, mnt = divmod(left // 60, 60)
    if h >= 24:
        return f" · resets {at:%a %H:%M}"
    return f" · resets in {h}h {mnt:02d}m" if h else f" · resets in {mnt}m"


def bar(pct: float, width: int = 10) -> str:
    full = min(width, round(pct / 100 * width))
    return "▓" * full + "░" * (width - full)


async def limits_lines() -> str:
    try:
        data = await asyncio.to_thread(fetch_usage)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "limits: login token expired, send any message to refresh it"
        return f"limits: unavailable (HTTP {e.code})"
    except Exception as e:
        log.warning("usage fetch failed: %s", e)
        return "limits: unavailable"
    names = [("five_hour", "5h"), ("seven_day", "week"), ("seven_day_opus", "week opus"), ("seven_day_sonnet", "week sonnet")]
    lines = []
    for key, label in names:
        w = data.get(key)
        if not w or w.get("utilization") is None:
            continue
        pct = float(w["utilization"])
        lines.append(f"{label}: `{bar(pct)}` **{pct:.0f}%**{until(w.get('resets_at'))}")
    return "\n".join(lines) or "limits: no data"


@dp.message(Command("new"), allowed)
async def cmd_new(m: Message) -> None:
    s = session_for(m.chat.id)
    if err := busy_guard(s):
        await m.answer(err)
        return
    await reset(s)
    await send_md(m.chat.id, f"🆕 new session in `{s.cwd}`")


@dp.message(Command("stop"), allowed)
async def cmd_stop(m: Message) -> None:
    s = session_for(m.chat.id)
    await s.stop()
    await m.answer("⏹ stopped" if s.busy else "nothing is running")


@dp.message(Command("cd"), allowed)
async def cmd_cd(m: Message, command: CommandObject) -> None:
    s = session_for(m.chat.id)
    if not command.args:
        await send_md(m.chat.id, f"cwd: `{s.cwd}`")
        return
    if err := busy_guard(s):
        await m.answer(err)
        return
    path = Path(os.path.expanduser(command.args.strip()))
    if not path.is_absolute():
        path = Path(s.cwd) / path
    path = path.resolve()
    if not path.is_dir():
        await send_md(m.chat.id, f"not a directory: `{path}`")
        return
    s.cwd = str(path)
    await reset(s)
    await send_md(m.chat.id, f"📁 `{s.cwd}` · new session")


@dp.message(Command("resume"), allowed)
async def cmd_resume(m: Message) -> None:
    s = session_for(m.chat.id)
    items = await asyncio.to_thread(list_sessions, s.cwd, 8)
    if not items:
        await send_md(m.chat.id, f"no sessions in `{s.cwd}`")
        return
    rows = []
    for it in items:
        when = datetime.fromtimestamp(it.last_modified / 1000).strftime("%b %d %H:%M")
        mark = "● " if it.session_id == s.session_id else ""
        rows.append([(short(f"{mark}{when} · {it.summary or it.first_prompt or it.session_id}", 60), f"r:{it.session_id}")])
    await send_md(m.chat.id, f"recent sessions in `{s.cwd}`:", kb(rows))


@dp.message(Command("mode"), allowed)
async def cmd_mode(m: Message) -> None:
    s = session_for(m.chat.id)
    rows = [[(("● " if x == s.mode else "") + x, f"m:{x}")] for x in MODES]
    await m.answer("permission mode:", reply_markup=kb(rows))


@dp.message(Command("model"), allowed)
async def cmd_model(m: Message) -> None:
    s = session_for(m.chat.id)
    current = effective_model(s)
    current_id = model_id(current) if current else None
    rows = [[(("● " if mid == current_id else "") + label, f"M:{mid}") for label, mid in MODELS]]
    await m.answer(f"model: {model_label(current) if current else 'unknown'}", reply_markup=kb(rows))


async def apply_effort(s: ChatSession, value: str | None) -> str:
    s.effort = value
    save_state()
    label = value or "default"
    if s.client is None:
        return f"effort: {label}"
    if s.busy:
        s.restart_pending = True
        return f"effort: {label} · applies after the current turn"
    await s.close()
    return f"effort: {label}"


@dp.message(Command("effort"), allowed)
async def cmd_effort(m: Message, command: CommandObject) -> None:
    s = session_for(m.chat.id)
    arg = (command.args or "").strip().lower()
    if arg:
        if arg not in EFFORTS + ["default"]:
            await m.answer("levels: " + ", ".join(EFFORTS + ["default"]))
            return
        await m.answer(await apply_effort(s, None if arg == "default" else arg))
        return
    current = s.effort or "default"
    rows = [[(("● " if x == current else "") + x, f"e:{x}") for x in EFFORTS[:3]],
            [(("● " if x == current else "") + x, f"e:{x}") for x in EFFORTS[3:] + ["default"]]]
    await m.answer(f"effort: {current}", reply_markup=kb(rows))


@dp.message(Command("off"), allowed)
async def cmd_off(m: Message) -> None:
    await m.answer("shutting down…")
    await dp.stop_polling()


@dp.callback_query(allowed)
async def on_callback(c: CallbackQuery) -> None:
    data = c.data or ""
    chat_id = c.message.chat.id if c.message else c.from_user.id
    s = session_for(chat_id)
    kind, _, rest = data.partition(":")
    if kind == "p":
        pid, _, choice = rest.partition(":")
        p = s.pending.get(pid)
        if p is None or p.future.done():
            await c.answer("expired")
            return
        p.future.set_result(choice)
        label = {"a": "✅ allowed", "d": "❌ denied", "A": "♾ always allowed"}[choice]
        await c.answer(label)
        if isinstance(c.message, Message):
            await c.message.edit_reply_markup(reply_markup=None)
            await c.message.reply(label, disable_notification=True)
    elif kind == "q":
        pid, _, choice = rest.partition(":")
        p = s.pending.get(pid)
        if p is None or p.future.done() or p.question is None:
            await c.answer("expired")
            return
        options = p.question.get("options", [])
        if choice == "done":
            answer = ", ".join(o["label"] for i, o in enumerate(options) if str(i) in p.selected)
        elif p.question.get("multiSelect"):
            p.selected ^= {choice}
            rows = [[(("☑ " if str(i) in p.selected else "") + o["label"], f"q:{pid}:{i}")] for i, o in enumerate(options)]
            rows.append([("☑️ done", f"q:{pid}:done")])
            await c.answer()
            if isinstance(c.message, Message):
                await c.message.edit_reply_markup(reply_markup=kb(rows))
            return
        else:
            answer = options[int(choice)]["label"]
        p.future.set_result(answer or "(nothing selected)")
        await c.answer(answer)
        if isinstance(c.message, Message):
            await c.message.edit_reply_markup(reply_markup=None)
            await c.message.reply(f"→ {answer}", disable_notification=True)
    elif kind == "r":
        if s.busy:
            await c.answer("busy — /stop first", show_alert=True)
            return
        await reset(s, rest)
        await c.answer("resumed")
        await send_md(chat_id, f"↩️ resumed `{rest}`")
    elif kind == "m":
        s.mode = rest
        if s.client is not None:
            await s.client.set_permission_mode(rest)
        save_state()
        await c.answer(rest)
        if isinstance(c.message, Message):
            await c.message.edit_text(f"permission mode: {rest}")
    elif kind == "M":
        s.model = rest
        if s.client is not None:
            await s.client.set_model(rest)
        save_state()
        await c.answer(model_label(rest))
        if isinstance(c.message, Message):
            await c.message.edit_text(f"model: {model_label(rest)}")
    elif kind == "e":
        text = await apply_effort(s, None if rest == "default" else rest)
        await c.answer(rest)
        if isinstance(c.message, Message):
            await c.message.edit_text(text)
    else:
        await c.answer()


async def download(m: Message) -> Path | None:
    obj = m.document or m.audio or m.video or m.voice or (m.photo[-1] if m.photo else None)
    if obj is None:
        return None
    INBOX.mkdir(parents=True, exist_ok=True)
    name = getattr(obj, "file_name", None) or f"{obj.file_unique_id}.{'jpg' if m.photo else 'bin'}"
    dest = INBOX / f"{datetime.now():%Y%m%d-%H%M%S}-{name}"
    await bot.download(obj, destination=dest)
    return dest


@dp.message(allowed)
async def on_message(m: Message) -> None:
    s = session_for(m.chat.id)
    text = m.text or m.caption or ""
    if m.text and s.text_answer(m.text):
        await m.reply("→ answered", disable_notification=True)
        return
    path = await download(m)
    if path is not None:
        text = f"[attached file: {path}]\n{text}".strip()
    if not text:
        return
    if s.submit(text):
        await m.reply("📥 queued", disable_notification=True)


def log_denied(event: Message | CallbackQuery, what: str) -> None:
    user = event.from_user
    if user is None:
        log.warning("denied %s from unknown sender", what)
        return
    log.warning(
        "denied %s from id=%s username=%s name=%r",
        what,
        user.id,
        user.username,
        user.full_name,
    )


@dp.message()
async def on_denied_message(m: Message) -> None:
    log_denied(m, f"message {short(m.text or m.caption or '', 80)!r}")
    await m.answer("not authorized")


@dp.callback_query()
async def on_denied_callback(c: CallbackQuery) -> None:
    log_denied(c, f"callback {c.data!r}")
    await c.answer("not authorized", show_alert=True)


async def on_startup() -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="new", description="new session"),
            BotCommand(command="resume", description="pick a recent session"),
            BotCommand(command="cd", description="change working folder"),
            BotCommand(command="mode", description="permission mode"),
            BotCommand(command="model", description="switch model"),
            BotCommand(command="effort", description="reasoning effort"),
            BotCommand(command="stop", description="interrupt claude"),
            BotCommand(command="status", description="current state"),
            BotCommand(command="off", description="shut down the bot"),
            BotCommand(command="help", description="help"),
        ]
    )
    for uid in ALLOWED:
        s = session_for(uid)
        try:
            await bot.send_message(uid, f"🟢 online · {s.cwd} · {s.mode}", disable_notification=True)
        except Exception as e:
            log.warning("cannot notify %s: %s", uid, e)


async def on_shutdown() -> None:
    for s in sessions.values():
        await s.close()
    for uid in ALLOWED:
        try:
            await bot.send_message(uid, "🔴 offline", disable_notification=True)
        except Exception:
            pass


async def main() -> None:
    global bot
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN or not ALLOWED or "replace-me" in TOKEN:
        log.error("not configured: run `clawd-tg-bot setup` or set TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS in %s", CONFIG_FILE)
        raise SystemExit(2)
    bot = Bot(TOKEN, session=AiohttpSession(proxy=PROXY) if PROXY else None)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, handle_signals=True)


if __name__ == "__main__":
    asyncio.run(main())
