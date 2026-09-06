import re
from unittest.mock import AsyncMock

import pytest

from ticket_runner.browser import EMPTY_ORDER_PATTERN, BrowserAdapter
from ticket_runner.domain import NeedsAttention


def test_observed_empty_order_message_requires_exact_match():
    assert re.fullmatch(EMPTY_ORDER_PATTERN, "您没有未完成的订单哦～")
    assert not re.fullmatch(EMPTY_ORDER_PATTERN, "没有未完成订单时可以继续购票")
    assert not re.fullmatch(EMPTY_ORDER_PATTERN, "您没有已完成的订单哦～")


@pytest.mark.parametrize(
    "state,accepted",
    [
        ({"active": True, "empty": True, "hasCards": False}, True),
        ({"active": False, "empty": True, "hasCards": False}, False),
        ({"active": True, "empty": False, "hasCards": False}, False),
        ({"active": True, "empty": True, "hasCards": True}, False),
    ],
)
async def test_account_check_requires_positive_empty_active_tab(config, tmp_path, state, accepted):
    adapter = BrowserAdapter(config, tmp_path)
    adapter.open_orders = AsyncMock()
    adapter.page = AsyncMock()
    adapter.page.evaluate.return_value = state
    if accepted:
        await adapter.check_existing_orders()
    else:
        with pytest.raises(NeedsAttention):
            await adapter.check_existing_orders()
