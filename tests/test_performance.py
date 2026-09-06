from datetime import timedelta

import pytest

from ticket_runner.domain import QueryFailed
from ticket_runner.runner import Runner
from ticket_runner.state import Store

from .conftest import FakeAdapter, FakeClock, FakeNotifier


@pytest.mark.parametrize("query_seconds,expected", [(2, [0, 5, 10]), (7, [0, 7, 14])])
async def test_cadence_counts_query_time_and_never_catches_up(
    config, now, tmp_path, query_seconds, expected
):
    clock = FakeClock(now)
    config.execution.stop_at = now + timedelta(seconds=15)
    config.journey.dates.append(config.journey.dates[0] + timedelta(days=1))
    adapter = FakeAdapter(None)
    starts, dates = [], []

    async def query(travel_date):
        starts.append((clock() - now).total_seconds())
        dates.append(travel_date)
        await clock.sleep(query_seconds)
        return []

    adapter.query = query
    store = Store(tmp_path)
    try:
        await Runner(config, store, adapter, FakeNotifier(), clock=clock, sleep=clock.sleep).run()
        assert starts == expected
        assert dates[:2] == config.journey.dates
        assert store.timing_report()["stages"]["query"]["mean_seconds"] == query_seconds
    finally:
        store.close()


async def test_no_wait_between_candidate_and_submission(config, offer, now, tmp_path):
    clock = FakeClock(now)
    adapter = FakeAdapter(offer)
    store = Store(tmp_path)
    try:
        await Runner(config, store, adapter, FakeNotifier(), clock=clock, sleep=clock.sleep).run()
        assert clock() == now
        assert adapter.submit_calls == 1
        assert store.timing_report()["stages"]["candidate_to_result"]["outcomes"] == {
            "confirmed": 1
        }
    finally:
        store.close()


async def test_backoff_remains_after_acceleration(config, now, tmp_path):
    clock, starts = FakeClock(now), []
    adapter = FakeAdapter(None)

    async def query(_):
        starts.append(clock())
        raise QueryFailed("官网提示访问频繁")

    adapter.query = query
    store = Store(tmp_path)
    try:
        await Runner(config, store, adapter, FakeNotifier(), clock=clock, sleep=clock.sleep).run()
        assert (starts[1] - starts[0]).total_seconds() >= 5
        assert store.get(config.task_id)["state"] == "ATTENTION"
        assert store.timing_report()["stages"]["query"]["outcomes"] == {"error": 2}
    finally:
        store.close()


async def test_wait_does_not_block_on_network_notifications(config, now, tmp_path):
    clock = FakeClock(now)
    store = Store(tmp_path)

    class SlowNotifier:
        async def flush(self):
            raise AssertionError("network delivery must not run on polling wait path")

    try:
        runner = Runner(
            config, store, FakeAdapter(None), SlowNotifier(), clock=clock, sleep=clock.sleep
        )
        await runner.pause(5)
        assert (clock() - now).total_seconds() == 5
    finally:
        store.close()
