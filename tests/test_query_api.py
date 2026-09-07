import hashlib
import json
from datetime import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from ticket_runner.browser import BrowserAdapter
from ticket_runner.config import Config
from ticket_runner.domain import NeedsAttention, QueryFailed, rank_offers
from ticket_runner.query_api import (
    ReadOnlyQueryClient,
    decode_fares,
    discover_query_path,
    inventory_offers,
    parse_inventory,
)


def wire_payload(**changes):
    fields = [""] * 58
    defaults = {
        0: "SYNTHETIC-SECRET-NEVER-LOG",
        2: "6i000G69800",
        3: "G698",
        6: "IOQ",
        7: "NKH",
        8: "08:35",
        9: "17:00",
        11: "Y",
        13: "20261001",
        16: "01",
        17: "15",
        30: "1",
        31: "无",
        35: "OM",
        39: "O076400001M095450000",
    }
    defaults.update({int(k): v for k, v in changes.items()})
    for index, value in defaults.items():
        fields[index] = value
    return {
        "status": True,
        "httpstatus": 200,
        "data": {
            "flag": "1",
            "map": {"IOQ": "深圳北", "NKH": "南京南"},
            "result": ["|".join(fields)],
        },
    }


def response(body, *, status=200, content_type="application/json"):
    result = MagicMock()
    result.status = status
    result.headers = {"content-type": content_type}
    result.body = AsyncMock(
        return_value=body if isinstance(body, bytes) else json.dumps(body).encode()
    )
    result.dispose = AsyncMock()
    return result


def test_current_wire_mapping_prices_and_no_token_leak(config, now):
    rows = parse_inventory(wire_payload(), config.journey.dates[0], config)
    offers = inventory_offers(rows, config)
    assert len(offers) == 1
    assert offers[0].row_id == "ticket_6i000G69800_01_15"
    assert offers[0].seat == "二等座" and offers[0].price == Decimal("764")
    assert offers[0].available == 1
    assert len(rank_offers(offers, config, now)) == 1
    assert "SYNTHETIC-SECRET" not in json.dumps(rows)


@pytest.mark.parametrize("count", ["", "无", "候补", "--", "0"])
def test_sold_out_rows_remain_selectable_but_never_buy(config, count):
    rows = parse_inventory(wire_payload(**{"30": count}), config.journey.dates[0], config)
    assert len(rows) == 1 and inventory_offers(rows, config) == []


def test_have_is_unknown_count_not_infinite_and_budget_still_applies(config, now):
    rows = parse_inventory(wire_payload(**{"30": "有"}), config.journey.dates[0], config)
    offers = inventory_offers(rows, config)
    assert offers[0].available is None
    config.preferences.max_total_price = Decimal("100")
    assert rank_offers(offers, config, now) == []


@pytest.mark.parametrize("changes", [{"0": ""}, {"0": "null"}, {"11": "N"}])
def test_unbookable_is_not_a_purchase_candidate(config, changes):
    rows = parse_inventory(wire_payload(**changes), config.journey.dates[0], config)
    assert not rows[0]["bookable"] and inventory_offers(rows, config) == []


def test_exact_stations_time_and_train_preferences(config):
    assert parse_inventory(wire_payload(**{"6": "SZQ"}), config.journey.dates[0], config) == []
    config.preferences.departure_after = time(9, 0)
    assert parse_inventory(wire_payload(), config.journey.dates[0], config) == []
    config.preferences.departure_after = time(0, 0)
    rows = parse_inventory(wire_payload(**{"3": "G999"}), config.journey.dates[0], config)
    assert len(rows) == 1 and inventory_offers(rows, config) == []


def test_station_dictionary_mismatch_requires_attention(config):
    payload = wire_payload()
    payload["data"]["map"]["IOQ"] = "深圳"
    with pytest.raises(NeedsAttention):
        parse_inventory(payload, config.journey.dates[0], config)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": False},
        {"status": True, "data": None},
        {"status": True, "data": {"flag": "1", "map": {}, "result": "not-a-list"}},
    ],
)
def test_failed_or_changed_envelope_is_never_no_tickets(config, payload):
    with pytest.raises(QueryFailed):
        parse_inventory(payload, config.journey.dates[0], config)


@pytest.mark.parametrize(
    "changes", [{"30": "未知"}, {"8": "oops"}, {"16": "../"}, {"3": "G698<script>"}, {"2": "../x"}]
)
def test_unknown_row_fields_fail_closed(config, changes):
    with pytest.raises(QueryFailed):
        parse_inventory(wire_payload(**changes), config.journey.dates[0], config)


def test_duplicate_short_rows_and_explicit_empty(config):
    payload = wire_payload()
    payload["data"]["result"] *= 2
    with pytest.raises(QueryFailed):
        parse_inventory(payload, config.journey.dates[0], config)
    payload["data"]["result"] = ["short|row"]
    with pytest.raises(QueryFailed):
        parse_inventory(payload, config.journey.dates[0], config)
    payload["data"]["result"] = []
    assert parse_inventory(payload, config.journey.dates[0], config) == []


def test_fares_are_decimal_tenths_and_unknown_price_is_not_zero(config):
    assert decode_fares("O060850001M0954500009188750000") == {
        "O": Decimal("608.5"),
        "M": Decimal("954.5"),
        "9": Decimal("1887.5"),
    }
    rows = parse_inventory(wire_payload(**{"39": ""}), config.journey.dates[0], config)
    assert rows[0]["seats"][0]["price"] is None
    with pytest.raises(QueryFailed, match="票价"):
        inventory_offers(rows, config)


@pytest.mark.parametrize("packed", ["O06085", "Oxx0850001", "O060850001O060000001"])
def test_malformed_or_conflicting_fares_fail_closed(packed):
    with pytest.raises(QueryFailed):
        decode_fares(packed)


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test/query",
        "//evil.test/x",
        "leftTicket/../queryG",
        "leftTicket/queryG?x=1",
        "leftTicket/submitOrderRequest",
    ],
)
def test_only_discovered_same_origin_query_paths_are_allowed(path):
    with pytest.raises(NeedsAttention):
        discover_query_path(f"var CLeftTicketUrl = '{path}';")


def test_no_guessing_query_path():
    assert discover_query_path("var CLeftTicketUrl='leftTicket/queryG';") == "leftTicket/queryG"
    for html in (
        "",
        "var CLeftTicketUrl='leftTicket/queryG'; var CLeftTicketUrl='leftTicket/queryU';",
    ):
        with pytest.raises(NeedsAttention):
            discover_query_path(html)


async def test_transport_reuses_one_context_disposes_responses_and_has_no_order_calls(config):
    request = MagicMock()
    init = response(b"var CLeftTicketUrl='leftTicket/queryG';", content_type="text/html")
    result = response(wire_payload())
    request.get = AsyncMock(side_effect=[init, result, response(wire_payload())])
    request.post = AsyncMock()
    client = ReadOnlyQueryClient(request)
    assert len(await client.offers(config.journey.dates[0], config)) == 1
    await client.catalog(config.journey.dates[0], config)
    assert request.get.await_count == 3  # Init is reused; no page refresh or fare request.
    for call in request.get.await_args_list:
        assert call.kwargs["max_redirects"] == call.kwargs["max_retries"] == 0
        assert call.args[0].startswith("https://kyfw.12306.cn/otn/leftTicket/")
    request.post.assert_not_awaited()
    init.dispose.assert_awaited_once()
    result.dispose.assert_awaited_once()


@pytest.mark.parametrize(
    "status,error",
    [(302, NeedsAttention), (403, NeedsAttention), (429, QueryFailed), (503, QueryFailed)],
)
async def test_transport_never_retries_rejection_or_follows_redirect(status, error):
    request = MagicMock()
    reply = response({}, status=status)
    request.get = AsyncMock(return_value=reply)
    client = ReadOnlyQueryClient(request)
    with pytest.raises(error):
        await client._read("leftTicket/queryG")
    assert request.get.await_count == 1
    reply.dispose.assert_awaited_once()


async def test_transport_html_and_sensitive_errors_not_leaked():
    request = MagicMock()
    request.get = AsyncMock(
        return_value=response(b"<html>verification</html>", content_type="text/html")
    )
    client = ReadOnlyQueryClient(request)
    with pytest.raises(NeedsAttention):
        await client._read("leftTicket/queryG")
    request.get.side_effect = RuntimeError("Cookie: SENSITIVE; secretStr=SECRET")
    with pytest.raises(QueryFailed) as caught:
        await client._read("leftTicket/queryG")
    assert "SENSITIVE" not in str(caught.value) and "SECRET" not in str(caught.value)


async def test_login_check_is_read_only_and_strict():
    request = MagicMock()
    request.post = AsyncMock(return_value=response({"status": True, "data": {"flag": True}}))
    client = ReadOnlyQueryClient(request)
    assert await client.check_login() is True
    request.post.return_value = response({"status": True, "data": {"flag": False}})
    assert await client.check_login() is False
    request.post.return_value = response({"status": True, "data": {"flag": "true"}})
    with pytest.raises(NeedsAttention):
        await client.check_login()
    assert all(c.args[0].endswith("/login/checkUser") for c in request.post.await_args_list)
    with pytest.raises(ValueError):
        await client._read("confirmPassenger/confirmSingleForQueue", login_check=True)


async def test_api_adapter_does_not_refresh_page_or_silently_fallback(config, tmp_path):
    config.query_backend = "api"
    adapter = BrowserAdapter(config, tmp_path)
    adapter.query_rows = AsyncMock()
    adapter.api_client = MagicMock()
    adapter.api_client.offers = AsyncMock(return_value=[])
    adapter.api_client.last_metrics = {"trains": 0, "seconds": 0.1}
    assert await adapter.query(config.journey.dates[0]) == []
    adapter.query_rows.assert_not_awaited()
    adapter.api_client.offers.side_effect = QueryFailed("retry later")
    with pytest.raises(QueryFailed):
        await adapter.query(config.journey.dates[0])
    adapter.query_rows.assert_not_awaited()


async def test_stale_api_hit_does_not_click_booking(config, offer, tmp_path):
    config.query_backend = "api"
    adapter = BrowserAdapter(config, tmp_path)
    adapter.query_rows = AsyncMock(return_value=[])
    adapter.page = MagicMock()
    with pytest.raises(QueryFailed, match="未点击预订"):
        await adapter.prepare(offer)
    adapter.page.locator.assert_not_called()


def test_legacy_browser_fingerprint_preserved_api_mode_is_distinct(config):
    legacy = hashlib.sha256(config.model_dump_json(exclude={"query_backend"}).encode()).hexdigest()
    assert config.fingerprint() == legacy
    config.query_backend = "api"
    assert config.fingerprint() != legacy
    with pytest.raises(ValueError):
        Config.model_validate({**config.model_dump(), "query_backend": "api-order"})
