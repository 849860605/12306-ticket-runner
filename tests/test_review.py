from unittest.mock import AsyncMock

import pytest

from ticket_runner.browser import BrowserAdapter
from ticket_runner.domain import NeedsAttention


class Review:
    def __init__(self, text, rows):
        self.text, self.rows = text, rows

    async def inner_text(self):
        return self.text

    def locator(self, _):
        return self

    async def evaluate_all(self, _):
        return self.rows


@pytest.mark.parametrize(
    "rows,total,accepted",
    [
        ([["1", "二等座", "成人票", "张三"]], "764", True),
        ([["1", "二等座", "成人票", "张三"]], "1000.01", False),
        ([["1", "二等座", "成人票", "张三丰"]], "764", False),
        ([["1", "二等座", "成人票", "张三"], ["2", "二等座", "成人票", "李四"]], "764", False),
        ([["1", "二等座", "儿童票", "张三"]], "764", False),
        ([["1", "一等座", "成人票", "张三"]], "764", False),
        ([["1", "二等座", "成人票", "张三"]], "", False),
    ],
)
async def test_final_review_requires_exact_passenger_type_seat_and_budget(
    config, offer, tmp_path, rows, total, accepted
):
    adapter = BrowserAdapter(config, tmp_path)
    text = "2026-10-01 G698 深圳北站 → 南京南站\n" + "\n".join(" ".join(r) for r in rows)
    if total:
        text += f"\n合计：{total}元"
    adapter.one = AsyncMock(return_value=Review(text, rows))
    adapter.verify_form = AsyncMock(side_effect=NeedsAttention("无法核实表单报价"))
    if accepted:
        await adapter.verify_review(offer)
    else:
        with pytest.raises(NeedsAttention):
            await adapter.verify_review(offer)


async def test_dialog_without_total_requires_independently_verified_form_quote(
    config, offer, tmp_path
):
    from decimal import Decimal

    adapter = BrowserAdapter(config, tmp_path)
    text = "2026-10-01 G698次深圳北站（08:35开）—南京南站（17:01到）\n张三 成人票 二等座"
    adapter.one = AsyncMock(return_value=Review(text, [["1", "二等座", "成人票", "张三"]]))
    adapter.verify_form = AsyncMock(return_value=Decimal("764"))
    await adapter.verify_review(offer)
    adapter.verify_form.assert_awaited_once_with(offer)
    adapter.verify_form.return_value = Decimal("1000.01")
    with pytest.raises(NeedsAttention):
        await adapter.verify_review(offer)
