from dataclasses import replace
from datetime import time, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ticket_runner.config import Config
from ticket_runner.domain import parse_offers, query_url, rank_offers


def test_query_url_preserves_station_separator(config):
    url = query_url(config.journey.origin, config.journey.destination, config.journey.dates[0])
    assert ",IOQ" in url and ",NKH" in url
    assert "%2C" not in url
    assert "date=2026-10-01" in url


def test_parse_accessible_ticket_descriptions(config):
    rows = [
        {
            "id": "ticket_example",
            "train": "G698",
            "origin": "深圳北",
            "destination": "南京南",
            "departure": "08:35",
            "bookable": True,
            "seats": [
                "G698次列车，商务座票价2549元，余票6",
                "G698次列车，一等座票价1221元，余票候补",
                "G698次列车，二等座票价764元，余票有",
                "G698次列车，无座票价764元，余票0",
                "G699次列车，硬座票价10元，余票1",
            ],
        }
    ]
    offers = parse_offers(rows, config.journey.dates[0])
    assert [(x.seat, x.available) for x in offers] == [("商务座", 6), ("二等座", None)]


def test_exact_stations_budget_and_group_size(config, offer, now):
    bad = [
        replace(offer, origin="深圳"),
        replace(offer, destination="南京"),
        replace(offer, price=Decimal("1000.01")),
        replace(offer, available=0),
        replace(offer, seat="无座"),
    ]
    assert rank_offers(bad, config, now) == []
    config.journey.passengers.append("李四")
    assert rank_offers([offer], config, now) == []
    assert rank_offers([replace(offer, price=Decimal("500"), available=2)], config, now)


def test_preference_order_and_elapsed_departure(config, offer, now):
    later_train = replace(offer, train="G2756", price=Decimal("700"))
    higher_seat = replace(offer, seat="一等座", price=Decimal("999"))
    assert rank_offers([later_train, higher_seat, offer], config, now) == [
        offer,
        higher_seat,
        later_train,
    ]
    assert rank_offers([offer], config, now + timedelta(days=30)) == []


def test_departure_window_and_date_preference(config, offer, now):
    config.preferences.departure_after = time(8, 30)
    config.journey.dates.append(offer.travel_date + timedelta(days=1))
    early = replace(offer, departure=time(8, 29))
    boundary = replace(offer, departure=time(8, 30))
    later_day = replace(boundary, travel_date=config.journey.dates[1])
    assert rank_offers([later_day, early, offer, boundary], config, now) == [
        boundary,
        offer,
        later_day,
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["execution"].update(start_at="2026-09-17T10:00:00"),
        lambda c: c["execution"].update(query_interval_seconds=0),
        lambda c: c["preferences"].update(seats=["无座"]),
        lambda c: c["preferences"].update(max_total_price="-1"),
        lambda c: c["journey"].update(passengers=["张三", "张三"]),
        lambda c: c.update(typo="silently ignored?"),
    ],
)
def test_invalid_configuration_is_rejected(config, mutation):
    raw = config.model_dump()
    mutation(raw)
    with pytest.raises(ValidationError):
        Config.model_validate(raw)
