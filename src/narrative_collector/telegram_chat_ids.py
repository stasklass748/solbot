"""Show private chat IDs for people who sent /start to this Telegram bot."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .config import load_dotenv
from .telegram_ui import telegram_request


def main() -> None:
    parser = argparse.ArgumentParser(description="Show Telegram private chat IDs without printing the bot token")
    parser.add_argument("--env-dir", default=".")
    args = parser.parse_args()
    load_dotenv(Path(args.env_dir).resolve() / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        parser.exit(1, "Set TELEGRAM_BOT_TOKEN in .env first.\n")
    try:
        updates = telegram_request(token, "getUpdates", {
            "limit": 100, "timeout": 0, "allowed_updates": ["message"],
        })
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Telegram lookup failed: {exc}\n")
    if not isinstance(updates, list):
        parser.exit(1, "Telegram returned an unexpected response.\n")
    found: dict[str, str] = {}
    for update in updates:
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            continue
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if (chat.get("type") != "private" or not isinstance(message.get("text"), str)
                or message["text"].split(maxsplit=1)[0].split("@")[0] != "/start"):
            continue
        chat_id = str(chat.get("id", ""))
        user_id = str(sender.get("id", ""))
        if chat_id and chat_id == user_id:
            found[chat_id] = str(sender.get("first_name") or "")[:40]
    if found:
        for chat_id, name in found.items():
            print(f"chat_id={chat_id}  name={name}")
    else:
        print("No private /start messages found. Ask both people to send /start to this bot.")


if __name__ == "__main__":
    main()
