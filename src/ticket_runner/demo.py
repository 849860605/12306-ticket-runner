"""An offline example of the most important failure: response loss after submission."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .config import Config
from .domain import SHANGHAI, Offer
from .notify import Notifier
from .runner import Runner
from .state import Store


async def demonstrate():
    now = datetime.now(SHANGHAI)
    departure = now + timedelta(days=1)
    config = Config.model_validate(
        {
            "task_id": "offline-demo",
            "journey": {
                "origin": {"name": "深圳北", "code": "IOQ"},
                "destination": {"name": "南京南", "code": "NKH"},
                "dates": [departure.date()],
                "passengers": ["模拟乘车人"],
            },
            "preferences": {"trains": ["G698"], "seats": ["二等座"], "max_total_price": 1000},
            "execution": {
                "start_at": now - timedelta(seconds=1),
                "stop_at": now + timedelta(hours=1),
                "auto_submit": True,
            },
            "notification": {"webhook_url_env": "TICKET_DEMO_UNUSED_WEBHOOK"},
        }
    )
    offer = Offer(
        "ticket_demo_01_02",
        "G698",
        departure.date(),
        "深圳北",
        "南京南",
        departure.time().replace(tzinfo=None),
        "二等座",
        Decimal("764"),
        1,
    )

    class SimulatedAdapter:
        submitted = 0

        async def ensure_login(self):
            pass

        async def check_existing_orders(self):
            pass

        async def query(self, _):
            return [offer]

        async def prepare(self, _):
            pass

        async def submit(self, _):
            self.submitted += 1
            raise TimeoutError("Simulated response loss")

        async def reconcile(self, _):
            return "DEMO-ORDER-ONLY"

    class OfflineNotifier(Notifier):
        async def flush(self):
            # Deliberately no outbound requests, even if environment contains credentials.
            pass

    with tempfile.TemporaryDirectory(prefix="ticket-runner-demo-") as temp:
        store = Store(Path(temp))
        try:
            adapter = SimulatedAdapter()
            notifier = OfflineNotifier(store, config.notification)
            await Runner(config, store, adapter, notifier).run()
            first = store.get(config.task_id)["state"]
            await Runner(config, store, adapter, notifier).run()
            second = store.get(config.task_id)["state"]
            await Runner(config, store, adapter, notifier).run()
            print(
                json.dumps(
                    {
                        "network": "disabled",
                        "first_run": first,
                        "after_restart": second,
                        "total_submit_calls": adapter.submitted,
                        "passed": first == "UNKNOWN"
                        and second == "ORDER_CREATED"
                        and adapter.submitted == 1,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            store.close()
