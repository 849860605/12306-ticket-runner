"""Offline Chromium smoke test of order-page guards, with entirely synthetic HTML.

Runs with the project container's existing dependencies under --network none.
Does not open an official page, use the user's data volume, or load a login session.
"""

import asyncio

from playwright.async_api import async_playwright

from ticket_runner.browser import (
    DEFAULT_SELECTORS,
    EMPTY_ORDER_PATTERN,
    ORDER_STATE_JS,
    BrowserAdapter,
)
from ticket_runner.domain import NeedsAttention


async def verify():
    settings = {
        "tab": DEFAULT_SELECTORS["order_tab"],
        "cards": DEFAULT_SELECTORS["order_cards"],
        "empty": DEFAULT_SELECTORS["order_empty"],
        "emptyPattern": EMPTY_ORDER_PATTERN,
    }
    active = '<ul id="order_tab"><li class="active"><a>未完成订单</a></li></ul>'
    empty = (
        '<div class="order-empty"><div class="empty-txt">'
        "<p>您没有未完成的订单哦～</p><p>其他静态说明</p></div></div>"
    )
    cases = [
        (active + empty, {"active": True, "empty": True, "hasCards": False}),
        (
            active.replace("未完成订单", "已完成订单") + empty,
            {"active": False, "empty": True, "hasCards": False},
        ),
        (
            active + '<div style="display:none">' + empty + "</div>",
            {"active": True, "empty": False, "hasCards": False},
        ),
        (
            active + "<p>您没有未完成的订单哦～</p>",
            {"active": True, "empty": False, "hasCards": False},
        ),
        (
            active
            + empty
            + '<div class="order-panel-unpaid"><div class="order-item-bd"><table class="order-item-table"><tr><td>待支付</td></tr></table></div></div>',
            {"active": True, "empty": True, "hasCards": True},
        ),
        (
            active + empty.replace("您没有未完成的订单哦～", "未知提示"),
            {"active": True, "empty": False, "hasCards": False},
        ),
    ]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            for number, (markup, expected) in enumerate(cases, 1):
                await page.set_content(markup)
                actual = await page.evaluate(ORDER_STATE_JS, settings)
                assert actual == expected, f"Case {number}: {actual} != {expected}"
            print(f"PASS: {len(cases)} offline Chromium order DOM cases; no account or network.")
            await page.set_content("""<ul id="normal_passenger_id">
                <li style="float:left"><input type="checkbox" id="normalPassenger_0" aria-label="额外说明">
                <label for="normalPassenger_0">张三</label></li>
                <li style="float:left"><input type="checkbox" id="normalPassenger_1">
                <label for="normalPassenger_1">张三丰</label></li>
                <li style="display:none"><label for="normalPassenger_1">张三</label></li>
                </ul>""")
            container = page.locator("#normal_passenger_id")
            checkbox = await BrowserAdapter.passenger_checkbox(container, "张三")
            await checkbox.check()
            assert await page.locator("#normalPassenger_0").is_checked()
            assert not await page.locator("#normalPassenger_1").is_checked()
            for mutation in (
                "document.querySelector('#normalPassenger_0').disabled = true",
                "document.querySelector('#normalPassenger_0').disabled = false; "
                "document.querySelector('#normal_passenger_id').insertAdjacentHTML('beforeend', '<li><label for=normalPassenger_1>张三</label></li>')",
            ):
                await page.evaluate(mutation)
                try:
                    await BrowserAdapter.passenger_checkbox(container, "张三")
                except NeedsAttention:
                    pass
                else:
                    raise AssertionError("Disabled or ambiguous passenger was not rejected")
            print("PASS: 3 offline Chromium passenger binding cases; no personal data.")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(verify())
