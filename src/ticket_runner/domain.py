from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import Decimal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .config import Config, Station

SHANGHAI = ZoneInfo("Asia/Shanghai")
QUERY_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"
ORDER_URL = "https://kyfw.12306.cn/otn/view/train_order.html"


class NeedsAttention(RuntimeError):
    """Stop automation and let the owner inspect the same browser."""


class QueryFailed(RuntimeError):
    """A failed query must never be interpreted as no inventory."""


@dataclass(frozen=True)
class OrderReceipt:
    """Visible official evidence; never invent an order number when none is shown."""

    order_id: str | None
    total_price: str
    passenger_count: int
    assigned_seats: tuple[str, ...]
    verified_at: str
    source: str = "official_unpaid_table"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Offer:
    row_id: str
    train: str
    travel_date: date
    origin: str
    destination: str
    departure: time
    seat: str
    price: Decimal
    available: int | None  # None means 有 (unquantified), never infinite certainty.

    def summary(self) -> str:
        return f"{self.travel_date} {self.train} {self.origin}→{self.destination} {self.seat}"

    def to_dict(self) -> dict:
        result = asdict(self)
        for key in ("travel_date", "departure", "price"):
            result[key] = str(result[key])
        return result

    @classmethod
    def from_dict(cls, raw: dict) -> Offer:
        return cls(
            **{
                **raw,
                "travel_date": date.fromisoformat(raw["travel_date"]),
                "departure": time.fromisoformat(raw["departure"]),
                "price": Decimal(raw["price"]),
            }
        )


def query_url(origin: Station, destination: Station, travel_date: date) -> str:
    # 12306 splits fs/ts by a literal comma before decoding the station name.
    params = {
        "linktypeid": "dc",
        "fs": f"{origin.name},{origin.code}",
        "ts": f"{destination.name},{destination.code}",
        "date": travel_date.isoformat(),
        "flag": "N,N,Y",
    }
    return QUERY_URL + "?" + urlencode(params, safe=",")


def parse_offers(rows: list[dict], travel_date: date) -> list[Offer]:
    offers = []
    for row in rows:
        if not row.get("bookable"):
            continue
        try:
            departure = time.fromisoformat(row["departure"])
        except (ValueError, KeyError):
            raise QueryFailed("无法识别出发时间，网页结构可能变化") from None
        for description in row["seats"]:
            match = re.fullmatch(
                r"(.+?)次列车，(.+?)票价([\d.]+)元，余票(有|\d+|候补|无|--)",
                description.strip(),
            )
            if not match:
                continue
            train, seat, price, count = match.groups()
            if train != row["train"] or count in {"候补", "无", "--", "0"}:
                continue
            offers.append(
                Offer(
                    row["id"],
                    train,
                    travel_date,
                    row["origin"],
                    row["destination"],
                    departure,
                    seat,
                    Decimal(price),
                    None if count == "有" else int(count),
                )
            )
    return offers


def rank_offers(offers: list[Offer], config: Config, now: datetime) -> list[Offer]:
    journey, pref = config.journey, config.preferences
    candidates = []
    for offer in offers:
        if offer.origin != journey.origin.name or offer.destination != journey.destination.name:
            continue
        if offer.travel_date not in journey.dates or offer.seat not in pref.seats:
            continue
        if pref.trains and offer.train not in pref.trains:
            continue
        if not pref.departure_after <= offer.departure <= pref.departure_before:
            continue
        if datetime.combine(offer.travel_date, offer.departure, SHANGHAI) <= now:
            continue
        if offer.price <= 0 or offer.price * len(journey.passengers) > pref.max_total_price:
            continue
        if offer.available is not None and offer.available < len(journey.passengers):
            continue
        candidates.append(offer)
    return sorted(
        candidates,
        key=lambda x: (
            journey.dates.index(x.travel_date),
            pref.trains.index(x.train) if pref.trains else 0,
            pref.seats.index(x.seat),
            x.departure,
            x.price,
        ),
    )


def parse_catalog(rows: list[dict], travel_date: date, config: Config) -> list[dict]:
    """A selection catalog, including sold-out trains. Never used as purchase authority."""
    catalog = []
    for row in rows:
        if (row.get("origin"), row.get("destination")) != (
            config.journey.origin.name,
            config.journey.destination.name,
        ):
            continue
        if not re.fullmatch(r"[A-Z]?\d{1,5}", row.get("train", "")):
            continue
        try:
            departure = time.fromisoformat(row["departure"])
        except (ValueError, KeyError):
            continue
        if (
            not config.preferences.departure_after
            <= departure
            <= config.preferences.departure_before
        ):
            continue
        seats = []
        for description in row.get("seats", []):
            match = re.fullmatch(
                r"(.+?)次列车，(.+?)票价([\d.]+)元，余票(有|\d+|候补|无|--)", description.strip()
            )
            if match and match[1] == row["train"]:
                seats.append({"name": match[2], "price": match[3], "status": match[4]})
        catalog.append(
            {
                "train": row["train"],
                "date": str(travel_date),
                "origin": row["origin"],
                "destination": row["destination"],
                "departure": row["departure"],
                "arrival": row.get("arrival", ""),
                "bookable": bool(row.get("bookable")),
                "seats": seats,
            }
        )
    return catalog
