import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from ticket_runner.control import Controller, SessionAdapter, demo_config
from ticket_runner.domain import SHANGHAI, OrderReceipt, parse_catalog
from ticket_runner.state import Store
from ticket_runner.web import create_app

from .conftest import FakeAdapter


class WebAdapter(FakeAdapter):
    def __init__(self, config, _):
        super().__init__(None)
        self.config = config
        self.on_login_required = None
        self.logged_in = False
        self.submit_entered = asyncio.Event()
        self.submit_wait = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def ensure_login(self):
        self.login_calls += 1
        self.logged_in = True

    async def authenticated(self):
        return self.logged_in

    async def query_catalog(self, day):
        self.query_calls += 1
        return [
            {
                "train": "G846",
                "date": str(day),
                "seats": [{"name": "二等座", "price": "608.5", "status": "候补"}],
            }
        ]

    async def submit(self, offer):
        self.submit_calls += 1
        self.submit_entered.set()
        if self.submit_wait:
            await self.submit_wait.wait()
        return OrderReceipt(None, "764", 1, ("01车01A号",), datetime.now(SHANGHAI).isoformat())


@pytest.fixture
async def dashboard(config, tmp_path):
    config = demo_config(config)
    config.journey.dates = config.journey.dates[:1]
    config.preferences.trains = ["G698"]
    app = create_app(config, tmp_path, adapter_factory=WebAdapter)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
        ) as client:
            boot = (await client.get("/api/bootstrap")).json()
            client.headers["x-control-token"] = boot["token"]
            yield client, app.state.controller, boot


async def test_opening_dashboard_does_not_launch_browser_or_task(dashboard):
    client, controller, boot = dashboard
    assert controller.adapter is None and controller.work is None
    assert boot["config"]["execution"]["auto_submit"] is False
    assert not boot["snapshot"]["tasks"]
    assert (await client.get("/healthz")).json()["version"] == "0.2.0"
    page = await client.get("/")
    assert page.status_code == 200
    assert 'src="/monitor"' in page.text and 'title="嵌入式实时抢票流程"' in page.text
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    for asset in ("app.js", "styles.css", "monitor.js", "stations.json"):
        assert (await client.get("/assets/" + asset)).status_code == 200
    assert controller.adapter is None and controller.work is None


@pytest.mark.parametrize(
    "headers",
    [{"host": "evil.example"}, {"origin": "https://evil.example"}, {"x-control-token": "wrong"}],
)
async def test_cross_origin_host_and_csrf_are_rejected(dashboard, headers):
    client, controller, _ = dashboard
    response = await client.post("/api/login", json={}, headers=headers)
    assert response.status_code == 403
    assert controller.adapter is None


async def test_api_needs_local_cookie_and_rejects_non_json(dashboard):
    client, controller, _ = dashboard
    assert (await client.post("/api/login", content="{}")).status_code == 415
    client.cookies.clear()
    assert (await client.get("/api/status")).status_code == 403
    assert controller.adapter is None


async def test_large_or_invalid_payload_never_echoes_passengers(dashboard):
    client, controller, boot = dashboard
    assert (await client.post("/api/config", json={"large": "x" * 33000})).status_code == 413
    raw = boot["config"]
    raw["journey"]["passengers"] = ["SECRET-PASSENGER"]
    raw["preferences"]["max_total_price"] = -5
    response = await client.post("/api/config", json=raw)
    assert response.status_code == 422 and "SECRET-PASSENGER" not in response.text
    assert controller.work is None


async def test_save_is_private_and_never_enables_auto_submit(dashboard):
    client, controller, boot = dashboard
    config = boot["config"]
    config["preferences"]["trains"] = ["G846"]
    config["execution"]["auto_submit"] = True
    response = await client.post("/api/config", json=config)
    assert response.status_code == 200
    assert response.json()["config"]["execution"]["auto_submit"] is False
    assert (controller.data_dir / "ui-config.yaml").stat().st_mode & 0o777 == 0o600
    assert controller.adapter is None and not controller.store.status()["tasks"]


async def test_api_engine_is_explicit_and_never_enables_api_orders(dashboard):
    client, controller, boot = dashboard
    config = boot["config"]
    assert config["query_backend"] == "browser"
    config["query_backend"] = "api"
    saved = (await client.post("/api/config", json=config)).json()
    assert saved["config"]["query_backend"] == "api"
    assert saved["fingerprint"] != boot["fingerprint"]
    state = (await client.get("/api/status")).json()
    assert state["query_backend"] == "api" and state["order_backend"] == "browser"
    assert controller.work is None and controller.adapter is None
    assert (await client.post("/api/query", json={})).status_code == 202
    await controller.work
    assert controller.adapter.submit_calls == controller.adapter.prepare_calls == 0
    config["query_backend"] = "api-order"
    assert (await client.post("/api/config", json=config)).status_code == 422


async def test_switching_engine_never_clears_unknown_order(dashboard, offer):
    client, controller, boot = dashboard
    controller.store.register(controller.base)
    controller.store.transition(controller.base.task_id, "UNKNOWN", offer=offer)
    config = boot["config"]
    config["query_backend"] = "api"
    saved = (await client.post("/api/config", json=config)).json()
    assert controller.store.get(controller.base.task_id)["state"] == "UNKNOWN"
    assert (
        await client.post(
            "/api/start",
            json={"auto_submit": True, "confirmed": True, "fingerprint": saved["fingerprint"]},
        )
    ).status_code == 409
    assert controller.adapter is None


async def test_readonly_catalog_includes_sold_out_and_never_buys(dashboard):
    client, controller, _ = dashboard
    assert (await client.post("/api/query", json={})).status_code == 202
    await controller.work
    assert controller.catalog[0]["seats"][0]["status"] == "候补"
    assert controller.adapter.prepare_calls == controller.adapter.submit_calls == 0
    assert controller.store.status()["tasks"] == []
    assert controller.snapshot()["completed_steps"] == []
    assert not controller.logged_in


async def test_login_is_explicit_and_does_not_buy(dashboard):
    client, controller, _ = dashboard
    assert (await client.post("/api/login", json={})).status_code == 202
    await controller.work
    assert controller.logged_in
    assert controller.adapter.login_calls == 1
    assert controller.adapter.query_calls == controller.adapter.submit_calls == 0


async def test_real_submit_needs_fresh_fingerprint_and_explicit_consent(dashboard):
    client, controller, boot = dashboard
    response = await client.post(
        "/api/start", json={"auto_submit": True, "fingerprint": boot["fingerprint"]}
    )
    assert response.status_code == 409
    assert (
        await client.post(
            "/api/start",
            json={"auto_submit": "true", "confirmed": True, "fingerprint": boot["fingerprint"]},
        )
    ).status_code == 422
    assert (
        await client.post(
            "/api/start", json={"auto_submit": True, "confirmed": True, "fingerprint": "0" * 64}
        )
    ).status_code == 409
    assert controller.work is None and controller.adapter is None


async def test_unresolved_previous_order_blocks_new_web_task(dashboard, offer):
    client, controller, boot = dashboard
    controller.store.register(controller.base)
    controller.store.transition(controller.base.task_id, "UNKNOWN", offer=offer)
    response = await client.post(
        "/api/start",
        json={"auto_submit": True, "confirmed": True, "fingerprint": boot["fingerprint"]},
    )
    assert response.status_code == 409
    assert controller.adapter is None and controller.work is None
    assert (await client.get("/api/status")).json()["blocker"]["state"] == "UNKNOWN"


async def test_double_start_is_rejected_and_stop_before_submit_is_safe(dashboard):
    client, controller, boot = dashboard
    payload = {"auto_submit": False, "fingerprint": boot["fingerprint"]}
    assert (await client.post("/api/start", json=payload)).status_code == 202
    assert (await client.post("/api/start", json=payload)).status_code == 409
    assert (await client.post("/api/query", json={})).status_code == 409
    assert (await client.post("/api/config", json=boot["config"])).status_code == 409
    assert (await client.post("/api/stop", json={})).status_code == 200
    assert controller.store.get(controller.active_task)["state"] == "PAUSED"
    assert controller.adapter.submit_calls == 0
    assert controller.busy is None


async def test_stop_during_submit_preserves_unknown_and_no_retries(dashboard, offer):
    client, controller, boot = dashboard
    adapter = await controller.browser(controller.draft)
    adapter.offer = replace(offer, travel_date=controller.draft.journey.dates[0])
    adapter.submit_wait = asyncio.Event()
    assert (
        await client.post(
            "/api/start",
            json={"auto_submit": True, "confirmed": True, "fingerprint": boot["fingerprint"]},
        )
    ).status_code == 202
    await asyncio.wait_for(adapter.submit_entered.wait(), timeout=2)
    assert (await client.post("/api/stop", json={})).status_code == 200
    assert controller.store.get(controller.active_task)["state"] == "UNKNOWN"
    assert (
        await client.post("/api/start", json={"fingerprint": boot["fingerprint"]})
    ).status_code == 409
    assert adapter.submit_calls == 1


async def test_readonly_mode_never_prepares_an_available_ticket(dashboard, offer):
    client, controller, boot = dashboard
    adapter = await controller.browser(controller.draft)
    adapter.offer = replace(offer, travel_date=controller.draft.journey.dates[0])
    assert (
        await client.post("/api/start", json={"fingerprint": boot["fingerprint"]})
    ).status_code == 202
    await controller.work
    assert controller.store.get(controller.active_task)["state"] == "DRY_RUN"
    assert adapter.prepare_calls == adapter.submit_calls == 0
    assert any(e["stage"] == "MATCHED" for e in controller.snapshot()["events"])
    assert controller.snapshot()["completed_steps"] == [0, 1]


async def test_web_submit_records_one_verified_receipt_and_stops(dashboard, offer):
    client, controller, boot = dashboard
    adapter = await controller.browser(controller.draft)
    adapter.offer = replace(offer, travel_date=controller.draft.journey.dates[0])
    payload = {"auto_submit": True, "confirmed": True, "fingerprint": boot["fingerprint"]}
    assert (await client.post("/api/start", json=payload)).status_code == 202
    await controller.work
    state = controller.snapshot()
    assert state["phase"] == "ORDER_CREATED" and state["busy"] is None
    assert state["completed_steps"] == [0, 1, 2, 3, 4]
    assert state["tasks"][0]["receipt"]["order_id"] is None
    assert adapter.prepare_calls == adapter.submit_calls == 1
    assert (await client.post("/api/start", json=payload)).status_code == 409
    assert adapter.submit_calls == 1


async def test_flow_evidence_and_events_survive_correctly(dashboard, offer):
    _, controller, _ = dashboard
    controller.publish("MATCHED", "模拟匹配事件", offer=offer.to_dict())
    assert controller.snapshot()["completed_steps"] == [1]
    controller.save(controller.draft)
    assert controller.snapshot()["completed_steps"] == []
    second = Store(controller.data_dir)
    try:
        assert [e["stage"] for e in second.recent_events()] == ["MATCHED", "CONFIG_SAVED"]
    finally:
        second.close()


async def test_resolve_requires_manual_confirmation_and_never_restarts(dashboard, offer):
    client, controller, _ = dashboard
    controller.store.register(controller.base)
    controller.store.transition(controller.base.task_id, "UNKNOWN", offer=offer)
    payload = {"task_id": controller.base.task_id, "outcome": "done", "confirmation": ""}
    assert (await client.post("/api/resolve", json=payload)).status_code == 409
    payload["confirmation"] = "已核对官方订单"
    assert (await client.post("/api/resolve", json=payload)).status_code == 200
    assert controller.store.get(controller.base.task_id)["state"] == "DONE"
    assert controller.work is None and controller.adapter is None


async def test_restart_keeps_unknown_and_does_not_resume(config, tmp_path, offer):
    store = Store(tmp_path)
    store.register(config)
    store.transition(config.task_id, "UNKNOWN", offer=offer)
    store.close()
    controller = Controller(config, tmp_path, adapter_factory=WebAdapter)
    await controller.open()
    try:
        assert controller.snapshot()["blocker"]["state"] == "UNKNOWN"
        assert controller.work is None and controller.adapter is None
    finally:
        await controller.close()


async def test_demo_never_uses_real_database_or_passengers(config, tmp_path, offer):
    store = Store(tmp_path)
    store.register(config)
    store.transition(config.task_id, "UNKNOWN", offer=offer)
    original = store.status()
    store.close()
    controller = Controller(config, tmp_path, demo=True)
    await controller.open()
    try:
        assert controller.data_dir == tmp_path / "ui-demo"
        assert controller.draft.journey.passengers == ["演示乘车人"]
        assert controller.snapshot()["blocker"] is None
        assert controller.adapter is None
    finally:
        await controller.close()
    store = Store(tmp_path)
    assert store.status() == original
    store.close()


async def test_query_pacing_shared_between_catalog_and_runner(dashboard):
    _, controller, _ = dashboard
    controller.last_query_started = 10
    controller.monotonic = lambda: 12
    controller.sleep = AsyncMock()
    adapter = await controller.browser(controller.draft)
    wrapped = SessionAdapter(controller, adapter)
    await wrapped.query(controller.draft.journey.dates[0])
    controller.sleep.assert_awaited_once_with(3)
    assert controller.last_query_started == 12


def test_catalog_sold_out_selectable_but_not_purchase_offer(config):
    rows = [
        {
            "train": "G698",
            "origin": "深圳北",
            "destination": "南京南",
            "departure": "08:35",
            "arrival": "17:00",
            "bookable": False,
            "seats": ["G698次列车，二等座票价764元，余票候补"],
        }
    ]
    catalog = parse_catalog(rows, config.journey.dates[0], config)
    assert len(catalog) == 1 and catalog[0]["bookable"] is False
    assert catalog[0]["seats"][0]["status"] == "候补"
    rows[0]["origin"] = "深圳"
    assert parse_catalog(rows, config.journey.dates[0], config) == []
