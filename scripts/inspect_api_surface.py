"""One-shot anonymous, read-only inspection of the official query page and endpoint.

No account/profile is opened. No POST, booking, payment, credential dump or retries.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ticket_runner.config import load_config

BASE = "https://kyfw.12306.cn"
INIT = BASE + "/otn/leftTicket/init"


def report(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)


def inspect(config_path: Path, assets: bool, fare: bool, source_only: bool):
    config = load_config(config_path)
    with httpx.Client(timeout=25, follow_redirects=False) as client:
        started = time.monotonic()
        page = client.get(INIT, params={"linktypeid": "dc"})
        report(
            stage="init",
            status=page.status_code,
            seconds=round(time.monotonic() - started, 3),
            content_type=page.headers.get("content-type", ""),
            bytes=len(page.content),
        )
        if page.status_code != 200:
            return
        match = re.search(r"\bCLeftTicketUrl\s*=\s*['\"](leftTicket/query[A-Z]?)['\"]", page.text)
        report(stage="discovery", query_path=match[1] if match else None)
        if assets:
            paths = re.findall(r'<script[^>]+src=["\']([^"\']+)', page.text, re.I)
            report(stage="public_scripts", paths=paths)
            for path in paths:
                url = urljoin(INIT, path)
                if urlsplit(url).netloc != "kyfw.12306.cn" or "queryLeftTicket" not in url:
                    continue
                source = client.get(url)
                report(
                    stage="public_query_source", status=source.status_code, path=urlsplit(url).path
                )
                if source.status_code == 200:
                    expressions = (
                        (r"function e\([^)]*\).{0,4500}",)
                        if source_only
                        else (
                            r'.{0,80}\.split\("\|"\).{0,2500}',
                            r'.{0,80}url:ctx\+"leftTicket/submitOrderRequest".{0,900}',
                            r".{0,80}yp_info_new.{0,400}",
                        )
                    )
                    for expression in expressions:
                        for excerpt in re.findall(expression, source.text)[:3]:
                            report(stage="public_source_excerpt", code=excerpt)
        if not match or source_only:
            return
        time.sleep(5)
        day = config.journey.dates[0].isoformat()
        params = {
            "leftTicketDTO.train_date": day,
            "leftTicketDTO.from_station": config.journey.origin.code,
            "leftTicketDTO.to_station": config.journey.destination.code,
            "purpose_codes": "ADULT",
        }
        started = time.monotonic()
        response = client.get(
            BASE + "/otn/" + match[1],
            params=params,
            headers={"Referer": str(page.url), "Accept": "application/json"},
        )
        report(
            stage="query",
            status=response.status_code,
            seconds=round(time.monotonic() - started, 3),
            content_type=response.headers.get("content-type", ""),
            bytes=len(response.content),
            redirected=response.is_redirect,
        )
        if (
            response.status_code != 200
            or "json" not in response.headers.get("content-type", "").lower()
        ):
            return
        payload = response.json()
        data = payload.get("data")
        report(
            stage="envelope",
            accepted=payload.get("status") is True,
            data_type=type(data).__name__,
            keys=sorted(data) if isinstance(data, dict) else [],
        )
        if not isinstance(data, dict):
            return
        rows = data.get("result", [])
        report(stage="rows", count=len(rows))
        # Only print ordinary timetable fields, never raw rows (contain order tokens).
        for raw in rows:
            cells = raw.split("|")
            if len(cells) < 36 or (
                config.preferences.trains and cells[3] not in config.preferences.trains
            ):
                continue
            report(
                stage="train",
                columns=len(cells),
                train=cells[3],
                origin=cells[6],
                destination=cells[7],
                departure=cells[8],
                arrival=cells[9],
                second_class=cells[30],
                first_class=cells[31],
                bookable=cells[11],
            )
            if fare:
                time.sleep(5)
                quote = client.get(
                    BASE + "/otn/leftTicket/queryTicketPrice",
                    params={
                        "train_no": cells[2],
                        "from_station_no": cells[16],
                        "to_station_no": cells[17],
                        "seat_types": cells[35],
                        "train_date": day,
                    },
                    headers={"Referer": str(page.url), "Accept": "application/json"},
                )
                report(
                    stage="fare",
                    status=quote.status_code,
                    content_type=quote.headers.get("content-type", ""),
                )
                if (
                    quote.status_code == 200
                    and "json" in quote.headers.get("content-type", "").lower()
                ):
                    prices = quote.json().get("data", {})
                    if isinstance(prices, dict):
                        report(
                            stage="fare_values",
                            prices={
                                key: prices[key]
                                for key in ("O", "M", "9", "A9", "AO", "AM", "OT", "WZ", "wz")
                                if key in prices
                            },
                        )
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--assets", action="store_true")
    parser.add_argument(
        "--fare", action="store_true", help="Read one matching train's fare, never reserve"
    )
    parser.add_argument(
        "--source-only", action="store_true", help="Inspect public fare decoder, do not query"
    )
    args = parser.parse_args()
    try:
        inspect(args.config, args.assets, args.fare, args.source_only)
    except httpx.HTTPError as exc:
        report(stage="network_error", error_type=type(exc).__name__)
        raise SystemExit(2) from None
