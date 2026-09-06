import pytest

from ticket_runner.config import NotificationConfig
from ticket_runner.notify import Notifier
from ticket_runner.state import Store


async def test_failed_notification_is_retained_without_token_logging(
    config, tmp_path, monkeypatch, caplog
):
    store = Store(tmp_path)
    store.register(config)
    store.transition(config.task_id, "UNKNOWN", "请核对订单", notify=True)
    monkeypatch.setenv("TEST_WEBHOOK", "https://example.invalid/SECRET_TOKEN")
    notifier = Notifier(store, NotificationConfig(webhook_url_env="TEST_WEBHOOK"))

    def fail(*_):
        raise RuntimeError("https://example.invalid/SECRET_TOKEN")

    monkeypatch.setattr(notifier, "_post", fail)
    await notifier.flush()
    assert "SECRET_TOKEN" not in caplog.text
    row = store.db.execute("SELECT * FROM outbox").fetchone()
    assert row["attempts"] == 1 and row["delivered"] == 0
    store.close()


async def test_no_webhook_keeps_notification_for_later(config, tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_WEBHOOK", raising=False)
    store = Store(tmp_path)
    store.register(config)
    store.transition(config.task_id, "ORDER_CREATED", "请付款", notify=True)
    await Notifier(store, NotificationConfig(webhook_url_env="TEST_WEBHOOK")).flush()
    assert store.status()["pending_notifications"] == 1
    store.close()


@pytest.mark.parametrize("outcome,expected", [("no-order", "READY"), ("done", "DONE")])
def test_explicit_resolution_clears_uncertain_state(config, tmp_path, outcome, expected):
    store = Store(tmp_path)
    store.register(config)
    store.transition(config.task_id, "UNKNOWN", "请核对")
    store.resolve(config.task_id, outcome)
    assert store.get(config.task_id)["state"] == expected
    store.close()
