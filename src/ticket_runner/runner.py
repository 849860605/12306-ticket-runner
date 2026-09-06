from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Protocol

from .config import Config
from .domain import SHANGHAI, NeedsAttention, Offer, OrderReceipt, QueryFailed, rank_offers
from .notify import Notifier
from .state import TERMINAL_STATES, Store

log = logging.getLogger(__name__)


class Adapter(Protocol):
    async def ensure_login(self) -> None: ...
    async def query(self, travel_date) -> list[Offer]: ...
    async def prepare(self, offer: Offer) -> None: ...
    async def submit(self, offer: Offer) -> OrderReceipt | str | None: ...
    async def reconcile(self, offer: Offer) -> OrderReceipt | str | None: ...
    async def check_existing_orders(self) -> None: ...


class Runner:
    def __init__(
        self,
        config: Config,
        store: Store,
        adapter: Adapter,
        notifier: Notifier,
        *,
        clock=None,
        sleep=None,
        monotonic=None,
    ):
        self.config, self.store, self.adapter, self.notifier = config, store, adapter, notifier
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.sleep = sleep or asyncio.sleep
        self.monotonic = monotonic or (
            time.monotonic if clock is None else lambda: clock().timestamp()
        )
        self.stop = asyncio.Event()

    async def pause(self, seconds: float):
        end = self.clock() + timedelta(seconds=max(0, seconds))
        while not self.stop.is_set() and self.clock() < end:
            await self.sleep(min(1, max(0, (end - self.clock()).total_seconds())))

    async def measured(self, stage, operation, *args):
        started = self.monotonic()
        outcome = "error"
        try:
            result = await operation(*args)
            outcome = "ok"
            return result
        finally:
            elapsed = max(0, self.monotonic() - started)
            self.store.record_timing(self.config.task_id, stage, elapsed, outcome)
            log.info("阶段耗时：%s %.3f 秒 %s", stage, elapsed, outcome)

    async def run(self):
        cfg = self.config
        self.store.register(cfg)
        existing = self.store.get(cfg.task_id)
        if existing["state"] in {"SUBMITTING", "UNKNOWN"}:
            # Preserve uncertainty even when an empty order list is returned just after a crash.
            try:
                if not existing["offer"]:
                    raise NeedsAttention("缺少待核对订单的行程记录")
                import json

                offer = Offer.from_dict(json.loads(existing["offer"]))
                await self.measured("login", self.adapter.ensure_login)
                order_id = await self.measured("reconcile", self.adapter.reconcile, offer)
                if order_id:
                    self.store.transition(
                        cfg.task_id,
                        "ORDER_CREATED",
                        "已核对到订单，请在官方 App 查看并付款",
                        **self.receipt_fields(order_id),
                        notify=True,
                    )
                else:
                    self.store.transition(
                        cfg.task_id,
                        "UNKNOWN",
                        "提交结果仍不确定，请核对官方订单后执行 resolve",
                        notify=True,
                    )
            except Exception:
                self.store.transition(
                    cfg.task_id,
                    "UNKNOWN",
                    "无法确认上次提交结果，请核对官方订单后执行 resolve",
                    notify=True,
                )
            await self.notifier.flush()
            return
        if existing["state"] in TERMINAL_STATES:
            log.info("任务已停止：%s", existing["state"])
            await self.notifier.flush()
            return
        if self.store.blocker(excluding=cfg.task_id):
            raise NeedsAttention("此数据目录有尚未处理的订单/提交记录，请先核对并 resolve")
        if self.clock() >= cfg.execution.stop_at:
            self.store.transition(cfg.task_id, "EXPIRED", "已超过任务截止时间", notify=True)
            await self.notifier.flush()
            return
        prepare_at = cfg.execution.start_at - timedelta(seconds=cfg.execution.prepare_seconds)
        self.store.transition(cfg.task_id, "WAITING", "等待执行时间")
        await self.pause((prepare_at - self.clock()).total_seconds())
        if self.stop.is_set():
            return
        try:
            await self.measured("login", self.adapter.ensure_login)
            await self.measured("account_check", self.adapter.check_existing_orders)
        except Exception as exc:
            self.store.transition(cfg.task_id, "ATTENTION", self.safe_error(exc), notify=True)
            await self.notifier.flush()
            return
        await self.pause((cfg.execution.start_at - self.clock()).total_seconds())
        errors = 0
        last_query_started = None
        while not self.stop.is_set() and self.clock() < cfg.execution.stop_at:
            try:
                self.store.transition(cfg.task_id, "QUERYING", "查询中")
                # Dates are ordered by preference. Each query leaves its own results in the browser.
                for travel_date in cfg.journey.dates:
                    if self.clock() >= cfg.execution.stop_at or self.stop.is_set():
                        break
                    if travel_date < self.clock().astimezone(SHANGHAI).date():
                        continue
                    # One global, start-to-start cadence across ALL dates. Slow queries
                    # never cause catch-up bursts; requests are always serial.
                    if last_query_started is not None:
                        remaining = cfg.execution.query_interval_seconds - (
                            self.monotonic() - last_query_started
                        )
                        await self.pause(
                            min(
                                max(0, remaining),
                                max(0, (cfg.execution.stop_at - self.clock()).total_seconds()),
                            )
                        )
                    if self.clock() >= cfg.execution.stop_at or self.stop.is_set():
                        break
                    last_query_started = self.monotonic()
                    offers = await self.measured("query", self.adapter.query, travel_date)
                    candidates = rank_offers(offers, cfg, self.clock())
                    if candidates:
                        offer = candidates[0]
                        if not cfg.execution.auto_submit:
                            self.store.transition(
                                cfg.task_id,
                                "DRY_RUN",
                                "演练发现匹配车票：" + offer.summary(),
                                offer=offer,
                                notify=True,
                            )
                            await self.notifier.flush()
                            return
                        candidate_started = self.monotonic()
                        await self.measured("prepare", self.adapter.prepare, offer)
                        if self.clock() >= cfg.execution.stop_at or self.stop.is_set():
                            break
                        self.store.transition(
                            cfg.task_id, "SUBMITTING", "已记录提交意图，等待官方结果", offer=offer
                        )
                        try:
                            order_id = await self.measured(
                                "submit_and_reconcile", self.adapter.submit, offer
                            )
                        except Exception:
                            order_id = None
                        if order_id:
                            self.store.transition(
                                cfg.task_id,
                                "ORDER_CREATED",
                                offer.summary() + "，请在官方 App 核对并付款",
                                **self.receipt_fields(order_id),
                                notify=True,
                            )
                        else:
                            self.store.transition(
                                cfg.task_id,
                                "UNKNOWN",
                                "提交结果不确定，已停止再次下单；请核对官方订单",
                                notify=True,
                            )
                        self.store.record_timing(
                            cfg.task_id,
                            "candidate_to_result",
                            self.monotonic() - candidate_started,
                            "confirmed" if order_id else "unknown",
                        )
                        await self.notifier.flush()
                        return
                errors = 0
                # When every configured date is in the past, do not busy-loop.
                if all(d < self.clock().astimezone(SHANGHAI).date() for d in cfg.journey.dates):
                    break
            except NeedsAttention as exc:
                self.store.transition(cfg.task_id, "ATTENTION", str(exc), notify=True)
                await self.notifier.flush()
                return
            except Exception as exc:
                errors += 1
                if errors >= cfg.execution.max_consecutive_errors:
                    self.store.transition(
                        cfg.task_id, "ATTENTION", self.safe_error(exc), notify=True
                    )
                    await self.notifier.flush()
                    return
                delay = min(
                    cfg.execution.query_interval_seconds * 2 ** (errors - 1),
                    cfg.execution.max_backoff_seconds,
                )
                self.store.transition(cfg.task_id, "BACKOFF", f"查询失败，{delay:g} 秒后重试")
                await self.pause(
                    min(delay, max(0, (cfg.execution.stop_at - self.clock()).total_seconds()))
                )
        if not self.stop.is_set():
            self.store.transition(cfg.task_id, "EXPIRED", "任务截止，未确认创建订单", notify=True)
        await self.notifier.flush()

    @staticmethod
    def receipt_fields(result) -> dict:
        if isinstance(result, OrderReceipt):
            return {"order_id": result.order_id, "receipt": result}
        return {"order_id": result}

    @staticmethod
    def safe_error(exc: Exception) -> str:
        if isinstance(exc, (NeedsAttention, QueryFailed)):
            return str(exc)
        return f"执行异常（{type(exc).__name__}），请检查本机浏览器；已暂停"
