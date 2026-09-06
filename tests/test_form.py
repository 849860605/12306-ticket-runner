from unittest.mock import AsyncMock, MagicMock

import pytest

from ticket_runner.browser import BrowserAdapter
from ticket_runner.domain import NeedsAttention


@pytest.mark.parametrize(
    "name,seat,ticket,accepted",
    [
        ("张三", "二等座（¥764元）", "成人票", True),
        ("李四", "二等座（¥764元）", "成人票", False),
        ("张三", "二等座（¥1000.01元）", "成人票", False),
        ("张三", "一等座（¥764元）", "成人票", False),
        ("张三", "二等座", "成人票", False),
        ("张三", "二等座（¥764元）", "儿童票", False),
    ],
)
async def test_form_audit_checks_names_seats_prices_and_adult_ticket(
    config, offer, tmp_path, name, seat, ticket, accepted
):
    adapter = BrowserAdapter(config, tmp_path)
    summary = AsyncMock()
    summary.inner_text.return_value = "2026-10-01 G698次深圳北站（08:35开）—南京南站（17:01到）"
    adapter.one = AsyncMock(return_value=summary)

    def field_list(value):
        field = MagicMock()
        field.input_value = AsyncMock(return_value=value)
        field.locator.return_value.inner_text = AsyncMock(return_value=value)
        collection = MagicMock()
        collection.all = AsyncMock(return_value=[field])
        return collection

    fields = {
        adapter.selectors["passenger_name"]: field_list(name),
        adapter.selectors["seat_select"]: field_list(seat),
        adapter.selectors["ticket_select"]: field_list(ticket),
    }
    adapter.page = MagicMock()
    adapter.page.locator.side_effect = fields.__getitem__
    if accepted:
        assert await adapter.verify_form(offer) == 764
    else:
        with pytest.raises(NeedsAttention):
            await adapter.verify_form(offer)
