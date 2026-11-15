# clawd-tg-bot

English · [Русский](README.ru.md)

a Telegram bot that lets you drive Claude Code on your own machine from a phone. you send a message, Claude works in a folder on that machine, and the reply comes back to the chat.

the bot runs your installed `claude` CLI under your login, so your settings, permission rules and `CLAUDE.md` files apply the same way they do in a terminal.

## features

- sessions per chat: start a new one, resume a recent one (including sessions you started in a terminal), switch folders;
- permission prompts as buttons: allow once, deny, or allow for the rest of the session;
- questions from Claude as buttons, with free-text answers;
- a live progress message that lists tool calls;
- photos and files you send land in an inbox folder, and Claude gets the path;
- Claude can send files and images back to the chat;
- model, reasoning effort and permission mode switches;
- `/status` with context usage and subscription limits;
- a message queue while Claude is busy.

## requirements

- Linux with systemd (user units);
- Python 3 with `venv` (tested on 3.14);
- Claude Code installed and logged in (`claude` in `PATH`);
- a Telegram account. the setup walks you through creating a bot with [@BotFather](https://t.me/BotFather).

## install

```sh
git clone https://github.com/chuh420/clawd-tg-bot
cd clawd-tg-bot
./install.sh
```

`install.sh` creates `.venv`, installs dependencies, writes a systemd user unit, links the `clawd-tg-bot` command into `~/.local/bin` and starts the setup. run it again after you move the repository.

## setup

the setup asks a few questions and writes the config for you:

1. finds your `claude` binary and checks that it is logged in. if it is not, it offers to run `claude auth login`;
2. asks for the bot token from @BotFather and checks it with Telegram. if Telegram is unreachable, it asks for a proxy;
3. asks you to send any message to the new bot, picks up your user id from it and asks you to confirm;
4. asks for the starting folder, saves the config and offers to start the bot.

run `clawd-tg-bot setup` again at any time to change the token, add another account or pick another folder. Enter keeps the current value.

## configuration

the setup writes `~/.config/clawd-tg-bot/env`. you can also edit it by hand with `clawd-tg-bot config`:

| variable | meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | token from @BotFather |
| `ALLOWED_USER_IDS` | Telegram user ids allowed to use the bot, comma-separated |
| `DEFAULT_CWD` | starting folder for new chats, `~` by default |
| `CLAUDE_PATH` | path to the `claude` binary if it is not in `PATH` |
| `TELEGRAM_PROXY` | optional proxy, for example `socks5://127.0.0.1:1080` |
| `LOG_LEVEL` | `INFO` by default |

## running

```sh
clawd-tg-bot setup     # first setup or changes
clawd-tg-bot on        # start
clawd-tg-bot off       # stop
clawd-tg-bot status    # active / inactive / failed
clawd-tg-bot logs      # follow the log
clawd-tg-bot restart   # after a config or code change
clawd-tg-bot config    # edit the config
```

the unit has no `[Install]` section, so the bot does not start at boot. you start it when you need it. `/off` in the chat stops it; to start it again you need shell access to the machine.

## commands

| command | action |
| --- | --- |
| `/new` | new session in the current folder |
| `/resume` | pick one of the last 8 sessions in the current folder |
| `/cd path` | change folder and start a new session |
| `/mode` | permission mode: default, acceptEdits, plan, bypassPermissions |
| `/model` | switch model |
| `/effort` | reasoning effort: low, medium, high, xhigh, max, default |
| `/stop` | interrupt the current turn and clear the queue |
| `/status` | folder, session, mode, model, context usage, limits |
| `/off` | shut the bot down |

a permission prompt without an answer counts as a denial after 10 minutes.

## security

anyone in `ALLOWED_USER_IDS` can run commands on your machine with your user's rights. keep the list to your own accounts and keep the token secret. the bot answers those ids only and logs every other sender.

`bypassPermissions` mode removes the approval buttons. use it only in folders where you would run Claude Code unattended.

## files

- config: `~/.config/clawd-tg-bot/env`;
- per-chat state (folder, session, mode, model): `~/.local/state/clawd-tg-bot/state.json`;
- received files: `~/.local/state/clawd-tg-bot/inbox/`.

## repository

- `clawd_tg_bot.py`: the bot;
- `setup_wizard.py`: the interactive setup behind `clawd-tg-bot setup`;
- `install.sh`: venv, systemd user unit, command link, then the setup;
- `deploy/`: the `clawd-tg-bot` command, the unit template and the config template.

## notes

`/status` reads subscription limits from an undocumented endpoint with the OAuth token Claude Code stores in `~/.claude/.credentials.json`. if that endpoint changes, the limits line shows "unavailable" and the rest of the bot keeps working.

this project is not affiliated with Anthropic.

## license

[MIT](LICENSE)
