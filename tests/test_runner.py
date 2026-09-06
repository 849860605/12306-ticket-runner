import pytest

from ticket_runner.domain import NeedsAttention, QueryFailed
from ticket_runner.runner import Runner
from ticket_runner.state import Store, exclusive_run

from .conftest import FakeAdapter, FakeClock, FakeNotifier


def make_runner(config, store, adapter, now):
    clock = FakeClock(now)
    return Runner(config, store, adapter, FakeNotifier(), clock=clock, sleep=clock.sleep)


async def test_timeout_restart_reconciles_without_resubmission(config, offer, now, tmp_path):
    adapter = FakeAdapter(offer)
    adapter.submit_error = TimeoutError("response lost")
    store = Store(tmp_path)
    await make_runner(config, store, adapter, now).run()
    assert store.get(config.task_id)["state"] == "UNKNOWN"
    store.close()
    # A real SQLite reopen proves the intent survives process recreation.
    store = Store(tmp_path)
    await make_runner(config, store, adapter, now).run()
    assert store.get(config.task_id)["state"] == "ORDER_CREATED"
    await make_runner(config, store, adapter, now).run()
    assert adapter.submit_calls == 1
    assert store.status()["pending_notifications"] == 2
    store.close()


async def test_missing_order_after_timeout_stays_unknown(config, offer, now, tmp_path):
    adapter, store = FakeAdapter(offer), Store(tmp_path)
    adapter.order_id = None
    await make_runner(config, store, adapter, now).run()
    await make_runner(config, store, adapter, now).run()
    assert store.get(config.task_id)["state"] == "UNKNOWN"
    assert adapter.submit_calls == 1
    store.close()


async def test_dry_run_never_clicks_booking(config, offer, now, tmp_path):
    config.execution.auto_submit = False
    adapter, store = FakeAdapter(offer), Store(tmp_path)
    await make_runner(config, store, adapter, now).run()
    assert store.get(config.task_id)["state"] == "DRY_RUN"
    assert adapter.prepare_calls == adapter.submit_calls == 0
    store.close()


async def test_persisted_intent_exists_before_submit(config, offer, now, tmp_path):
    adapter, store = FakeAdapter(offer), Store(tmp_path)

    async def submit(_):
        assert store.get(config.task_id)["state"] == "SUBMITTING"
        second = Store(tmp_path)
        try:
            assert second.get(config.task_id)["offer"]
        finally:
            second.close()
        return "E123456789"

    adapter.submit = submit
    await make_runner(config, store, adapter, now).run()
    assert store.get(config.task_id)["state"] == "ORDER_CREATED"
    store.close()


async def test_query_errors_are_bounded_not_no_inventory(config, offer, now, tmp_path):
    adapter, store = FakeAdapter(offer), Store(tmp_path)
    adapter.query_error = QueryFailed("网络失败")
    await make_runner(config, store, adapter, now).run()
    assert store.get(config.task_id)["state"] == "ATTENTION"
    assert adapter.query_calls == 2 and adapter.submit_calls == 0
    store.close()


async def test_review_mismatch_stops(config, offer, now, tmp_path):
    adapter, store = FakeAdapter(offer), Store(tmp_path)
    adapter.prepare_error = NeedsAttention("乘车人不匹配")
    await make_runner(config, store, adapter, now).run()
    assert store.get(config.task_id)["state"] == "ATTENTION"
    assert adapter.submit_calls == 0
    store.close()


async def test_expired_task_never_logs_in(config, offer, now, tmp_path):
    adapter, store = FakeAdapter(offer), Store(tmp_path)
    await make_runner(config, store, adapter, config.execution.stop_at).run()
    assert store.get(config.task_id)["state"] == "EXPIRED"
    assert adapter.login_calls == 0
    store.close()


async def test_new_task_cannot_bypass_unresolved_order(config, offer, now, tmp_path):
    adapter, store = FakeAdapter(offer), Store(tmp_path)
    adapter.order_id = None
    await make_runner(config, store, adapter, now).run()
    config.task_id = "another-task"
    with pytest.raises(NeedsAttention):
        await make_runner(config, store, adapter, now).run()
    assert adapter.submit_calls == 1
    store.close()


def test_configuration_cannot_mutate_existing_task(config, tmp_path):
    store = Store(tmp_path)
    store.register(config)
    config.journey.passengers = ["另一个人"]
    with pytest.raises(NeedsAttention):
        store.register(config)
    store.close()


def test_single_instance_lock(tmp_path):
    with exclusive_run(tmp_path), pytest.raises(NeedsAttention), exclusive_run(tmp_path):
        pass
