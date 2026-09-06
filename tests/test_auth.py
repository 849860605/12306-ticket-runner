import json
import stat
from unittest.mock import AsyncMock, MagicMock

import pytest

from ticket_runner.auth import restore_session, save_session
from ticket_runner.browser import BrowserAdapter
from ticket_runner.domain import ORDER_URL, NeedsAttention


async def test_session_only_cookies_survive_restart_without_expiry_changes(tmp_path):
    cookie = {"name": "test", "value": "private", "domain": ".12306.cn", "path": "/", "expires": -1}
    context = AsyncMock()
    context.cookies.return_value = [cookie]
    await save_session(context, tmp_path)
    checkpoint = tmp_path / "session-cookies.json"
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    fresh_context = AsyncMock()
    await restore_session(fresh_context, tmp_path)
    fresh_context.add_cookies.assert_awaited_once_with([cookie])
    assert not list(tmp_path.glob(".session-*"))


async def test_checkpoint_excludes_unrelated_and_lookalike_domains(tmp_path):
    context = AsyncMock()
    context.cookies.return_value = [
        {"domain": "kyfw.12306.cn", "name": "allowed", "value": "secret"},
        {"domain": "12306.cn.attacker.example", "value": "other"},
        {"domain": "evil12306.cn", "value": "other"},
    ]
    await save_session(context, tmp_path)
    saved = json.loads((tmp_path / "session-cookies.json").read_text())
    assert len(saved) == 1 and saved[0]["domain"] == "kyfw.12306.cn"


@pytest.mark.parametrize("contents", ["broken", "{}", '[{"domain":"example.com"}]', "[null]"])
async def test_invalid_checkpoint_stops_without_leaking_contents(tmp_path, contents):
    (tmp_path / "session-cookies.json").write_text(contents)
    context = AsyncMock()
    with pytest.raises(NeedsAttention, match="登录状态文件无法恢复"):
        await restore_session(context, tmp_path)
    context.add_cookies.assert_not_awaited()


async def test_first_start_without_checkpoint(tmp_path):
    context = AsyncMock()
    await restore_session(context, tmp_path)
    context.add_cookies.assert_not_awaited()


async def test_valid_restored_session_does_not_reopen_login_page(config, tmp_path, monkeypatch):
    (tmp_path / "session-cookies.json").write_text("[]")
    adapter = BrowserAdapter(config, tmp_path)
    adapter.page = MagicMock()
    adapter.page.goto = AsyncMock()
    adapter.page.get_by_role.return_value.wait_for = AsyncMock()
    adapter.authenticated = AsyncMock(side_effect=[False, True])
    save = AsyncMock()
    monkeypatch.setattr("ticket_runner.browser.save_session", save)
    await adapter.ensure_login()
    adapter.page.goto.assert_awaited_once_with(ORDER_URL, wait_until="domcontentloaded")
    save.assert_awaited_once()
