from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SEATS = {"二等座", "一等座", "商务座", "特等座", "硬座", "软座"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Station(StrictModel):
    name: str = Field(min_length=1, max_length=30)
    code: str = Field(pattern=r"^[A-Z]{3}$")


class Journey(StrictModel):
    origin: Station
    destination: Station
    dates: list[date] = Field(min_length=1, max_length=15)
    passengers: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def check_journey(self):
        if self.origin.code == self.destination.code:
            raise ValueError("出发站和到达站不能相同")
        if len(set(self.dates)) != len(self.dates):
            raise ValueError("乘车日期不能重复")
        if any(not x.strip() for x in self.passengers):
            raise ValueError("乘车人姓名不能为空")
        if len(set(self.passengers)) != len(self.passengers):
            raise ValueError("同名乘车人需要人工处理，不能自动区分")
        return self


class Preferences(StrictModel):
    trains: list[str] = Field(default_factory=list)
    seats: list[str] = Field(default_factory=lambda: ["二等座"], min_length=1)
    max_total_price: Decimal = Field(gt=0, max_digits=8, decimal_places=2)
    departure_after: time = time(0, 0)
    departure_before: time = time(23, 59)

    @field_validator("trains")
    @classmethod
    def valid_trains(cls, values):
        if any(not re.fullmatch(r"[A-Z]?\d{1,5}", x) for x in values):
            raise ValueError("车次必须为 G698 等大写格式")
        if len(set(values)) != len(values):
            raise ValueError("车次不能重复")
        return values

    @field_validator("seats")
    @classmethod
    def valid_seats(cls, values):
        if not set(values) <= SEATS or len(set(values)) != len(values):
            raise ValueError(f"席别仅支持 {sorted(SEATS)}，且不能重复")
        return values

    @model_validator(mode="after")
    def check_times(self):
        if self.departure_after > self.departure_before:
            raise ValueError("出发时段不能跨午夜，请拆分任务")
        return self


class Execution(StrictModel):
    start_at: datetime
    stop_at: datetime
    auto_submit: bool = False
    query_interval_seconds: float = Field(default=30, ge=5, le=3600)
    max_backoff_seconds: float = Field(default=300, ge=5, le=3600)
    max_consecutive_errors: int = Field(default=5, ge=1, le=100)
    prepare_seconds: int = Field(default=300, ge=0, le=3600)

    @model_validator(mode="after")
    def check_times(self):
        for dt in (self.start_at, self.stop_at):
            if dt.tzinfo is None or dt.utcoffset() is None:
                raise ValueError("起止时间必须带时区，例如 +08:00")
        if self.start_at >= self.stop_at:
            raise ValueError("stop_at 必须晚于 start_at")
        if self.max_backoff_seconds < self.query_interval_seconds:
            raise ValueError("max_backoff_seconds 不能小于查询间隔")
        return self


class BrowserConfig(StrictModel):
    headless: bool = False
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    login_timeout_seconds: int = Field(default=600, ge=30, le=3600)


class NotificationConfig(StrictModel):
    webhook_url_env: str = Field(default="TICKET_NOTIFY_WEBHOOK", pattern=r"^[A-Z][A-Z0-9_]*$")
    attempts: int = Field(default=5, ge=1, le=20)


class Config(StrictModel):
    task_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    journey: Journey
    preferences: Preferences
    execution: Execution
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def load_config(path: Path) -> Config:
    with path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    return Config.model_validate(raw)
