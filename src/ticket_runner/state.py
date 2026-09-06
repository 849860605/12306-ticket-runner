from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .domain import NeedsAttention, Offer, OrderReceipt

BLOCKING_STATES = ("SUBMITTING", "UNKNOWN", "ORDER_CREATED")
TERMINAL_STATES = (*BLOCKING_STATES, "DONE", "EXPIRED", "DRY_RUN", "ATTENTION")


@contextmanager
def exclusive_run(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data_dir / "runner.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NeedsAttention("已有执行器或登录进程占用此数据目录") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(data_dir / "state.sqlite3")
        os.chmod(data_dir / "state.sqlite3", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, offer TEXT, order_id TEXT,
                message TEXT NOT NULL DEFAULT '', updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                title TEXT NOT NULL, body TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS timings (
                id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, stage TEXT NOT NULL,
                seconds REAL NOT NULL, outcome TEXT NOT NULL, recorded REAL NOT NULL
            );
        """)
        if "receipt" not in {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN receipt TEXT")
            self.db.commit()

    def close(self):
        self.db.close()

    def record_timing(self, task_id: str, stage: str, seconds: float, outcome: str):
        with self.db:
            self.db.execute(
                "INSERT INTO timings(task_id,stage,seconds,outcome,recorded) VALUES(?,?,?,?,?)",
                (task_id, stage, max(0, seconds), outcome, time.time()),
            )
            # Keep long-lived unattended instances bounded; no passenger data here.
            self.db.execute("DELETE FROM timings WHERE id <= (SELECT MAX(id)-10000 FROM timings)")

    def timing_report(self) -> dict:
        rows = self.db.execute("SELECT stage,seconds,outcome FROM timings ORDER BY id").fetchall()
        report = {}
        for stage in sorted({row["stage"] for row in rows}):
            matching = [row for row in rows if row["stage"] == stage]
            values = sorted(row["seconds"] for row in matching)
            report[stage] = {
                "samples": len(values),
                "mean_seconds": round(sum(values) / len(values), 3),
                "p95_seconds": round(values[max(0, (95 * len(values) + 99) // 100 - 1)], 3),
                "max_seconds": round(values[-1], 3),
                "outcomes": {
                    key: sum(row["outcome"] == key for row in matching)
                    for key in sorted({row["outcome"] for row in matching})
                },
            }
        return {"stages": report, "note": "本机阶段耗时，不是抢票成功率或真人速度对照实验"}

    def get(self, task_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def register(self, config: Config):
        old = self.get(config.task_id)
        if old and old["fingerprint"] != config.fingerprint():
            raise NeedsAttention("同一 task_id 的配置发生变化；核对旧订单后使用新的 task_id")
        if not old:
            with self.db:
                self.db.execute(
                    "INSERT INTO tasks(task_id,fingerprint,state,updated) VALUES(?,?,?,?)",
                    (config.task_id, config.fingerprint(), "READY", time.time()),
                )

    def blocker(self, excluding: str | None = None) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM tasks WHERE state IN ('SUBMITTING','UNKNOWN','ORDER_CREATED') "
            "AND task_id != ? LIMIT 1",
            (excluding or "",),
        ).fetchone()
        return dict(row) if row else None

    def transition(
        self,
        task_id: str,
        state: str,
        message: str = "",
        *,
        offer: Offer | None = None,
        order_id: str | None = None,
        receipt: OrderReceipt | None = None,
        notify: bool = False,
    ):
        with self.db:
            self.db.execute(
                "UPDATE tasks SET state=?, message=?, updated=?, "
                "offer=COALESCE(?,offer), order_id=COALESCE(?,order_id), receipt=COALESCE(?,receipt) WHERE task_id=?",
                (
                    state,
                    message,
                    time.time(),
                    json.dumps(offer.to_dict()) if offer else None,
                    order_id,
                    json.dumps(receipt.to_dict()) if receipt else None,
                    task_id,
                ),
            )
            if notify:
                self.db.execute(
                    "INSERT INTO outbox(task_id,title,body) VALUES(?,?,?)",
                    (task_id, f"12306 {state}", message),
                )

    def resolve(self, task_id: str, outcome: str):
        old = self.get(task_id)
        if not old or old["state"] not in (*BLOCKING_STATES, "ATTENTION", "DRY_RUN"):
            raise NeedsAttention("该任务不处于可以人工处理的状态")
        state = "READY" if outcome == "no-order" else "DONE"
        with self.db:
            self.db.execute(
                "UPDATE tasks SET state=?,offer=NULL,order_id=NULL,receipt=NULL,message=?,updated=? WHERE task_id=?",
                (state, f"用户核对官方订单后标记 {outcome}", time.time(), task_id),
            )

    def status(self) -> dict:
        tasks = [
            dict(row)
            for row in self.db.execute(
                "SELECT task_id,state,message,order_id,receipt,updated FROM tasks ORDER BY updated DESC"
            )
        ]
        for task in tasks:
            task["receipt"] = json.loads(task["receipt"]) if task["receipt"] else None
        pending = self.db.execute("SELECT COUNT(*) FROM outbox WHERE delivered=0").fetchone()[0]
        return {"tasks": tasks, "pending_notifications": pending}
