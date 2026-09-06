from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from ticket_runner import cli


def setup_checks(monkeypatch, config, offer, now):
    adapter = AsyncMock()
    adapter.__aenter__.return_value = adapter
    adapter.query.return_value = [offer]
    adapter.checkout_diagnostics.return_value = {}
    monkeypatch.setattr(cli, "BrowserAdapter", lambda *_: adapter)
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    clock = MagicMock()
    clock.now.return_value = now
    monkeypatch.setattr(cli, "datetime", clock)
    return adapter


async def test_checkout_check_never_submits_even_if_config_allows_it(
    monkeypatch, config, offer, now, tmp_path
):
    config.execution.auto_submit = True
    adapter = setup_checks(monkeypatch, config, offer, now)
    args = cli.parser().parse_args(["--data-dir", str(tmp_path), "check-checkout"])
    assert await cli.execute(args) == 0
    adapter.check_existing_orders.assert_awaited_once()
    adapter.prepare.assert_awaited_once_with(offer)
    adapter.submit.assert_not_awaited()


async def test_multi_date_probe_is_query_only_and_spaces_requests(
    monkeypatch, config, offer, now, tmp_path
):
    config.journey.dates.append(config.journey.dates[0] + timedelta(days=1))
    adapter = setup_checks(monkeypatch, config, offer, now)
    sleep = AsyncMock()
    monkeypatch.setattr(cli.asyncio, "sleep", sleep)
    args = cli.parser().parse_args(["--data-dir", str(tmp_path), "probe", "--all-dates"])
    assert await cli.execute(args) == 0
    assert [call.args[0] for call in adapter.query.await_args_list] == config.journey.dates
    sleep.assert_awaited_once_with(config.execution.query_interval_seconds)
    adapter.ensure_login.assert_not_awaited()
    adapter.prepare.assert_not_awaited()
    adapter.submit.assert_not_awaited()
