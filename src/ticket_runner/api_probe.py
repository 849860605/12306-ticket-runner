"""Explicit read-only API feasibility probe; never registers or submits a task."""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.async_api import async_playwright

from .browser import BrowserAdapter
from .domain import SHANGHAI, parse_catalog, rank_offers
from .query_api import ReadOnlyQueryClient, inventory_offers
from .state import exclusive_run


def emit(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)


async def probe_api(
    config, data_dir, *, with_session=False, compare_browser=False, all_dates=False
):
    async def inspect(client, adapter=None):
        if with_session:
            emit(
                stage="session_check",
                authenticated=await client.check_login(),
                note="只检查已有登录态，不扫码、不查询乘车人、不下单",
            )
        days = config.journey.dates if all_dates else config.journey.dates[:1]
        last_start = None
        for day in days:
            if last_start is not None:
                await asyncio.sleep(
                    max(
                        0, config.execution.query_interval_seconds - (time.monotonic() - last_start)
                    )
                )
            last_start = time.monotonic()
            rows = await client.catalog(day, config)
            offers = inventory_offers(rows, config)
            matches = rank_offers(offers, config, datetime.now(SHANGHAI))
            emit(
                stage="api_query",
                mode="read_only",
                date=str(day),
                **client.last_metrics,
                matches=[item.to_dict() for item in matches],
            )
            if compare_browser:
                await asyncio.sleep(
                    max(
                        0, config.execution.query_interval_seconds - (time.monotonic() - last_start)
                    )
                )
                last_start = time.monotonic()
                dom_rows = await adapter.query_rows(day)
                elapsed = time.monotonic() - last_start
                dom_catalog = parse_catalog(dom_rows, day, config)

                def identities(values):
                    return {
                        (x["train"], x["origin"], x["destination"], x["departure"]) for x in values
                    }

                def prices(values):
                    return {
                        (x["train"], s["name"]): s["price"]
                        for x in values
                        for s in x["seats"]
                        if s["price"] is not None
                    }

                a, b = prices(rows), prices(dom_catalog)
                common = a.keys() & b.keys()
                emit(
                    stage="browser_comparison",
                    seconds=round(elapsed, 3),
                    trains=len(dom_catalog),
                    same_timetable=identities(rows) == identities(dom_catalog),
                    common_quotes=len(common),
                    same_common_prices=all(a[k] == b[k] for k in common),
                    note="顺序单样本，库存可能变化；不是长期性能或成功率结论",
                )

    if with_session:
        with exclusive_run(data_dir):
            emit(stage="browser_start", headless=config.browser.headless, note="复用专用会话，不进入预订")
            async with BrowserAdapter(config, data_dir) as adapter:
                emit(stage="browser_ready", note="开始只读接口检查")
                await inspect(adapter.query_client(), adapter)
    elif compare_browser:
        # Never accidentally reuse an account just to measure anonymous query speed.
        with TemporaryDirectory(prefix="ticket-api-probe-") as directory:
            isolated = config.model_copy(deep=True)
            isolated.browser.headless = True
            async with BrowserAdapter(isolated, Path(directory)) as adapter:
                await inspect(adapter.query_client(), adapter)
    else:
        async with async_playwright() as playwright:
            request = await playwright.request.new_context()
            try:
                await inspect(
                    ReadOnlyQueryClient(request, timeout_seconds=config.browser.timeout_seconds)
                )
            finally:
                await request.dispose()
