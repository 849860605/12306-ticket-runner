"""Private cookie checkpoint for this application's dedicated browser profile.

Chromium's profile alone does not reliably restore session-only cookies after exit.
This checkpoint preserves their original fields; it cannot extend server-side login
validity. No state is sent anywhere except to the same browser context on startup.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .domain import NeedsAttention


def official_cookie(cookie: dict) -> bool:
    domain = str(cookie.get("domain", "")).lstrip(".").lower()
    return domain == "12306.cn" or domain.endswith(".12306.cn")


async def restore_session(context, data_dir: Path):
    checkpoint = data_dir / "session-cookies.json"
    if not checkpoint.exists():
        return
    try:
        cookies = json.loads(checkpoint.read_text(encoding="utf-8"))
        if not isinstance(cookies, list) or any(
            not isinstance(cookie, dict) or not official_cookie(cookie) for cookie in cookies
        ):
            raise ValueError("invalid checkpoint")
        await context.add_cookies(cookies)
    except Exception:
        raise NeedsAttention("登录状态文件无法恢复，请保留备份并重新扫码登录") from None


async def save_session(context, data_dir: Path):
    # Include session cookies without changing expiration or bypassing server checks.
    cookies = [cookie for cookie in await context.cookies() if official_cookie(cookie)]
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".session-", dir=data_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(cookies, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, data_dir / "session-cookies.json")
    finally:
        Path(temporary).unlink(missing_ok=True)
