"""Telegram notifier: sends a short message when a job starts or ends. It stays silent until a bot
token and a chat id exist in the config directory, so it can be installed before the bot is created."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .queue import JobRow
from .runner import CONF_DIR, safe


def format_event(event: str, row: JobRow) -> str | None:
    if event == "started":
        return f"Started: {row.repo}\n{safe(row.task.splitlines()[0] if row.task.strip() else 'task', 80)}"
    if event != "finished":
        return None
    if row.status == "pr-open":
        return f"Draft PR ready: {row.pr}"
    if row.status == "no-changes":
        return f"No changes were needed ({row.repo})."
    if row.status == "cancelled":
        return f"Cancelled ({row.repo})."
    return f"Failed ({row.repo}): {safe(row.reason or row.status, 200)}"


def _send(token: str, chat_id: str, text: str) -> None:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", method="POST",
        data=json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"telegram send failed: HTTP {e.code}") from None  # no URL: it contains the token
    except (urllib.error.URLError, OSError):
        raise RuntimeError("telegram send failed: network error") from None


class TelegramNotifier:
    def __init__(self, conf_dir: Path | None = None, send: Callable[[str, str, str], None] = _send):
        self.conf_dir, self.send = conf_dir, send
        self.__name__ = "telegram"

    def _creds(self) -> tuple[str, str] | None:
        base = self.conf_dir or CONF_DIR
        try:
            return (base / "telegram-token").read_text().strip(), (base / "telegram-chat").read_text().strip()
        except OSError:
            return None

    def __call__(self, event: str, row: JobRow) -> None:
        creds, text = self._creds(), format_event(event, row)
        if creds and creds[0] and creds[1] and text:
            self.send(creds[0], creds[1], text)
