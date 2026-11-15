import asyncio
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

CONFIG_FILE = Path.home() / ".config/clawd-tg-bot/env"
UNIT = "clawd-tg-bot"
WAIT_SECONDS = 180
KEYS = ["TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS", "DEFAULT_CWD", "CLAUDE_PATH", "TELEGRAM_PROXY", "LOG_LEVEL"]


def say(text: str = "") -> None:
    print(text, flush=True)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise SystemExit(1)
    return value or default


def confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = ask(f"{prompt} ({hint})").lower()
    if not value:
        return default
    return value in ("y", "yes")


def read_config() -> dict[str, str]:
    values: dict[str, str] = {}
    if not CONFIG_FILE.exists():
        return values
    for line in CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def write_config(values: dict[str, str]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={values[key]}" for key in KEYS if values.get(key)]
    lines += [f"{key}={value}" for key, value in values.items() if key not in KEYS and value]
    if not values.get("TELEGRAM_PROXY"):
        lines.append("# TELEGRAM_PROXY=socks5://127.0.0.1:1080")
    CONFIG_FILE.touch(mode=0o600, exist_ok=True)
    CONFIG_FILE.chmod(0o600)
    CONFIG_FILE.write_text("\n".join(lines) + "\n")


def find_claude(configured: str) -> str | None:
    for candidate in (configured, shutil.which("claude"), str(Path.home() / ".local/bin/claude")):
        if candidate and Path(candidate).expanduser().is_file() and os.access(Path(candidate).expanduser(), os.X_OK):
            return str(Path(candidate).expanduser())
    return None


def auth_status(claude: str) -> dict:
    try:
        out = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}


def setup_claude(values: dict[str, str]) -> None:
    say("step 1/4 · Claude Code")
    claude = find_claude(values.get("CLAUDE_PATH", ""))
    while not claude:
        say("  the claude command was not found.")
        say("  install Claude Code: https://docs.claude.com/en/docs/claude-code/setup")
        path = ask("  path to the claude binary, or Enter to check again")
        claude = find_claude(path)
    try:
        version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        version = "unknown version"
    say(f"  found {claude} ({version})")
    if claude != shutil.which("claude"):
        values["CLAUDE_PATH"] = claude
    else:
        values.pop("CLAUDE_PATH", None)
    status = auth_status(claude)
    while not status.get("loggedIn"):
        say("  Claude Code is not logged in.")
        if not confirm("  run `claude auth login` now?"):
            say("  the bot will not work until you log in. continuing.")
            break
        subprocess.run([claude, "auth", "login"])
        status = auth_status(claude)
    if status.get("loggedIn"):
        plan = status.get("subscriptionType") or status.get("authMethod") or "unknown"
        say(f"  logged in · {plan}")
    say()


def make_bot(token: str, proxy: str) -> Bot:
    return Bot(token, session=AiohttpSession(proxy=proxy) if proxy else None)


async def check_token(token: str, proxy: str) -> tuple[str | None, str]:
    bot = make_bot(token, proxy)
    try:
        me = await bot.get_me()
        return me.username, ""
    except TelegramUnauthorizedError:
        return None, "token"
    except (TelegramNetworkError, OSError) as e:
        return None, f"network: {e}"
    except Exception as e:
        return None, str(e)
    finally:
        await bot.session.close()


def setup_token(values: dict[str, str]) -> str:
    say("step 2/4 · Telegram bot")
    say("  open @BotFather in Telegram, send /newbot and copy the token it gives you.")
    current = values.get("TELEGRAM_BOT_TOKEN", "")
    if "replace-me" in current:
        current = ""
    while True:
        if current:
            token = getpass.getpass("  bot token (Enter keeps the current one): ").strip() or current
        else:
            token = getpass.getpass("  bot token: ").strip()
        if not token:
            continue
        username, error = asyncio.run(check_token(token, values.get("TELEGRAM_PROXY", "")))
        if username:
            values["TELEGRAM_BOT_TOKEN"] = token
            say(f"  connected to @{username}")
            say()
            return username
        if error == "token":
            say("  Telegram rejected this token. copy it again from @BotFather.")
            current = ""
            continue
        say(f"  cannot reach Telegram ({error}).")
        proxy = ask("  proxy url, for example socks5://127.0.0.1:1080, or Enter to retry", values.get("TELEGRAM_PROXY", ""))
        values["TELEGRAM_PROXY"] = proxy
        current = token


async def wait_for_user(token: str, proxy: str) -> tuple[int, str] | None:
    bot = make_bot(token, proxy)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAIT_SECONDS
        offset = None
        while loop.time() < deadline:
            updates = await bot.get_updates(offset=offset, timeout=20, allowed_updates=["message"])
            for update in updates:
                offset = update.update_id + 1
                message = update.message
                if message and message.from_user and message.chat.type == "private":
                    user = message.from_user
                    name = f"@{user.username}" if user.username else user.full_name
                    await bot.get_updates(offset=offset, timeout=0)
                    await bot.send_message(user.id, "✅ found you. finish the setup in the terminal.")
                    return user.id, name
        return None
    finally:
        await bot.session.close()


def setup_user(values: dict[str, str], username: str) -> None:
    say("step 3/4 · your Telegram account")
    current = values.get("ALLOWED_USER_IDS", "")
    if current and current != "123456789":
        say(f"  allowed user ids: {current}")
        if not confirm("  add another account?", default=False):
            say()
            return
    else:
        current = ""
    ids = [x for x in current.split(",") if x]
    say(f"  open https://t.me/{username} and send it any message, for example /start.")
    say(f"  waiting up to {WAIT_SECONDS // 60} minutes. press Ctrl+C to enter the id by hand.")
    found = None
    try:
        found = asyncio.run(wait_for_user(values["TELEGRAM_BOT_TOKEN"], values.get("TELEGRAM_PROXY", "")))
    except KeyboardInterrupt:
        say()
    if found:
        uid, name = found
        say(f"  message from {name} (id {uid})")
        if confirm("  allow this account to control Claude Code?"):
            ids.append(str(uid))
    while not ids:
        manual = ask("  your numeric Telegram user id (ask @userinfobot)")
        if manual.isdigit():
            ids.append(manual)
    values["ALLOWED_USER_IDS"] = ",".join(dict.fromkeys(ids))
    say()


def setup_cwd(values: dict[str, str]) -> None:
    say("step 4/4 · working folder")
    say("  new chats start in this folder. /cd changes it from the chat.")
    while True:
        cwd = ask("  folder", values.get("DEFAULT_CWD", "~"))
        if Path(cwd).expanduser().is_dir():
            values["DEFAULT_CWD"] = cwd
            break
        say("  this folder does not exist.")
    say()


def systemd_available() -> bool:
    return shutil.which("systemctl") is not None and subprocess.run(
        ["systemctl", "--user", "cat", UNIT], capture_output=True
    ).returncode == 0


def main() -> None:
    if not sys.stdin.isatty():
        say(f"run this in a terminal, or edit {CONFIG_FILE} by hand.")
        raise SystemExit(1)
    say("clawd-tg-bot setup")
    say()
    values = read_config()
    try:
        setup_claude(values)
        username = setup_token(values)
        setup_user(values, username)
        setup_cwd(values)
    except KeyboardInterrupt:
        say()
        say("setup cancelled, nothing saved.")
        raise SystemExit(1)
    write_config(values)
    say(f"saved {CONFIG_FILE}")
    if systemd_available():
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0
        if confirm("restart the bot now?" if active else "start the bot now?"):
            subprocess.run(["systemctl", "--user", "restart", UNIT], check=False)
            say(f"started. write to https://t.me/{username}")
            return
    say(f"start it with: {UNIT} on")


if __name__ == "__main__":
    main()
