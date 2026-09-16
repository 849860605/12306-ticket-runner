"""Offline Chromium regressions for the real login adapter, with no account access."""

import asyncio
from contextlib import suppress

import pytest

from ticket_runner.browser import BrowserAdapter
from ticket_runner.domain import LOGIN_URL, ORDER_URL, NeedsAttention

LOGIN = """<!doctype html><meta charset="utf-8">
<a id="qr-tab" href="javascript:;">扫码登录</a>
<div id="qr-panel" hidden><img id="J-qrImg" width="128" height="128"></div>
<button onclick="document.querySelector('#J-qrImg').src='/fixture-image.svg'">加载测试图片</button>
<button onclick="document.body.innerHTML='<a href=#>退出</a>'">模拟确认</button>
<script src="/fixture-initialization.js"></script>
"""
IMAGE = '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128"><rect width="128" height="128" fill="green"/></svg>'


@pytest.fixture
async def login_browser(config, tmp_path):
    config.browser.headless = True
    config.browser.timeout_seconds = 5
    config.browser.login_timeout_seconds = 30
    requests = []
    state = {"restored": False, "image_loaded": False, "announcements": 0, "slow_scripts": False}
    (tmp_path / "session-cookies.json").write_text("[]")

    async def route(request):
        url = request.request.url
        requests.append(url)
        if url == ORDER_URL:
            if state["restored"]:
                await request.fulfill(
                    body='<a href="#">退出</a>', content_type="text/html; charset=utf-8"
                )
            else:
                # Model the official page's client-side redirect. Browser routing
                # does not intercept a server redirect's subsequent request.
                await request.fulfill(
                    body=f'<script>location.replace("{LOGIN_URL}")</script>',
                    content_type="text/html",
                )
        elif url == LOGIN_URL:
            await request.fulfill(body=LOGIN, content_type="text/html; charset=utf-8")
        elif url.endswith("/fixture-image.svg"):
            state["image_loaded"] = True
            await request.fulfill(body=IMAGE, content_type="image/svg+xml")
        elif url.endswith("/fixture-initialization.js"):
            if state["slow_scripts"]:
                await asyncio.sleep(0.6)
            await request.fulfill(
                body="document.querySelector('#qr-tab').onclick = () => {document.querySelector('#qr-panel').hidden=false};",
                content_type="application/javascript",
            )
        else:
            await request.abort()
            raise AssertionError(f"Unexpected request in offline login fixture: {url}")

    async with BrowserAdapter(config, tmp_path) as adapter:
        await adapter.context.route("**/*", route)
        await adapter.context.set_offline(True)
        ready = asyncio.Event()

        async def announce():
            state["announcements"] += 1
            assert state["image_loaded"], "Must not ask the user to scan an unloaded image"
            assert (tmp_path / "login-qr.png").is_file()
            ready.set()

        adapter.on_login_required = announce
        try:
            yield adapter, state, requests, ready
        finally:
            # Tests attach their task so failure/cancellation never leaks browser work.
            task = getattr(adapter, "test_login_task", None)
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task


def start_login(adapter):
    adapter.test_login_task = asyncio.create_task(adapter.ensure_login())
    return adapter.test_login_task


@pytest.mark.parametrize("slow_scripts", [False, True])
async def test_expired_session_redirect_opens_qr_without_waiting_for_logout(login_browser, slow_scripts):
    adapter, state, requests, ready = login_browser
    state["slow_scripts"] = slow_scripts
    task = start_login(adapter)
    # Five-second configured timeout must not delay a clearly visible login entry.
    await adapter.page.locator("#qr-panel").wait_for(state="visible", timeout=2000)
    assert not ready.is_set() and state["announcements"] == 0
    await adapter.page.get_by_role("button", name="加载测试图片").click()
    await asyncio.wait_for(ready.wait(), timeout=2)
    assert requests.count(LOGIN_URL) == 1, "Reuse the redirected page without another navigation"
    await adapter.page.get_by_role("button", name="模拟确认").click()
    await asyncio.wait_for(task, timeout=3)
    assert await adapter.authenticated()
    assert not (adapter.data_dir / "login-qr.png").exists()


async def test_restored_session_is_kept_without_requesting_a_scan(login_browser):
    adapter, state, requests, ready = login_browser
    state["restored"] = True
    await asyncio.wait_for(adapter.ensure_login(), timeout=2)
    assert requests == [ORDER_URL]
    assert not ready.is_set()


async def test_missing_qr_times_out_without_false_scan_prompt(login_browser):
    adapter, state, _, ready = login_browser
    task = start_login(adapter)
    with pytest.raises(NeedsAttention, match="二维码未能加载"):
        await asyncio.wait_for(task, timeout=7)
    assert state["announcements"] == 0 and not ready.is_set()
    assert not (adapter.data_dir / "login-qr.png").exists()


async def test_login_can_retry_after_user_pauses_while_qr_loads(login_browser):
    adapter, _, _, ready = login_browser
    first = start_login(adapter)
    await adapter.page.locator("#qr-panel").wait_for(state="visible", timeout=2000)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    loading = asyncio.Event()
    adapter.on_progress = lambda stage, _: loading.set() if stage == "LOGIN_QR_LOADING" else None
    second = start_login(adapter)
    await asyncio.wait_for(loading.wait(), timeout=2)
    await adapter.page.locator("#qr-panel").wait_for(state="visible", timeout=2000)
    await adapter.page.get_by_role("button", name="加载测试图片").click()
    await asyncio.wait_for(ready.wait(), timeout=2)
    await adapter.page.get_by_role("button", name="模拟确认").click()
    await asyncio.wait_for(second, timeout=3)
