import json

import pytest

from ticket_runner.browser import BrowserAdapter
from ticket_runner.runner import Runner
from ticket_runner.state import Store

from .conftest import FakeAdapter, FakeClock, FakeNotifier


def card_for(offer):
    def cell(text, leaves=None):
        return {"text": text, "segments": leaves or [text]}

    return {
        "rows": [
            [
                cell(
                    f"{offer.origin}{offer.destination} {offer.train}\n{offer.travel_date}   08:35 开",
                    [
                        offer.origin,
                        offer.destination,
                        offer.train,
                        str(offer.travel_date),
                        "08:35 开",
                    ],
                ),
                cell("张三\n居民身份证"),
                cell("二等座\n13车08C号"),
                cell("成人票\n764元 8.6折"),
                cell("待支付"),
            ]
        ]
    }


def test_pending_receipt_without_visible_order_number(config, offer, tmp_path):
    receipt = BrowserAdapter(config, tmp_path).match_order_card(card_for(offer), offer)
    assert receipt and receipt.order_id is None
    assert receipt.total_price == "764"
    assert receipt.assigned_seats == ("13车08C号",)
    assert "张三" not in json.dumps(receipt.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["rows"][0][1].update(text="张三丰\n居民身份证"),
        lambda c: c["rows"][0][2].update(text="无座\n13车08C号"),
        lambda c: c["rows"][0][2].update(text="二等座\n尚未分配"),
        lambda c: c["rows"][0][3].update(text="儿童票\n764元"),
        lambda c: c["rows"][0][3].update(text="成人票\n1001元"),
        lambda c: c["rows"][0][4].update(text="已取消"),
        lambda c: c["rows"][0][0].update(text="深圳北南京南 G698\n2026-10-01 07:35 开"),
        lambda c: c["rows"][0][0].update(
            segments=["南京南", "深圳北", "G698", "2026-10-01", "08:35 开"]
        ),
        lambda c: c["rows"].append(c["rows"][0]),
    ],
)
def test_inexact_receipt_never_confirms(config, offer, tmp_path, mutation):
    card = card_for(offer)
    mutation(card)
    assert BrowserAdapter(config, tmp_path).match_order_card(card, offer) is None


async def test_recovered_receipt_persists_without_fabricated_id(config, offer, now, tmp_path):
    adapter = FakeAdapter(offer)
    adapter.order_id = BrowserAdapter(config, tmp_path).match_order_card(card_for(offer), offer)
    store = Store(tmp_path)
    store.register(config)
    store.transition(config.task_id, "UNKNOWN", offer=offer)
    clock = FakeClock(now)
    await Runner(config, store, adapter, FakeNotifier(), clock=clock, sleep=clock.sleep).run()
    store.close()
    store = Store(tmp_path)
    try:
        assert store.get(config.task_id)["state"] == "ORDER_CREATED"
        assert store.get(config.task_id)["order_id"] is None
        assert store.status()["tasks"][0]["receipt"]["total_price"] == "764"
        await Runner(config, store, adapter, FakeNotifier(), clock=clock, sleep=clock.sleep).run()
        assert adapter.submit_calls == adapter.query_calls == 0
    finally:
        store.close()
