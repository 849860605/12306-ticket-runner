from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from .browser import BrowserAdapter
from .config import load_config
from .domain import SHANGHAI, NeedsAttention, Offer, rank_offers
from .notify import Notifier
from .runner import Runner
from .state import Store, exclusive_run

log = logging.getLogger(__name__)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="12306 单账号配置驱动购票执行器")
    root.add_argument("--config", type=Path, default=Path("config.yaml"))
    root.add_argument("--data-dir", type=Path, default=Path("data"))
    root.add_argument("--selectors", type=Path, help="登录后页面结构需要适配时的 JSON 选择器覆盖")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="检查配置，不联网、不登录")
    sub.add_parser("login", help="官方扫码登录，二维码保存为 data/login-qr.png")
    probe = sub.add_parser("probe", help="只查询并筛选；不登录、不点击预订、不提交")
    probe.add_argument(
        "--all-dates", action="store_true", help="依次检查全部配置日期，遵守查询间隔"
    )
    account = sub.add_parser(
        "check-account", help="扫码后只检查登录及未完成订单页面，不预订、不下单"
    )
    account.add_argument(
        "--diagnostics", action="store_true", help="输出订单页提示及元素结构以辅助适配"
    )
    sub.add_parser("check-checkout", help="按配置进入预订表单并核对乘车人；不点击提交订单")
    sub.add_parser("check-order", help="只核对已记录的待确认订单；不查询余票、不提交、不付款")
    run = sub.add_parser("run", help="按配置执行任务；auto_submit 决定是否真实提交")
    run.add_argument(
        "--keep-alive", action="store_true", help="任务停止后保留浏览器供接管，并继续投递通知"
    )
    sub.add_parser("status", help="查看本地状态，不启动浏览器")
    sub.add_parser("timings", help="查看阶段耗时统计；不联网、不启动浏览器")
    sub.add_parser("notify", help="重试当前待发送通知，不启动购票")
    resolve = sub.add_parser("resolve", help="在官方 App 核对后人工解除暂停/结束任务")
    resolve.add_argument("--task-id", required=True)
    resolve.add_argument(
        "--outcome",
        choices=["no-order", "done"],
        required=True,
        help="no-order=已确认没有订单、允许重试；done=任务已处理完毕",
    )
    sub.add_parser("demo", help="离线模拟超时、重启、核对订单流程，不访问12306")
    serve = sub.add_parser("serve", help="启动 V2 本地控制台；不会自动开始抢票")
    serve.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "0.0.0.0", "::1"])
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--demo", action="store_true", help="独立演示模式：不联网、不使用真实账号、不生成真实订单"
    )
    return root


async def execute(args) -> int:
    if args.command == "demo":
        from .demo import demonstrate

        await demonstrate()
        return 0
    if args.command in {"status", "timings"}:
        store = Store(args.data_dir)
        try:
            print(
                json.dumps(
                    store.status() if args.command == "status" else store.timing_report(),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            store.close()
        return 0
    if args.command == "resolve":
        with exclusive_run(args.data_dir):
            store = Store(args.data_dir)
            try:
                store.resolve(args.task_id, args.outcome)
                print("已记录人工处理结果。任务恢复需重新运行 run。")
            finally:
                store.close()
        return 0
    config = load_config(args.config)
    if args.command == "validate":
        print(f"配置有效：{config.task_id}；自动提交：{config.execution.auto_submit}")
        if config.execution.stop_at <= datetime.now(SHANGHAI):
            print("注意：任务已过期，run 不会再执行购票。")
        return 0
    with exclusive_run(args.data_dir):
        store = Store(args.data_dir)
        notifier = Notifier(store, config.notification)
        try:
            if args.command == "notify":
                await notifier.flush()
                return 0
            async with BrowserAdapter(config, args.data_dir, args.selectors) as adapter:
                if args.command == "check-order":
                    existing = store.get(config.task_id)
                    if (
                        not existing
                        or not existing["offer"]
                        or existing["state"] not in {"SUBMITTING", "UNKNOWN", "ORDER_CREATED"}
                    ):
                        raise NeedsAttention("没有可以回查的已记录订单，不会创建新订单")
                    store.register(config)
                    await adapter.ensure_login()
                    receipt = await adapter.reconcile(
                        Offer.from_dict(json.loads(existing["offer"]))
                    )
                    if not receipt:
                        print(
                            json.dumps(
                                await adapter.account_diagnostics(), ensure_ascii=False, indent=2
                            )
                        )
                        raise NeedsAttention("未能逐项确认既有待支付订单，保留原状态，禁止自动重下")
                    store.transition(
                        config.task_id,
                        "ORDER_CREATED",
                        "已逐项核对官方待支付订单；未付款，不再自动下单",
                        receipt=receipt,
                        order_id=receipt.order_id,
                        notify=existing["state"] != "ORDER_CREATED",
                    )
                    print(json.dumps(store.status(), ensure_ascii=False, indent=2))
                    return 0
                if args.command == "login":
                    await adapter.ensure_login()
                    return 0
                if args.command == "check-account":
                    await adapter.ensure_login()
                    try:
                        await adapter.check_existing_orders()
                    finally:
                        if args.diagnostics:
                            print(
                                json.dumps(
                                    await adapter.account_diagnostics(),
                                    ensure_ascii=False,
                                    indent=2,
                                )
                            )
                    print("官网登录和未完成订单页面检查通过，未创建任何订单。")
                    return 0
                if args.command == "probe":
                    dates = config.journey.dates if args.all_dates else config.journey.dates[:1]
                    for index, travel_date in enumerate(dates):
                        if index:
                            await asyncio.sleep(config.execution.query_interval_seconds)
                        offers = await adapter.query(travel_date)
                        ranked = rank_offers(offers, config, datetime.now(SHANGHAI))
                        print(
                            json.dumps(
                                {
                                    "mode": "read_only",
                                    "date": str(travel_date),
                                    "parsed_offers": len(offers),
                                    "matches": [offer.to_dict() for offer in ranked],
                                },
                                ensure_ascii=False,
                                indent=2,
                            )
                        )
                    return 0
                if args.command == "check-checkout":
                    await adapter.ensure_login()
                    await adapter.check_existing_orders()
                    for index, travel_date in enumerate(config.journey.dates):
                        if index:
                            await asyncio.sleep(config.execution.query_interval_seconds)
                        offers = await adapter.query(travel_date)
                        candidates = rank_offers(offers, config, datetime.now(SHANGHAI))
                        if not candidates:
                            continue
                        offer = candidates[0]
                        print(
                            json.dumps(
                                {"mode": "prepare_only", "offer": offer.to_dict()},
                                ensure_ascii=False,
                            )
                        )
                        try:
                            await adapter.prepare(offer)
                        finally:
                            print(
                                json.dumps(
                                    await adapter.checkout_diagnostics(offer),
                                    ensure_ascii=False,
                                    indent=2,
                                )
                            )
                        print("乘车人及预订表单检查通过，未点击提交订单，未创建订单。")
                        return 0
                    print("当前没有符合配置的可预订车票，未进入预订表单。")
                    return 0
                runner = Runner(config, store, adapter, notifier)

                async def login_notification():
                    current = store.get(config.task_id)
                    state = current["state"] if current else "LOGIN_REQUIRED"
                    # Preserve SUBMITTING/UNKNOWN if a recovered order needs authentication.
                    store.transition(
                        config.task_id,
                        state,
                        "需要官方扫码登录，请查看远程桌面或 login-qr.png",
                        notify=True,
                    )
                    await notifier.flush()

                adapter.on_login_required = login_notification
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, runner.stop.set)
                work = asyncio.create_task(runner.run())
                interrupted = asyncio.create_task(runner.stop.wait())

                async def deliver_notifications():
                    while not runner.stop.is_set():
                        await notifier.flush()
                        await asyncio.sleep(1)

                delivery = asyncio.create_task(deliver_notifications())
                try:
                    done, _ = await asyncio.wait(
                        {work, interrupted}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if interrupted in done and not work.done():
                        work.cancel()
                    try:
                        await work
                    except asyncio.CancelledError:
                        log.info("收到停止信号，已保留任务状态")
                finally:
                    interrupted.cancel()
                    delivery.cancel()
                    with suppress(asyncio.CancelledError):
                        await delivery
                print(json.dumps(store.status(), ensure_ascii=False, indent=2))
                if args.keep_alive:
                    log.info("任务已停止执行，保留浏览器与通知服务；修改配置/resolve 前先停止容器")
                    while not runner.stop.is_set():
                        await notifier.flush()
                        await runner.pause(10)
                return 0
        finally:
            store.close()


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parser().parse_args()
    try:
        if args.command == "serve":
            import uvicorn

            from .web import create_app

            if not 1024 <= args.port <= 65535:
                raise ValueError("端口必须在 1024 到 65535 之间")
            config = load_config(args.config)
            print(f"V2 控制台：http://127.0.0.1:{args.port}；启动界面不会开始抢票。", flush=True)
            uvicorn.run(
                create_app(config, args.data_dir, demo=args.demo),
                host=args.host,
                port=args.port,
                access_log=False,
                timeout_graceful_shutdown=10,
                proxy_headers=False,
            )
            return
        result = asyncio.run(execute(args))
    except NeedsAttention as exc:
        log.error("%s", exc)
        result = 2
    except (ValueError, FileNotFoundError) as exc:
        # Configuration errors contain user-provided values; keep tracebacks out of logs.
        log.error("配置/文件错误：%s", exc)
        result = 2
    except KeyboardInterrupt:
        result = 130
    except Exception as exc:
        log.error("运行失败：%s；检查浏览器依赖、配置和官方页面", type(exc).__name__)
        result = 1
    raise SystemExit(result)


if __name__ == "__main__":
    main()
