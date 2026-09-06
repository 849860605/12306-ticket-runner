from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import NotificationConfig
from .state import Store

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, store: Store, config: NotificationConfig):
        self.store, self.config = store, config
        self._flush_lock = asyncio.Lock()

    async def flush(self):
        async with self._flush_lock:
            await self._flush()

    async def _flush(self):
        rows = self.store.db.execute(
            "SELECT * FROM outbox WHERE delivered=0 AND attempts<? AND next_try<=? ORDER BY id LIMIT 10",
            (self.config.attempts, time.time()),
        ).fetchall()
        endpoint = os.environ.get(self.config.webhook_url_env, "")
        for row in rows:
            if not endpoint:
                # Keep the outbox durable: adding the webhook later can still deliver it.
                if row["attempts"] == 0:
                    log.warning("通知待发送（未配置 Webhook）：%s %s", row["title"], row["body"])
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE outbox SET next_try=? WHERE id=?", (time.time() + 60, row["id"])
                    )
                continue
            try:
                if urlparse(endpoint).scheme != "https":
                    raise ValueError("Webhook requires HTTPS")
                payload = {
                    "title": row["title"],
                    "body": row["body"],
                    "text": f"{row['title']}\n{row['body']}",
                    "task_id": row["task_id"],
                }
                await asyncio.to_thread(self._post, endpoint, payload)
            except Exception as exc:
                # Exception strings can contain webhook tokens. Never log them.
                log.warning("通知发送失败：%s，第 %d 次", type(exc).__name__, row["attempts"] + 1)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=?",
                        (time.time() + min(30 * 2 ** row["attempts"], 900), row["id"]),
                    )
            else:
                with self.store.db:
                    self.store.db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (row["id"],))

    @staticmethod
    def _post(endpoint: str, payload: dict):
        request = Request(
            endpoint,
            json.dumps(payload, ensure_ascii=False).encode(),
            {"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError("Webhook returned non-success status")
