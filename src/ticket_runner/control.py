"""Single-owner web control plane. Opening a page never starts a booking task."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import suppress
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import yaml

from .browser import BrowserAdapter
from .config import Config, load_config
from .domain import SHANGHAI, NeedsAttention, Offer, OrderReceipt
from .notify import Notifier
from .runner import Runner
from .state import BLOCKING_STATES, Store, exclusive_run


def write_config(path: Path, config: Config):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".ui-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            yaml.safe_dump(
                config.model_dump(mode="json"), stream, allow_unicode=True, sort_keys=False
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def demo_config(base: Config) -> Config:
    raw = base.model_dump(mode="json")
    now = datetime.now(SHANGHAI)
    raw["task_id"] = "demo-draft"
    raw["journey"]["passengers"] = ["演示乘车人"]
    raw["journey"]["dates"] = [str((now + timedelta(days=n)).date()) for n in (3, 4)]
    raw["execution"].update(
        start_at=now.isoformat(),
        stop_at=(now + timedelta(hours=2)).isoformat(),
        auto_submit=False,
        query_interval_seconds=5,
    )
    return Config.model_validate(raw)


class DemoAdapter:
    """Explicit synthetic mode: no browser, account, official network or payment."""

    def __init__(self, config: Config, *_):
        self.config = config
        self.on_login_required = None
        self.logged_in = False
        self.query_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def ensure_login(self):
        await asyncio.sleep(0.25)
        self.logged_in = True

    async def authenticated(self):
        return self.logged_in

    async def check_existing_orders(self):
        await asyncio.sleep(0.2)

    def offer(self, day):
        cfg = self.config
        return Offer(
            "ticket_demo_01_02",
            (cfg.preferences.trains or ["G846"])[0],
            day,
            cfg.journey.origin.name,
            cfg.journey.destination.name,
            datetime.strptime("09:20", "%H:%M").time(),
            cfg.preferences.seats[0],
            Decimal("608.5"),
            5,
        )

    async def query_catalog(self, day):
        await asyncio.sleep(0.25)
        result = []
        for train, departure, arrival, count in (
            ("G846", "09:20", "15:03", "候补"),
            ("G80", "11:05", "16:48", "3"),
            ("G822", "14:12", "20:09", "有"),
        ):
            if (
                not self.config.preferences.departure_after.strftime("%H:%M")
                <= departure
                <= self.config.preferences.departure_before.strftime("%H:%M")
            ):
                continue
            result.append(
                {
                    "train": train,
                    "date": str(day),
                    "origin": self.config.journey.origin.name,
                    "destination": self.config.journey.destination.name,
                    "departure": departure,
                    "arrival": arrival,
                    "bookable": count != "候补",
                    "seats": [
                        {"name": "二等座", "price": "608.5", "status": count},
                        {"name": "一等座", "price": "954.5", "status": "候补"},
                    ],
                }
            )
        return result

    async def query(self, day):
        await asyncio.sleep(0.3)
        self.query_count += 1
        return [] if self.query_count < 3 else [self.offer(day)]

    async def prepare(self, _):
        await asyncio.sleep(0.6)

    async def submit(self, offer):
        await asyncio.sleep(0.6)
        count = len(self.config.journey.passengers)
        return OrderReceipt(
            None,
            str(offer.price * count),
            count,
            tuple(f"演示席位 {n + 1}" for n in range(count)),
            datetime.now(SHANGHAI).isoformat(),
            "synthetic_demo_not_a_real_order",
        )

    async def reconcile(self, offer):
        return await self.submit(offer)


class SilentNotifier:
    async def flush(self):
        pass


class SessionAdapter:
    """Keep account-wide query pacing across manual queries and scheduled runs."""

    def __init__(self, controller, adapter):
        self.controller, self.adapter = controller, adapter

    async def ensure_login(self):
        await self.adapter.ensure_login()
        self.controller.logged_in = True
        self.controller.publish(
            "LOGIN_READY",
            "已确认登录状态" if not self.controller.demo else "模拟登录完成，未访问12306",
        )

    async def query(self, day):
        await self.controller.throttle(self.adapter.config.execution.query_interval_seconds)
        return await self.adapter.query(day)

    async def prepare(self, offer):
        result = await self.adapter.prepare(offer)
        self.controller.publish("PREPARED", "乘车人、席别与预算已核对，尚未提交")
        return result

    async def submit(self, offer):
        return await self.adapter.submit(offer)

    async def reconcile(self, offer):
        return await self.adapter.reconcile(offer)

    async def check_existing_orders(self):
        result = await self.adapter.check_existing_orders()
        self.controller.publish("ACCOUNT_READY", "登录及未完成订单检查通过")
        return result


class Controller:
    def __init__(self, base: Config, data_dir: Path, *, demo=False, adapter_factory=None):
        self.demo = demo
        # Demo cannot open or mutate the real task database, browser profile or config.
        self.data_dir = data_dir / "ui-demo" if demo else data_dir
        self.base = demo_config(base) if demo else base.model_copy(deep=True)
        draft_path = self.data_dir / "ui-config.yaml"
        self.draft = (
            load_config(draft_path) if draft_path.exists() else self.base.model_copy(deep=True)
        )
        self.draft.execution.auto_submit = False
        self.factory = adapter_factory or (DemoAdapter if demo else BrowserAdapter)
        self.adapter = None
        self.store = None
        self.lock = None
        self.work = None
        self.runner = None
        self.active_task = None
        self.busy = None
        self.phase = "IDLE"
        self.message = "控制台就绪，尚未启动抢票"
        self.logged_in = False
        self.catalog = []
        self.catalog_query = None
        self.version = 0
        self.query_count = 0
        self.last_query_started = None
        self.monotonic = time.monotonic
        self.sleep = asyncio.sleep
        self.last_cycle_at = None
        self.last_error = None
        self.completed_steps = set()

    async def open(self):
        self.lock = exclusive_run(self.data_dir)
        self.lock.__enter__()
        try:
            self.store = Store(self.data_dir)
            self.store.on_event = self.on_event
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise
        # No browser/network/login/run here, including after a process restart.

    async def close(self):
        try:
            await self.stop()
            if self.adapter:
                await self.adapter.__aexit__(None, None, None)
        finally:
            if self.store:
                self.store.close()
            if self.lock:
                self.lock.__exit__(None, None, None)

    def on_event(self, stage, message, details):
        self.phase, self.message = stage, message
        # Completion is evidence-based, never inferred from a later phase number.
        proven = {"ACCOUNT_READY": (0,), "MATCHED": (1,), "PREPARED": (2,), "ORDER_CREATED": (3, 4)}
        self.completed_steps.update(proven.get(stage, ()))
        if stage == "CONFIG_SAVED":
            self.completed_steps.clear()
        if stage == "QUERY":
            self.query_count += 1
            self.last_cycle_at = time.time()
        if stage in {"ATTENTION", "UNKNOWN"}:
            self.last_error = message
        self.version += 1

    def publish(self, stage, message, **details):
        self.store.event(self.active_task or "control", stage, message, **details)

    def require_idle(self):
        if self.busy:
            raise NeedsAttention("已有操作进行中，请先暂停；不能同时控制同一个账号浏览器")

    def validate_journey_dates(self, config):
        today = datetime.now(SHANGHAI).date()
        if any(day < today or day > today + timedelta(days=14) for day in config.journey.dates):
            raise NeedsAttention("请选择今天起 15 天内的乘车日期；未开售日期请等进入预售期再查询")

    def save(self, config: Config):
        self.require_idle()
        config = config.model_copy(deep=True)
        config.execution.auto_submit = False
        if (
            config.journey.model_dump() != self.draft.journey.model_dump()
            or config.query_backend != self.draft.query_backend
            or config.preferences.departure_after != self.draft.preferences.departure_after
            or config.preferences.departure_before != self.draft.preferences.departure_before
        ):
            self.catalog, self.catalog_query = [], None
        write_config(self.data_dir / "ui-config.yaml", config)
        self.draft = config
        self.publish("CONFIG_SAVED", "已保存选择，尚未启动抢票")

    async def browser(self, config):
        if self.adapter is None:
            candidate = self.factory(config.model_copy(deep=True), self.data_dir)
            self.adapter = await candidate.__aenter__()
        self.adapter.config = config.model_copy(deep=True)

        async def login_needed():
            self.logged_in = False
            self.publish("LOGIN_REQUIRED", "请在官方浏览器小窗扫码；额外核验由你手动完成")

        self.adapter.on_login_required = login_needed
        self.adapter.on_progress = self.publish
        return self.adapter

    async def throttle(self, interval):
        if self.last_query_started is not None:
            await self.sleep(max(0, interval - (self.monotonic() - self.last_query_started)))
        self.last_query_started = self.monotonic()

    def launch(self, kind, operation):
        self.require_idle()
        self.completed_steps.clear()
        self.busy = kind
        self.last_error = None
        self.work = asyncio.create_task(self._operate(kind, operation))
        self.version += 1

    async def _operate(self, kind, operation):
        try:
            await operation()
        except asyncio.CancelledError:
            existing = self.store.get(self.active_task) if self.active_task else None
            if existing and existing["state"] in {"SUBMITTING", "UNKNOWN"}:
                self.store.transition(
                    self.active_task,
                    "UNKNOWN",
                    "提交阶段已暂停，结果未确认；禁止再次自动下单",
                    notify=True,
                )
            elif existing and existing["state"] == "ORDER_CREATED":
                self.publish("ORDER_CREATED", "已确认订单；暂停不会取消订单，也不会付款")
            else:
                if kind == "run" and existing:
                    self.store.transition(self.active_task, "PAUSED", "已暂停查询；不会继续下单")
                else:
                    self.publish("PAUSED", "当前操作已暂停，不会自动重新开始")
        except Exception as exc:
            self.last_error = Runner.safe_error(exc)
            self.publish("ATTENTION", self.last_error)
        finally:
            self.busy = None
            self.runner = None
            self.version += 1

    def login(self):
        async def operation():
            self.publish("LOGIN", "正在检查官方登录状态")
            adapter = await self.browser(self.draft)
            await adapter.ensure_login()
            self.logged_in = True
            self.publish(
                "LOGIN_READY",
                "已确认官方登录状态" if not self.demo else "演示登录完成（未访问12306）",
            )

        self.launch("login", operation)

    def query(self):
        self.validate_journey_dates(self.draft)
        config = self.draft.model_copy(deep=True)

        async def operation():
            self.catalog = []
            self.catalog_query = {
                "origin": config.journey.origin.name,
                "destination": config.journey.destination.name,
                "dates": [str(day) for day in config.journey.dates],
            }
            adapter = await self.browser(config)
            for day in config.journey.dates:
                await self.throttle(config.execution.query_interval_seconds)
                self.publish("QUERY", "只读查询车次；不预订、不下单", date=str(day))
                self.catalog.extend(await adapter.query_catalog(day))
                self.version += 1
            self.publish(
                "CATALOG_READY",
                f"查询完成，找到 {len(self.catalog)} 个车次日期组合；无票车次也可加入监控",
            )

        self.launch("query", operation)

    def start(self, *, auto_submit: bool, confirmed: bool, fingerprint: str):
        self.require_idle()
        if fingerprint != self.draft.fingerprint():
            raise NeedsAttention("配置已变化，请重新保存并核对后启动")
        if auto_submit and not confirmed:
            raise NeedsAttention("真实提交需要明确确认行程、人数、席别和总预算；不会自动付款")
        blocker = self.store.blocker()
        if blocker:
            raise NeedsAttention(
                f"存在未处理的 {blocker['state']} 记录；请先核对官方订单，不能重复下单"
            )
        self.validate_journey_dates(self.draft)
        config = self.draft.model_copy(deep=True)
        if config.execution.stop_at <= datetime.now(SHANGHAI):
            raise NeedsAttention("截止时间已过，请修改后保存")
        config.task_id = (
            "web-" + datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8]
        )
        config.execution.auto_submit = auto_submit
        write_config(self.data_dir / "tasks" / f"{config.task_id}.yaml", config)
        self.store.register(config)
        self.active_task = config.task_id
        self.query_count = 0
        self.last_cycle_at = None

        async def operation():
            self.publish(
                "STARTING",
                "正在启动任务；仅提交待支付订单，不付款"
                if auto_submit
                else "开始只读监控；发现匹配票即停止，不点击预订",
            )
            adapter = await self.browser(config)
            if isinstance(adapter, DemoAdapter):
                adapter.query_count = 0
            notifier = SilentNotifier() if self.demo else Notifier(self.store, config.notification)
            self.runner = Runner(config, self.store, SessionAdapter(self, adapter), notifier)

            async def notify_loop():
                while True:
                    await notifier.flush()
                    await asyncio.sleep(1)

            delivery = asyncio.create_task(notify_loop())
            try:
                await self.runner.run()
            finally:
                delivery.cancel()
                with suppress(asyncio.CancelledError):
                    await delivery
            # A session-status refresh must not overwrite a confirmed order result.
            with suppress(Exception):
                self.logged_in = await adapter.authenticated()

        self.launch("run", operation)

    async def stop(self):
        if self.work and not self.work.done():
            if self.runner:
                self.runner.stop.set()
            self.work.cancel()
            with suppress(asyncio.CancelledError):
                await self.work

    def resolve(self, task_id: str, outcome: str, confirmation: str):
        self.require_idle()
        if confirmation != "已核对官方订单":
            raise NeedsAttention("请先在12306核对订单，并输入确认文字")
        self.store.resolve(task_id, outcome)
        self.publish("RESOLVED", "已记录人工核对结果；不会自动恢复抢票")

    def reconcile(self, task_id: str):
        self.require_idle()
        existing = self.store.get(task_id)
        if not existing or existing["state"] not in BLOCKING_STATES or not existing["offer"]:
            raise NeedsAttention("没有可回查的提交记录")
        path = self.data_dir / "tasks" / f"{task_id}.yaml"
        if path.exists():
            config = load_config(path)
        elif task_id == self.base.task_id:
            config = self.base
        else:
            raise NeedsAttention("缺少该任务的原始配置，请使用原配置执行 check-order")
        self.store.register(config)
        offer = Offer.from_dict(json.loads(existing["offer"]))
        self.active_task = task_id

        async def operation():
            self.publish("RECONCILE", "只回查既有订单，不提交、不取消、不付款")
            adapter = await self.browser(config)
            await adapter.ensure_login()
            self.logged_in = True
            receipt = await adapter.reconcile(offer)
            if receipt:
                self.store.transition(
                    task_id,
                    "ORDER_CREATED",
                    "已逐项核对官方待支付订单；请在官方 App 查看",
                    receipt=receipt,
                    order_id=receipt.order_id,
                    notify=existing["state"] != "ORDER_CREATED",
                )
            else:
                self.store.transition(
                    task_id,
                    "UNKNOWN",
                    "未确认到匹配的待支付订单；禁止自动重下，请人工核对",
                    notify=True,
                )

        self.launch("reconcile", operation)

    def snapshot(self):
        state = self.store.status()
        blocker = self.store.blocker()
        return {
            "version": self.version,
            "server_time": datetime.now(SHANGHAI).isoformat(),
            "mode": "demo" if self.demo else "browser",
            "query_backend": self.draft.query_backend,
            "order_backend": "browser",
            "busy": self.busy,
            "phase": self.phase,
            "completed_steps": sorted(self.completed_steps),
            "message": self.message,
            "logged_in": self.logged_in,
            "active_task": self.active_task,
            "query_count": self.query_count,
            "last_cycle_at": self.last_cycle_at,
            "interval": self.draft.execution.query_interval_seconds,
            "last_error": self.last_error,
            "blocker": {"task_id": blocker["task_id"], "state": blocker["state"]}
            if blocker
            else None,
            "catalog": self.catalog,
            "catalog_query": self.catalog_query,
            "events": self.store.recent_events(),
            "tasks": state["tasks"],
            "pending_notifications": state["pending_notifications"],
            "notification_configured": bool(os.environ.get(self.draft.notification.webhook_url_env))
            and not self.demo,
        }
