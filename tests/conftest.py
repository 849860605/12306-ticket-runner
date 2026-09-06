from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from ticket_runner.config import Config
from ticket_runner.domain import SHANGHAI, Offer


@pytest.fixture
def now():
    return datetime(2026, 9, 17, 10, 0, tzinfo=SHANGHAI)


@pytest.fixture
def config(now):
    return Config.model_validate(
        {
            "task_id": "test-trip",
            "journey": {
                "origin": {"name": "深圳北", "code": "IOQ"},
                "destination": {"name": "南京南", "code": "NKH"},
                "dates": ["2026-10-01"],
                "passengers": ["张三"],
            },
            "preferences": {
                "trains": ["G698", "G2756"],
                "seats": ["二等座", "一等座"],
                "max_total_price": "1000",
            },
            "execution": {
                "start_at": now - timedelta(seconds=1),
                "stop_at": now + timedelta(minutes=1),
                "auto_submit": True,
                "query_interval_seconds": 5,
                "max_consecutive_errors": 2,
            },
        }
    )


@pytest.fixture
def offer(config):
    return Offer(
        "ticket_6i000G69800_01_15",
        "G698",
        config.journey.dates[0],
        "深圳北",
        "南京南",
        datetime.strptime("08:35", "%H:%M").time(),
        "二等座",
        Decimal("764"),
        1,
    )


class FakeClock:
    def __init__(self, now):
        self.current = now

    def __call__(self):
        return self.current

    async def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


class FakeNotifier:
    async def flush(self):
        pass


class FakeAdapter:
    def __init__(self, offer):
        self.offer = offer
        self.login_calls = self.query_calls = self.prepare_calls = self.submit_calls = 0
        self.order_id = "E123456789"
        self.query_error = self.submit_error = self.prepare_error = None

    async def ensure_login(self):
        self.login_calls += 1

    async def check_existing_orders(self):
        pass

    async def query(self, _):
        self.query_calls += 1
        if self.query_error:
            raise self.query_error
        return [self.offer] if self.offer else []

    async def prepare(self, _):
        self.prepare_calls += 1
        if self.prepare_error:
            raise self.prepare_error

    async def submit(self, _):
        self.submit_calls += 1
        if self.submit_error:
            raise self.submit_error
        return self.order_id

    async def reconcile(self, _):
        return self.order_id
