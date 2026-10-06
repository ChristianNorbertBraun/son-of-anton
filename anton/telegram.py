"""Telegram notifier: sends a short message when a job starts or ends. It stays silent until a bot
token and a chat id exist in the config directory, so it can be installed before the bot is created."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import re
import uuid
from pathlib import Path
from typing import Callable

from .queue import JobRow
from .runner import CONF_DIR, safe, scrub

TASK_LIMIT = 3500  # Telegram allows 4096 characters per message


def full_text(text, limit: int = TASK_LIMIT) -> str:
    """The whole text (line breaks kept), without control characters or secrets, cut only above `limit`."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f\x9b]", "?", scrub(text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def format_event(event: str, row: JobRow) -> str | None:
    if getattr(row, "kind", "change") == "ask":
        return None  # a question is answered in the chat itself, no extra messages
    pr_number = getattr(row, "pr_number", None)
    if event == "started":
        what = f"{row.repo} PR #{pr_number}" if pr_number else row.repo
        return f"Started: {what}\n{full_text(row.task) or 'task'}"
    if event != "finished":
        return None
    if row.status == "pr-open":
        return f"PR updated: {row.pr}" if pr_number else f"Draft PR ready: {row.pr}"
    if row.status == "proposed":
        return (f"Patch ready, nothing was pushed ({row.repo}): {safe(row.reason or '', 600)}\n"
                f"Apply it with: git apply {patch_name(row)}")
    if row.status == "no-changes":
        return f"No changes were needed ({row.repo})."
    if row.status == "cancelled":
        return f"Cancelled ({row.repo})."
    return f"Failed ({row.repo}): {safe(row.reason or row.status, 800)}"


def patch_name(row: JobRow) -> str:
    return f"anton-{re.sub(r'[^A-Za-z0-9-]', '', row.id)}.patch"


def _send_file(token: str, chat_id: str, name: str, content: str) -> None:
    boundary = "----anton" + uuid.uuid4().hex
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
             (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{name}\"\r\n"
              "Content-Type: text/x-diff\r\n\r\n").encode(), content.encode(), f"\r\n--{boundary}--\r\n".encode()]
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument", method="POST", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"telegram send failed: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError):
        raise RuntimeError("telegram send failed: network error") from None


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
    def __init__(self, conf_dir: Path | None = None, send: Callable[[str, str, str], None] = _send,
                 send_file: Callable[[str, str, str, str], None] = _send_file):
        self.conf_dir, self.send, self.send_file = conf_dir, send, send_file
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
            if row.status == "proposed" and row.answer:
                self.send_file(creds[0], creds[1], patch_name(row), row.answer)
