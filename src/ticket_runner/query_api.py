"""Read-only 12306 query transport; intentionally contains no order submission API.

Wire fields and fare units were checked against the official query page's public
queryLeftTicket_end_js.js on 2026-09-07. Unknown schemas fail closed.
"""

from __future__ import annotations

import json
import re
import time as monotonic_time
from datetime import date, time
from decimal import Decimal

from .config import Config
from .domain import NeedsAttention, Offer, QueryFailed

BASE = "https://kyfw.12306.cn/otn/"
INIT_PATH = "leftTicket/init"
LOGIN_CHECK_PATH = "login/checkUser"
QUERY_PATH = re.compile(r"leftTicket/query[A-Z]?")
# Display name, public seat code, inventory column. No standing/sleeper fallback.
SEAT_FIELDS = (
    ("二等座", "O", 30),
    ("一等座", "M", 31),
    ("商务座", "9", 32),
    ("特等座", "P", 25),
    ("硬座", "1", 29),
    ("软座", "2", 24),
)
NO_SEATS = {"", "--", "无", "候补", "0"}


def discover_query_path(html: str) -> str:
    matches = re.findall(r"\bCLeftTicketUrl\s*=\s*['\"]([^'\"]+)['\"]", html)
    if len(set(matches)) != 1 or not QUERY_PATH.fullmatch(matches[0]):
        raise NeedsAttention("官网查询入口无法确认；未猜测接口或切换其他地址")
    return matches[0]


def decode_fares(packed: str) -> dict[str, Decimal]:
    if not packed:
        return {}
    if len(packed) % 10 or len(packed) > 1000:
        raise QueryFailed("接口票价格式变化，不能使用未知报价")
    fares = {}
    for offset in range(0, len(packed), 10):
        item = packed[offset : offset + 10]
        if not re.fullmatch(r"[A-Z0-9][0-9]{9}", item):
            raise QueryFailed("接口票价格式变化，不能使用未知报价")
        code, price = item[0], Decimal(item[1:6]) / 10
        if code in fares and fares[code] != price:
            raise QueryFailed("同一席别存在不同报价，需通过官方页面核对")
        if price > 0:
            fares[code] = price
    return fares


def parse_inventory(payload, day: date, config: Config) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("status") is not True:
        raise QueryFailed("余票接口未返回成功状态，不能当作无票")
    data = payload.get("data")
    if (
        payload.get("httpstatus", 200) != 200
        or not isinstance(data, dict)
        or not isinstance(data.get("result"), list)
        or not isinstance(data.get("map"), dict)
        or data.get("flag") != "1"
    ):
        raise QueryFailed("余票接口结构变化，不能当作无票")
    rows = data["result"]
    if len(rows) > 2000:
        raise QueryFailed("余票结果规模异常，已停止解析")
    result, seen = [], set()
    origin, destination = config.journey.origin, config.journey.destination
    for raw in rows:
        if not isinstance(raw, str) or len(raw) > 20000:
            raise QueryFailed("余票车次结构无法识别")
        fields = raw.split("|")
        if len(fields) < 40:
            raise QueryFailed("余票车次字段不足，不能当作无票")
        if (fields[6], fields[7]) != (origin.code, destination.code):
            continue
        if (
            data["map"].get(origin.code) != origin.name
            or data["map"].get(destination.code) != destination.name
        ):
            raise NeedsAttention("接口返回的车站名称与配置代码不对应，请核对站点")
        if not re.fullmatch(r"[A-Z]?\d{1,5}", fields[3]) or not re.fullmatch(
            r"[A-Za-z0-9]{6,24}", fields[2]
        ):
            raise QueryFailed("接口车次标识无法识别")
        if any(not re.fullmatch(r"\d{2,3}", fields[i]) for i in (16, 17)):
            raise QueryFailed("接口上下车站序无法识别")
        try:
            departure = time.fromisoformat(fields[8])
            time.fromisoformat(fields[9])
        except ValueError:
            raise QueryFailed("接口发到时刻无法识别") from None
        if (
            not config.preferences.departure_after
            <= departure
            <= config.preferences.departure_before
        ):
            continue
        row_id = f"ticket_{fields[2]}_{fields[16]}_{fields[17]}"
        if row_id in seen:
            raise QueryFailed("接口返回重复行程，停止使用不明确结果")
        seen.add(row_id)
        fares = decode_fares(fields[39])
        seats = []
        for name, code, index in SEAT_FIELDS:
            count = fields[index] or "--"
            if count not in NO_SEATS and count != "有" and not re.fullmatch(r"\d{1,5}", count):
                raise QueryFailed("接口余票数量无法识别，不能当作无票")
            price = fares.get(code)
            seats.append({"name": name, "status": count, "price": str(price) if price else None})
        result.append(
            {
                "row_id": row_id,
                "train": fields[3],
                "date": str(day),
                "origin": origin.name,
                "destination": destination.name,
                "departure": fields[8],
                "arrival": fields[9],
                "bookable": fields[11] == "Y" and fields[0] not in {"", "null"},
                "seats": seats,
                "source": "api",
            }
        )
    return result


def inventory_offers(rows: list[dict], config: Config) -> list[Offer]:
    result = []
    for row in rows:
        if not row["bookable"] or (
            config.preferences.trains and row["train"] not in config.preferences.trains
        ):
            continue
        for seat in row["seats"]:
            count = seat["status"]
            if seat["name"] not in config.preferences.seats or count in NO_SEATS:
                continue
            if seat["price"] is None:
                raise QueryFailed("匹配车次缺少可核实票价，不使用未知价格进入预订")
            result.append(
                Offer(
                    row["row_id"],
                    row["train"],
                    date.fromisoformat(row["date"]),
                    row["origin"],
                    row["destination"],
                    time.fromisoformat(row["departure"]),
                    seat["name"],
                    Decimal(seat["price"]),
                    None if count == "有" else int(count),
                )
            )
    return result


class ReadOnlyQueryClient:
    """Accepts Playwright APIRequestContext; context.request shares browser cookies.

    Only GET query/init and the read-only POST login check are allowed. Redirects
    and transport retries are disabled. Responses and order tokens are not retained.
    The caller owns serial query pacing across all dates and engines.
    """

    def __init__(self, request, *, timeout_seconds=25):
        self.request = request
        self.timeout_seconds = timeout_seconds
        self.endpoint = None
        self.last_metrics = {}

    async def _read(self, path, *, params=None, login_check=False):
        allowed = (
            path == LOGIN_CHECK_PATH
            if login_check
            else path == INIT_PATH or bool(QUERY_PATH.fullmatch(path))
        )
        if not allowed:
            raise ValueError("Read-only transport rejects this endpoint")
        options = {
            "timeout": self.timeout_seconds * 1000,
            "max_redirects": 0,
            "max_retries": 0,
            "headers": {"Accept": "application/json, text/html", "Referer": BASE + INIT_PATH},
        }
        response = None
        try:
            if login_check:
                response = await self.request.post(BASE + path, form={}, **options)
            else:
                response = await self.request.get(BASE + path, params=params, **options)
            status = response.status
            if status in {401, 403} or 300 <= status < 400:
                raise NeedsAttention("接口要求登录、核验或跳转；已停止，请在官方浏览器处理")
            if status == 429:
                raise QueryFailed("接口提示访问频繁，将按执行器规则退避")
            if status != 200:
                raise QueryFailed(f"接口 HTTP {status}，不能当作无票")
            body = await response.body()
            if len(body) > 2_000_000:
                raise QueryFailed("接口响应过大，停止解析")
            content_type = response.headers.get("content-type", "").lower()
            if path == INIT_PATH:
                if "html" not in content_type:
                    raise NeedsAttention("查询初始化页面无法确认")
                return body.decode("utf-8", errors="strict")
            if "json" not in content_type:
                raise NeedsAttention("接口没有返回 JSON，可能需要登录或核验；未当作无票")
            try:
                return json.loads(body)
            except (ValueError, UnicodeError):
                raise QueryFailed("接口 JSON 无法识别，不能当作无票") from None
        except (QueryFailed, NeedsAttention, ValueError):
            raise
        except Exception as exc:
            # Playwright errors can contain URLs/headers: never echo the original.
            raise QueryFailed(f"接口请求失败（{type(exc).__name__}），等待退避或人工检查") from None
        finally:
            if response:
                await response.dispose()

    async def initialize(self):
        if self.endpoint is None:
            self.endpoint = discover_query_path(await self._read(INIT_PATH))

    async def check_login(self) -> bool:
        payload = await self._read(LOGIN_CHECK_PATH, login_check=True)
        if (
            not isinstance(payload, dict)
            or payload.get("status") is not True
            or not isinstance(payload.get("data"), dict)
            or type(payload["data"].get("flag")) is not bool
        ):
            raise NeedsAttention("登录接口状态无法确认，未假定登录成功")
        return payload["data"]["flag"]

    async def catalog(self, day: date, config: Config) -> list[dict]:
        await self.initialize()
        started = monotonic_time.monotonic()
        payload = await self._read(
            self.endpoint,
            params={
                "leftTicketDTO.train_date": day.isoformat(),
                "leftTicketDTO.from_station": config.journey.origin.code,
                "leftTicketDTO.to_station": config.journey.destination.code,
                "purpose_codes": "ADULT",
            },
        )
        rows = parse_inventory(payload, day, config)
        self.last_metrics = {
            "endpoint": self.endpoint,
            "seconds": round(monotonic_time.monotonic() - started, 3),
            "trains": len(rows),
            "source": "api",
        }
        return rows

    async def offers(self, day: date, config: Config) -> list[Offer]:
        return inventory_offers(await self.catalog(day, config), config)
