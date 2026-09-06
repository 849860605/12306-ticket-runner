from dataclasses import replace

import pytest

from ticket_runner.browser import BrowserAdapter
from ticket_runner.domain import NeedsAttention


def test_trip_check_rejects_different_train_and_station_suffix(offer):
    BrowserAdapter.verify_trip("2026-10-01 G698 深圳北站 → 南京南站", offer)
    with pytest.raises(NeedsAttention):
        BrowserAdapter.verify_trip("2026-10-01 G6980 深圳北站 → 南京南站", offer)
    with pytest.raises(NeedsAttention):
        BrowserAdapter.verify_trip(
            "2026-10-01 G698 深圳北站 → 南京南站", replace(offer, origin="深圳")
        )
    with pytest.raises(NeedsAttention):
        BrowserAdapter.verify_trip("2026-10-02 G698 深圳北站 → 南京南站", offer)


def test_official_compact_trip_header_and_direction(offer):
    BrowserAdapter.verify_trip(
        "2026-10-01（周四）G698次深圳北站（08:35开）—南京南站（17:01到）", offer
    )
    for text in (
        "2026-10-01（周四）G698次南京南站（08:35开）—深圳北站（17:01到）",
        "2026-10-01（周四）G698次深圳北东站（08:35开）—南京南站（17:01到）",
        "2026-10-01 G698A 深圳北站 → 南京南站",
    ):
        with pytest.raises(NeedsAttention):
            BrowserAdapter.verify_trip(text, offer)


@pytest.mark.parametrize(
    "label,valid",
    [
        ("二等座", True),
        ("二等座（¥608.5元）", True),
        ("二等座(￥608.50元)", True),
        ("一等座（¥954.5元）", False),
        ("二等座包厢（¥608.5元）", False),
        ("二等座（¥608.5元）无座", False),
    ],
)
def test_seat_label_with_price_is_still_exact(label, valid):
    assert bool(BrowserAdapter.seat_option(label, "二等座")) is valid
