"""Read-only order-page diagnostics; never select a train or submit an order.

Run inside the project container with /data mounted and no other runner active.
Only login status, visible element structure and short empty-state labels are printed.
Cookies, input values, passenger details and full page contents are not exported.
"""

import asyncio
import json
from pathlib import Path

from ticket_runner.browser import BrowserAdapter
from ticket_runner.config import load_config
from ticket_runner.domain import ORDER_URL, NeedsAttention
from ticket_runner.state import exclusive_run


async def inspect():
    config = load_config(Path("/config/config.yaml"))
    config.browser.timeout_seconds = 15
    with exclusive_run(Path("/data")):
        async with BrowserAdapter(config, Path("/data")) as adapter:
            await adapter.page.goto(ORDER_URL, wait_until="domcontentloaded")
            try:
                await adapter.page.get_by_role("link", name="退出", exact=True).wait_for()
            except Exception:
                print(json.dumps({"authenticated": False}))
                print(json.dumps(await adapter.account_diagnostics(), ensure_ascii=False, indent=2))
                return
            print(json.dumps({"authenticated": await adapter.authenticated()}))
            try:
                await adapter.check_existing_orders()
                print("account_check_passed")
            except NeedsAttention as exc:
                print(str(exc))
            snapshot = await adapter.account_diagnostics()
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(inspect())
