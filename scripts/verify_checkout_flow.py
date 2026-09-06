"""Full synthetic checkout using real Chromium and production adapter, OFFLINE only.

Run with Docker --network none, without the user's data volume. All URLs are routed
to synthetic fixtures; no real login, passenger, order or notification is involved.
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from ticket_runner.browser import BrowserAdapter
from ticket_runner.config import Config
from ticket_runner.domain import SHANGHAI
from ticket_runner.runner import Runner
from ticket_runner.state import Store

LOGGED_IN = '<a href="#">退出</a>'
ORDER_TAB = '<ul id="order_tab"><li class="active"><a href="#">未完成订单</a></li></ul>'
EMPTY = '<div class="order-empty"><div class="empty-txt"><p>您没有未完成的订单哦～</p></div></div>'
PENDING = """<div class="order-panel-unpaid"><div class="order-item-bd">
<table class="order-item-table"><tr>
<td><div class="order-info-ticket"><div>深圳北<i class="icon icon-to"></i>南京南 G698</div>
<div>2100-10-01&nbsp;&nbsp;08:35 开</div></div></td>
<td><div class="passenger-operation"><span class="name-yichu">张三</span><div>居民身份证</div></div></td>
<td><div>二等座</div><div>01车01A号</div></td>
<td><div>成人票</div><div><span>764元</span> <span>8.6折</span></div></td>
<td><div class="ticket-status-name">待支付</div></td>
</tr></table></div></div>"""
QUERY = """<table id="queryLeftTable"><tr id="ticket_test_01_02">
<td><a class="number">G698</a><div class="cdz"><strong>深圳北</strong><strong>南京南</strong></div>
<div class="cds"><strong>08:35</strong><strong>17:00</strong></div></td>
<td aria-label="G698次列车，二等座票价764元，余票1">1</td>
<td><a href="/otn/confirmPassenger/initDc">预订</a></td></tr></table>"""
CHECKOUT = """<div id="ticket_tit_id">2100-10-01 G698次深圳北站（08:35开）—南京南站（17:00到）</div>
<ul id="normal_passenger_id"><li style="float:left"><input type="checkbox" id="normalPassenger_0">
<label for="normalPassenger_0">张三</label></li></ul>
<div style="clear:both"><input id="passenger_name_1" value="张三">
<select id="ticketType_1"><option>成人票</option></select>
<select id="seatType_1"><option value="O">二等座（¥764元）</option></select></div>
<button id="submitOrder_id" onclick="document.querySelector('#content_checkticketinfo_id').style.display='block'">提交订单</button>
<div id="content_checkticketinfo_id" style="display:none">
<div>2100-10-01 G698次深圳北站（08:35开）—南京南站（17:00到）</div>
<table><tr><td>张三</td><td>成人票</td><td>二等座</td></tr></table>
<button id="qr_submit_id" onclick="location.href='/otn/payOrder/init'">确认</button></div>"""


class OfflineNotifier:
    async def flush(self):
        pass


async def verify():
    now = datetime.now(SHANGHAI)
    config = Config.model_validate(
        {
            "task_id": "offline-only",
            "journey": {
                "origin": {"name": "深圳北", "code": "IOQ"},
                "destination": {"name": "南京南", "code": "NKH"},
                "dates": ["2100-10-01"],
                "passengers": ["张三"],
            },
            "preferences": {"trains": ["G698"], "seats": ["二等座"], "max_total_price": 1000},
            "execution": {
                "start_at": now - timedelta(seconds=1),
                "stop_at": now + timedelta(minutes=2),
                "auto_submit": True,
                "query_interval_seconds": 5,
            },
            "browser": {"headless": True, "timeout_seconds": 5},
        }
    )
    calls = {"query": 0, "submit": 0, "unknown": 0}

    async def serve(route):
        path = urlparse(route.request.url).path
        if path == "/fixture-login":
            body = ""
        elif path == "/otn/view/train_order.html":
            body = ORDER_TAB + (PENDING if calls["submit"] else EMPTY)
        elif path == "/otn/leftTicket/init":
            calls["query"] += 1
            body = QUERY
        elif path == "/otn/confirmPassenger/initDc":
            body = CHECKOUT
        elif path == "/otn/payOrder/init":
            calls["submit"] += 1
            body = "<p>席位已锁定</p>"
        else:
            calls["unknown"] += 1
            await route.abort()
            return
        await route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=LOGGED_IN + body
        )

    with TemporaryDirectory(prefix="ticket-offline-") as directory:
        data = Path(directory)
        store = Store(data)
        try:
            async with BrowserAdapter(config, data) as adapter:
                await adapter.context.route("**/*", serve)
                await adapter.page.goto("https://kyfw.12306.cn/fixture-login")
                await Runner(config, store, adapter, OfflineNotifier()).run()
                status = store.status()["tasks"][0]
                assert status["state"] == "ORDER_CREATED", status
                assert status["order_id"] is None
                assert status["receipt"]["total_price"] == "764"
                assert status["receipt"]["assigned_seats"] == ["01车01A号"]
                store.close()
                store = Store(data)
                await Runner(config, store, adapter, OfflineNotifier()).run()
                assert calls == {"query": 1, "submit": 1, "unknown": 0}, calls
                print(
                    "PASS: real Chromium + production adapter synthetic query → prepare → confirm → receipt → SQLite reopen; exactly one simulated submit."
                )
                print(store.timing_report())
        finally:
            store.close()


if __name__ == "__main__":
    asyncio.run(verify())
