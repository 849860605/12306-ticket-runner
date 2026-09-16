import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ticket_runner.browser import BrowserAdapter
from ticket_runner.domain import NeedsAttention
from ticket_runner.profile import clear_container_singletons, profile_lease

from .test_query_api import response, wire_payload


@pytest.fixture
def runtime(monkeypatch):
    playwright = MagicMock()
    playwright.stop = AsyncMock()
    request = MagicMock()
    request.dispose = AsyncMock()
    request.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
    request.get = AsyncMock(side_effect=[
        response(b"var CLeftTicketUrl='leftTicket/queryG';", content_type="text/html"),
        response(wire_payload()),
    ])
    playwright.request.new_context = AsyncMock(return_value=request)
    playwright.chromium.launch_persistent_context = AsyncMock(
        side_effect=RuntimeError("browser unavailable")
    )
    manager = MagicMock()
    manager.start = AsyncMock(return_value=playwright)
    monkeypatch.setattr("ticket_runner.browser.async_playwright", lambda: manager)
    return playwright, request


async def test_api_catalog_runs_when_chromium_cannot_start(config, tmp_path, runtime):
    config.query_backend = "api"
    playwright, request = runtime
    async with BrowserAdapter(config, tmp_path) as adapter:
        rows = await adapter.query_catalog(config.journey.dates[0])
        assert rows[0]["train"] == "G698"
        assert adapter.page is adapter.context is None
        playwright.chromium.launch_persistent_context.assert_not_awaited()
    request.dispose.assert_awaited_once()
    playwright.stop.assert_awaited_once()


async def test_failed_login_browser_start_does_not_break_api_query(config, tmp_path, runtime):
    config.query_backend = "api"
    async with BrowserAdapter(config, tmp_path) as adapter:
        with pytest.raises(RuntimeError, match="browser unavailable"):
            await adapter.ensure_login()
        assert len(await adapter.query_catalog(config.journey.dates[0])) == 1
        assert adapter.profile_lock is None


async def test_closed_browser_query_uses_fresh_http_transport(config, tmp_path, runtime):
    config.query_backend = "api"
    playwright, request = runtime
    async with BrowserAdapter(config, tmp_path) as adapter:
        adapter.context = MagicMock()
        adapter.context.close = AsyncMock()
        adapter.api_request = None
        adapter.api_client = MagicMock()
        adapter.browser_closed(adapter.context)
        assert len(await adapter.query_catalog(config.journey.dates[0])) == 1
        assert playwright.request.new_context.await_count == 2
        adapter.context.request.get.assert_not_called()


async def test_lazy_browser_handoff_shares_cookies_and_releases_http(config, tmp_path, runtime):
    config.query_backend = "api"
    playwright, request = runtime
    context = MagicMock()
    context.add_cookies = AsyncMock()
    context.close = AsyncMock()
    context.new_page = AsyncMock()
    page = MagicMock()
    page.is_closed.return_value = False
    context.new_page.return_value = page
    playwright.chromium.launch_persistent_context.side_effect = None
    playwright.chromium.launch_persistent_context.return_value = context
    cookie = {"name": "test", "value": "private", "domain": ".12306.cn", "path": "/", "expires": -1}
    (tmp_path / "session-cookies.json").write_text(json.dumps([cookie]))
    request.storage_state.return_value = {"cookies": [cookie], "origins": []}
    async with BrowserAdapter(config, tmp_path) as adapter:
        adapter.authenticated = AsyncMock(return_value=False)
        await adapter.ensure_browser()
        await adapter.ensure_browser()
        assert adapter.query_client().request is context.request
        assert adapter.api_request is None
        assert context.add_cookies.await_args.args[0] == [cookie]
        playwright.chromium.launch_persistent_context.assert_awaited_once()
        request.dispose.assert_awaited_once()
    assert adapter.profile_lock is None


@pytest.fixture
def container_profile(tmp_path, monkeypatch):
    from pathlib import Path

    root = tmp_path / "proc"
    root.mkdir()
    container = tmp_path / ".dockerenv"
    container.touch()
    monkeypatch.setattr("ticket_runner.profile.Path", lambda path: root if path == "/proc" else container)
    monkeypatch.setattr("ticket_runner.profile.socket.gethostname", lambda: "b" * 12)
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("a" * 12 + "-27")
    (profile / "SingletonCookie").symlink_to("123456")
    (profile / "SingletonSocket").symlink_to(tmp_path / "gone" / "SingletonSocket")
    (profile / "Preferences").write_text("keep profile")
    assert isinstance(profile, Path)
    return profile, root


def test_dead_docker_markers_removed_without_resetting_profile(container_profile):
    profile, _ = container_profile
    clear_container_singletons(profile)
    assert not any(p.is_symlink() for p in profile.iterdir())
    assert (profile / "Preferences").read_text() == "keep profile"


def test_active_socket_preserves_all_markers(container_profile):
    profile, _ = container_profile
    target = (profile / "SingletonSocket").readlink()
    target.parent.mkdir()
    target.touch()
    with pytest.raises(NeedsAttention):
        clear_container_singletons(profile)
    assert (profile / "SingletonLock").is_symlink()


def test_running_profile_process_preserves_all_markers(container_profile):
    profile, root = container_profile
    process = root / "27"
    process.mkdir()
    (process / "cmdline").write_bytes(f"chrome\0--user-data-dir={profile}\0".encode())
    with pytest.raises(NeedsAttention):
        clear_container_singletons(profile)
    assert (profile / "SingletonLock").is_symlink()


@pytest.mark.parametrize("target", ["b" * 12 + "-27", "another-host-27"])
def test_current_or_unrecognized_host_lock_is_left_to_chromium(container_profile, target):
    profile, _ = container_profile
    lock = profile / "SingletonLock"
    lock.unlink()
    lock.symlink_to(target)
    clear_container_singletons(profile)
    assert lock.is_symlink()


def test_profile_lease_blocks_second_adapter_before_cleanup(tmp_path, monkeypatch):
    cleanup = MagicMock()
    monkeypatch.setattr("ticket_runner.profile.clear_container_singletons", cleanup)
    with profile_lease(tmp_path):
        with pytest.raises(NeedsAttention), profile_lease(tmp_path):
            pass
        cleanup.assert_called_once()
    with profile_lease(tmp_path):
        assert cleanup.call_count == 2
