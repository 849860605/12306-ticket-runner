from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from .auth import restore_session, save_session
from .config import Config
from .domain import (
    LOGIN_URL,
    ORDER_URL,
    SHANGHAI,
    NeedsAttention,
    Offer,
    OrderReceipt,
    QueryFailed,
    parse_offers,
    query_url,
)

log = logging.getLogger(__name__)

# Selectors were observed on 2026-09-06, including a real unpaid order. Every action is
# guarded by semantic checks; unknown or ambiguous DOM always requires manual attention.
DEFAULT_SELECTORS = {
    "results": "#queryLeftTable",
    "login_qr": "#J-qrImg",
    "passengers": "#normal_passenger_id",
    "passenger_rows": "#passengerInfo_id tr",
    "seat_select": "select[id^='seatType_']",
    "ticket_select": "select[id^='ticketType_']",
    "passenger_name": "input[id^='passenger_name_']",
    "trip_summary": "#ticket_tit_id",
    "submit": "#submitOrder_id",
    "review": "#content_checkticketinfo_id",
    "confirm": "#qr_submit_id",
    "order_tab": "#order_tab",
    "order_empty": ".order-empty .empty-txt p",
    "order_cards": ".order-panel-unpaid .order-item-bd",
}

EMPTY_ORDER_PATTERN = (
    r"^(?:您没有未完成的订单哦[～~]?|您没有对应的订单内容|暂无未完成订单|没有未完成订单)[。！!]*$"
)

ORDER_STATE_JS = """settings => {
    const visible = e => e && e.getClientRects().length;
    const active = Array.from(document.querySelectorAll(settings.tab + ' .active'))
        .some(e => visible(e) && e.innerText.trim() === '未完成订单');
    const hasCards = Array.from(document.querySelectorAll(settings.cards))
        .some(e => visible(e) && e.querySelector('table.order-item-table tr td'));
    const emptyPattern = new RegExp(settings.emptyPattern);
    const empty = Array.from(document.querySelectorAll(settings.empty))
        .some(e => visible(e) && emptyPattern.test(e.innerText.trim()));
    return {active, hasCards, empty};
}"""

QUERY_ROWS_JS = """root => Array.from(root.querySelectorAll('tr[id^="ticket_"]'))
  .filter(r => r.getClientRects().length && r.querySelector('a.number'))
  .map(r => ({
    id: r.id,
    train: r.querySelector('a.number').textContent.trim(),
    origin: r.querySelector('.cdz strong:first-child')?.textContent.trim() || '',
    destination: r.querySelector('.cdz strong:last-child')?.textContent.trim() || '',
    departure: r.querySelector('.cds strong:first-child')?.textContent.trim() || '',
    seats: Array.from(r.querySelectorAll('td[aria-label]')).map(c => c.getAttribute('aria-label')),
    bookable: Array.from(r.querySelectorAll('a')).some(a => a.textContent.trim() === '预订')
  }))"""

ORDER_ROWS_JS = """cards => cards.filter(e => e.getClientRects().length).map(card => ({
    rows: Array.from(card.querySelectorAll('table.order-item-table tr'))
      .filter(r => r.getClientRects().length)
      .map(r => Array.from(r.querySelectorAll(':scope > td')).map(c => ({
        text: c.innerText.trim(),
        segments: (() => {
          const walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
          const pieces = [];
          while (walker.nextNode()) {
            const n = walker.currentNode;
            if (n.parentElement.getClientRects().length && n.textContent.trim())
              pieces.push(n.textContent.trim());
          }
          return pieces;
        })()
      })))
}))"""

QUERY_READY_JS = """selector => {
    const root = document.querySelector(selector);
    const text = document.body.innerText;
    return /操作频繁|访问频繁|请求过于频繁|请完成验证|请进行验证|进行登录核验|滑动验证|网络环境存在风险/.test(text)
      || location.pathname.includes('error.html')
      || (root && Array.from(root.querySelectorAll('tr[id^="ticket_"]'))
          .some(r => r.getClientRects().length && r.querySelector('a.number')))
      || Array.from(document.querySelectorAll('body *')).some(e =>
          !e.children.length && e.getClientRects().length &&
          e.innerText?.includes('没有符合筛选条件的车次，请修改筛选条件'));
}"""


class BrowserAdapter:
    def __init__(self, config: Config, data_dir: Path, selectors_path: Path | None = None):
        self.config, self.data_dir = config, data_dir
        self.selectors = dict(DEFAULT_SELECTORS)
        if selectors_path:
            with selectors_path.open(encoding="utf-8") as stream:
                overrides = json.load(stream)
            if not isinstance(overrides, dict) or set(overrides) - set(self.selectors):
                raise ValueError("未知页面适配项")
            if any(not isinstance(v, str) or not v for v in overrides.values()):
                raise ValueError("页面适配项必须为非空 CSS 选择器")
            self.selectors.update(overrides)
        self.page = None
        self.context = None
        self.playwright = None
        self.current_date: date | None = None
        self.on_login_required = None

    async def __aenter__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.data_dir / "browser-profile"),
                headless=self.config.browser.headless,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1440, "height": 1000},
                accept_downloads=False,
            )
            await restore_session(self.context, self.data_dir)
            self.context.set_default_timeout(self.config.browser.timeout_seconds * 1000)
            self.page = await self.context.new_page()
        except BaseException:
            if self.context:
                await self.context.close()
            await self.playwright.stop()
            raise
        return self

    async def __aexit__(self, *_):
        if self.context:
            try:
                if self.page and not self.page.is_closed() and await self.authenticated():
                    await save_session(self.context, self.data_dir)
            except Exception:
                # Keep the last successful checkpoint; do not mask order/task errors.
                log.warning("退出时未能更新登录状态，保留上次成功的会话文件")
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()

    async def authenticated(self) -> bool:
        # Do not infer successful login just from a cookie or disappearance of the QR.
        logout = self.page.get_by_role("link", name="退出", exact=True)
        return await logout.count() == 1 and await logout.is_visible()

    async def ensure_login(self):
        if await self.authenticated():
            await save_session(self.context, self.data_dir)
            return
        if (self.data_dir / "session-cookies.json").exists():
            # The login entry can show a new QR even when the protected order page
            # accepts the restored session. Check that page before requesting a scan.
            await self.page.goto(ORDER_URL, wait_until="domcontentloaded")
            try:
                await self.page.get_by_role("link", name="退出", exact=True).wait_for(
                    state="visible"
                )
            except PlaywrightTimeoutError:
                await self.detect_interruption()
            else:
                if await self.authenticated():
                    await save_session(self.context, self.data_dir)
                    log.info("已复用官网登录状态，无需重新扫码")
                    return
        await self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        qr_tab = self.page.get_by_role("link", name="扫码登录", exact=True)
        ready_deadline = asyncio.get_running_loop().time() + self.config.browser.timeout_seconds
        while asyncio.get_running_loop().time() < ready_deadline:
            if await self.authenticated():
                await save_session(self.context, self.data_dir)
                log.info("已复用官网登录状态")
                return
            if await qr_tab.count() == 1 and await qr_tab.is_visible():
                break
            await asyncio.sleep(0.5)
        else:
            raise NeedsAttention("登录页未正常加载，请检查浏览器访问环境")
        await qr_tab.click()
        if self.on_login_required:
            await self.on_login_required()
        deadline = asyncio.get_running_loop().time() + self.config.browser.login_timeout_seconds
        qr_file = self.data_dir / "login-qr.png"
        next_capture = 0.0
        log.warning("请用官方 12306 App 扫码。二维码文件：%s；也可通过远程桌面操作", qr_file)
        while asyncio.get_running_loop().time() < deadline:
            if await self.authenticated():
                await save_session(self.context, self.data_dir)
                qr_file.unlink(missing_ok=True)
                log.info("已检测到官网登录状态")
                return
            expired = self.page.get_by_text("二维码已失效", exact=True)
            if await expired.count() == 1 and await expired.is_visible():
                refresh = self.page.get_by_role("link", name="刷新", exact=True)
                if await refresh.count() == 1 and await refresh.is_visible():
                    await refresh.click()
                    next_capture = asyncio.get_running_loop().time() + 2
            qr = self.page.locator(self.selectors["login_qr"])
            if (
                asyncio.get_running_loop().time() >= next_capture
                and await qr.count() == 1
                and await qr.is_visible()
            ):
                ready = await qr.evaluate(
                    "e => e.tagName !== 'IMG' || (e.complete && e.naturalWidth > 50)"
                )
                if ready:
                    await qr.screenshot(path=str(qr_file))
                    qr_file.chmod(0o600)
                    next_capture = asyncio.get_running_loop().time() + 10
            await asyncio.sleep(2)
        raise NeedsAttention("扫码登录/手机核验未在规定时间内完成；请执行 login 后恢复任务")

    async def detect_interruption(self):
        # Read only rendered text. Error classification does not expose account contents.
        text = await self.page.locator("body").inner_text()
        if any(x in text for x in ("操作频繁", "访问频繁", "请求过于频繁")):
            raise QueryFailed("官网提示访问频繁，进入退避")
        if any(
            x in text
            for x in ("请完成验证", "请进行验证", "进行登录核验", "滑动验证", "网络环境存在风险")
        ):
            raise NeedsAttention("官网要求额外核验，请通过同一浏览器完成后恢复任务")
        if "error.html" in self.page.url:
            raise NeedsAttention("官网返回异常页面，请检查访问环境")

    async def query(self, travel_date: date) -> list[Offer]:
        target = query_url(self.config.journey.origin, self.config.journey.destination, travel_date)
        # Fresh navigation prevents accidentally reading rows from a previous date/query.
        await self.page.goto(target, wait_until="domcontentloaded")
        self.current_date = travel_date
        results = self.page.locator(self.selectors["results"])
        await results.wait_for(state="attached")
        try:
            # Poll only local DOM state at 50 ms, NOT the website/network. This avoids
            # adding a fixed 500 ms to a result that has already arrived.
            await self.page.wait_for_function(
                QUERY_READY_JS, arg=self.selectors["results"], polling=50
            )
        except PlaywrightTimeoutError:
            raise QueryFailed("查询未返回可确认的结果，不能判定为无票") from None
        await self.detect_interruption()
        rows = await results.evaluate(QUERY_ROWS_JS)
        if rows:
            if not sum(len(row["seats"]) for row in rows):
                raise QueryFailed("结果缺少可核实的票价/余票描述，停止使用未知页面结构")
            return parse_offers(rows, travel_date)
        empty = self.page.get_by_text("没有符合筛选条件的车次，请修改筛选条件", exact=False)
        if await empty.count() == 1 and await empty.is_visible():
            return []
        raise QueryFailed("查询未返回可确认的结果，不能判定为无票")

    async def one(self, key: str):
        locator = self.page.locator(self.selectors[key])
        if await locator.count() != 1 or not await locator.is_visible():
            raise NeedsAttention(f"页面结构不匹配：{key}；请核对后更新 selectors.json")
        return locator

    @staticmethod
    def verify_trip(text: str, offer: Offer):
        compact = re.sub(r"\s+", "", text)
        dates = (
            offer.travel_date.isoformat(),
            offer.travel_date.strftime("%Y年%m月%d日"),
            f"{offer.travel_date.year}年{offer.travel_date.month}月{offer.travel_date.day}日",
        )

        def station_pattern(name):
            return rf"(?<![\u4e00-\u9fff]){re.escape(name)}(?:站)?(?![\u4e00-\u9fff])"

        train_pattern = rf"(?<![A-Z0-9]){re.escape(offer.train)}(?![A-Z0-9])"
        # The official header joins 'G846次深圳北站' without spaces. Remove only
        # the verified train prefix before checking exact station boundaries.
        route_text = re.sub(train_pattern + r"(?:\s*次)?", " ", text)
        origins = list(re.finditer(station_pattern(offer.origin), route_text))
        destinations = list(re.finditer(station_pattern(offer.destination), route_text))
        if (
            not re.search(train_pattern, text)
            or len(origins) != 1
            or len(destinations) != 1
            or origins[0].start() >= destinations[0].start()
            or not any(d in compact for d in dates)
        ):
            raise NeedsAttention("确认页面的车次、日期或车站与配置不匹配")

    @staticmethod
    def seat_option(label: str, seat: str):
        return re.fullmatch(
            re.escape(seat) + r"(?:[（(]\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元[）)])?",
            label.strip(),
        )

    @staticmethod
    async def passenger_checkbox(container, name: str):
        # Bind the exact visible native label to its checkbox. Other accessible
        # names/hidden templates must not select a different passenger.
        labels = container.locator("label").filter(
            has_text=re.compile(r"^\s*" + re.escape(name) + r"\s*$")
        )
        visible = [label for label in await labels.all() if await label.is_visible()]
        if len(visible) != 1:
            raise NeedsAttention("乘车人不存在或姓名有歧义，请在官网检查")
        target = await visible[0].get_attribute("for")
        if not target or not re.fullmatch(r"normalPassenger_\d+", target):
            raise NeedsAttention("乘车人姓名与复选框不能可靠关联")
        checkbox = container.locator(f"input[type=checkbox][id='{target}']")
        if (
            await checkbox.count() != 1
            or not await checkbox.is_visible()
            or not await checkbox.is_enabled()
        ):
            raise NeedsAttention("配置乘车人不可选或存在重复字段")
        return checkbox

    async def prepare(self, offer: Offer):
        if self.current_date != offer.travel_date:
            raise NeedsAttention("查询日期与待提交行程不一致")
        await self.detect_interruption()
        if not await self.authenticated():
            raise NeedsAttention("登录已失效，请重新扫码")
        # The exact result row includes route segment; a train number alone is ambiguous.
        if not re.fullmatch(r"ticket_[A-Za-z0-9_]+", offer.row_id):
            raise NeedsAttention("无法识别车次行标识")
        row = self.page.locator(f"[id='{offer.row_id}']")
        if await row.count() != 1:
            raise NeedsAttention("车次行发生变化，请重新查询")
        button = row.get_by_role("link", name="预订", exact=True)
        if await button.count() != 1:
            raise NeedsAttention("当前车次没有唯一的预订入口")
        await button.click()
        container = self.page.locator(self.selectors["passengers"])
        try:
            await container.wait_for(state="attached")
            # The official UL contains floated children and may have zero height;
            # its rendered checkboxes, not the wrapper box, must be visible.
            await container.locator("input[type=checkbox]").first.wait_for(state="visible")
        except PlaywrightTimeoutError:
            await self.detect_interruption()
            raise NeedsAttention("没有进入可识别的乘车人确认页，请远程检查") from None
        self.verify_trip(await (await self.one("trip_summary")).inner_text(), offer)
        if await container.count() != 1:
            raise NeedsAttention("乘车人列表有歧义")
        # Clear any previous selections so unrelated passengers cannot be purchased.
        selected = container.locator("input[type=checkbox]:checked")
        for checkbox in await selected.all():
            await checkbox.uncheck()
        for name in self.config.journey.passengers:
            checkbox = await self.passenger_checkbox(container, name)
            await checkbox.check()
        seat_boxes = self.page.locator(self.selectors["seat_select"])
        ticket_boxes = self.page.locator(self.selectors["ticket_select"])
        count = len(self.config.journey.passengers)
        if await seat_boxes.count() != count or await ticket_boxes.count() != count:
            raise NeedsAttention("乘车人数或票种字段不匹配")
        for seat, ticket in zip(await seat_boxes.all(), await ticket_boxes.all(), strict=True):
            await ticket.select_option(label="成人票")
            options = await seat.locator("option").evaluate_all(
                "es => es.map(e => ({label:e.textContent.trim(),value:e.value}))"
            )
            choices = [o for o in options if self.seat_option(o["label"], offer.seat)]
            if len(choices) != 1:
                raise NeedsAttention("配置席别不可选，不自动降级或改为无座")
            price = self.seat_option(choices[0]["label"], offer.seat).group(1)
            if price and (
                Decimal(price) <= 0
                or Decimal(price) * count > self.config.preferences.max_total_price
            ):
                raise NeedsAttention("预订表单的席别票价超出总预算或无法确认")
            await seat.select_option(value=choices[0]["value"])
        await self.verify_form(offer)
        await self.detect_interruption()

    async def verify_form(self, offer: Offer) -> Decimal:
        summary = await (await self.one("trip_summary")).inner_text()
        self.verify_trip(summary, offer)
        departure = re.search(
            re.escape(offer.origin) + r"(?:站)?[（(](\d{2}:\d{2})开[）)]",
            re.sub(r"\s+", "", summary),
        )
        if not departure or departure.group(1) != offer.departure.strftime("%H:%M"):
            raise NeedsAttention("预订表单的发车时刻与所选车票不一致")
        names = [
            (await field.input_value()).strip()
            for field in await self.page.locator(self.selectors["passenger_name"]).all()
        ]
        if sorted(names) != sorted(self.config.journey.passengers):
            raise NeedsAttention("预订表单乘车人姓名或人数与配置不一致")
        seats = await self.page.locator(self.selectors["seat_select"]).all()
        tickets = await self.page.locator(self.selectors["ticket_select"]).all()
        if len(seats) != len(names) or len(tickets) != len(names):
            raise NeedsAttention("预订表单席别或票种人数不一致")
        total = Decimal(0)
        for seat, ticket in zip(seats, tickets, strict=True):
            label = await seat.locator("option:checked").inner_text()
            match = self.seat_option(label, offer.seat)
            if not match or not match.group(1):
                raise NeedsAttention("预订表单不能核实所选席别和票价")
            price = Decimal(match.group(1))
            if price <= 0:
                raise NeedsAttention("预订表单票价异常")
            total += price
            if (await ticket.locator("option:checked").inner_text()).strip() != "成人票":
                raise NeedsAttention("预订表单存在非成人票")
        if total > self.config.preferences.max_total_price:
            raise NeedsAttention("预订表单合计票价超过预算")
        return total

    async def verify_review(self, offer: Offer):
        review = await self.one("review")
        text = await review.inner_text()
        # The main trip summary was verified before submission. The review must include
        # the same trip, every passenger, requested seat, adult ticket and actual total.
        self.verify_trip(text, offer)
        if any(name not in text for name in self.config.journey.passengers):
            raise NeedsAttention("最终确认页缺少配置乘车人")
        if offer.seat not in text:
            raise NeedsAttention("最终确认页席别不符")
        if "成人" not in text:
            raise NeedsAttention("最终确认页不能确认成人票种")
        rows = await review.locator("tr").evaluate_all(
            "es => es.filter(e=>e.getClientRects().length).map(e=>Array.from(e.querySelectorAll('td')).map(c=>c.innerText.trim()))"
        )
        passenger_rows = [
            row
            for row in rows
            if any(cell in {"成人", "成人票", "儿童", "儿童票", "学生", "学生票"} for cell in row)
        ]
        names = self.config.journey.passengers
        if (
            len(passenger_rows) != len(names)
            or any(sum(name in row for row in passenger_rows) != 1 for name in names)
            or any(
                offer.seat not in row or not any(c in {"成人", "成人票"} for c in row)
                for row in passenger_rows
            )
        ):
            raise NeedsAttention("最终确认页乘车人数量、姓名、票种或席别不能逐项匹配")
        total = re.search(r"(?:总票价|总金额|合计|总计)[：:\s]*[¥￥]?\s*(\d+(?:\.\d{1,2})?)", text)
        # The observed official dialog lists passengers/seats but no total. Its
        # exact rows must match the still-present form, whose selected fare for
        # each adult is independently checked and summed. Never infer unknown fares.
        amount = Decimal(total.group(1)) if total else await self.verify_form(offer)
        if amount <= 0 or amount > self.config.preferences.max_total_price:
            raise NeedsAttention("最终确认票价超出预算或无法识别")

    async def checkout_diagnostics(self, offer: Offer) -> dict:
        """Inspect rendered form structure without exporting passenger input values."""
        return await self.page.locator("body").evaluate(
            """(root, trip) => {
                const visible = e => e.getClientRects().length;
                const elements = Array.from(root.querySelectorAll('*')).filter(visible);
                return {
                    pathname: location.pathname,
                    ids: elements.filter(e => e.id).map(e => ({tag:e.tagName, id:e.id, class:e.className})),
                    summaries: elements.filter(e => {
                        const text = e.innerText?.trim() || '';
                        return text.length < 300 && text.includes(trip.train) &&
                            text.includes(trip.origin) && text.includes(trip.destination) &&
                            !/证件|姓名|身份证/.test(text);
                    }).map(e => ({tag:e.tagName, id:e.id, class:e.className, text:e.innerText.trim()})),
                    selects: elements.filter(e => e.tagName === 'SELECT').map(e => ({
                        id:e.id, options:Array.from(e.options).map(o => o.textContent.trim())
                    })),
                    passengers: Array.from(root.querySelectorAll('#normal_passenger_id input[type=checkbox]')).map(e => {
                        const item = e.closest('li') || e.parentElement;
                        const label = item.innerText.trim();
                        const matched = trip.passengers.filter(name => label.includes(name));
                        return {
                            id:e.id, labelCount:e.labels?.length || 0, disabled:e.disabled,
                            configuredNameMatches:matched.length,
                            configuredText:matched.length === 1 ? label.replaceAll(matched[0], '[配置乘车人]') : null,
                            descendants:Array.from(item.querySelectorAll('*')).map(c => ({tag:c.tagName,id:c.id,class:c.className}))
                        };
                    }),
                    actions: elements.filter(e => ['A','BUTTON'].includes(e.tagName) &&
                        /^(提交订单|确认|确认提交|取消|返回修改|未完成订单)$/.test(e.innerText.trim())
                    ).map(e => ({tag:e.tagName, id:e.id, text:e.innerText.trim()})),
                    reviewRows: elements.filter(e => e.tagName === 'TR').map(e =>
                        Array.from(e.querySelectorAll('td')).map(c => c.innerText.trim())
                    ).filter(row => row.some(c => ['成人','成人票','儿童票','学生票'].includes(c)))
                    .map(row => row.map(c => trip.passengers.includes(c) ? '[配置乘车人]' :
                        /^(成人|成人票|儿童票|学生票|二等座|一等座|商务座|无座|居民身份证|\\d{1,2}|[¥￥]?\\d+(?:\\.\\d{1,2})?元)$/.test(c) ? c : '[其他字段]'))
                };
            }""",
            {
                "train": offer.train,
                "origin": offer.origin,
                "destination": offer.destination,
                "passengers": self.config.journey.passengers,
            },
        )

    async def submit(self, offer: Offer) -> OrderReceipt | None:
        # Runner writes SUBMITTING durably before entering here. Any exception from
        # this point is an uncertain order outcome; it MUST NOT trigger blind retry.
        if datetime.now(SHANGHAI) >= self.config.execution.stop_at:
            return None
        await self.verify_form(offer)
        await (await self.one("submit")).click()
        try:
            await self.page.locator(self.selectors["review"]).wait_for(state="visible")
            await self.verify_review(offer)
            if datetime.now(SHANGHAI) >= self.config.execution.stop_at:
                return None
            await (await self.one("confirm")).click()
            await self.page.get_by_text(
                re.compile("席位已锁定|订票成功|订单提交成功")
            ).first.wait_for(state="visible")
        except (PlaywrightTimeoutError, NeedsAttention) as exc:
            reason = str(exc) if isinstance(exc, NeedsAttention) else "等待官方确认页面超时"
            log.warning("提交暂停：%s；已保留浏览器页面，不会重复提交", reason)
            try:
                log.info(
                    "确认页结构：%s",
                    json.dumps(await self.checkout_diagnostics(offer), ensure_ascii=False),
                )
            except Exception:
                log.warning("无法读取确认页结构，请从远程桌面检查")
            return None
        # Success text alone is insufficient; independently inspect the official order.
        return await self.reconcile(offer)

    async def open_orders(self) -> str:
        await self.page.goto(ORDER_URL, wait_until="domcontentloaded")
        incomplete = self.page.locator(self.selectors["order_tab"]).get_by_text(
            "未完成订单", exact=True
        )
        try:
            await incomplete.wait_for(state="visible")
            if await incomplete.count() != 1:
                raise NeedsAttention("未完成订单入口有歧义")
            await incomplete.click()
        except PlaywrightTimeoutError:
            raise NeedsAttention("无法打开官方未完成订单，请检查登录状态") from None
        await self.detect_interruption()
        # Wait for a positive empty-state or a rendered order card, not a fixed sleep.
        try:
            await self.page.wait_for_function(
                "settings => { const state = (" + ORDER_STATE_JS + ") (settings); "
                "return state.active && (state.hasCards || state.empty); }",
                arg=self.order_settings(),
            )
        except PlaywrightTimeoutError:
            raise NeedsAttention("无法确认官方订单列表状态") from None
        return await self.page.locator("body").inner_text()

    def order_settings(self) -> dict:
        return {
            "tab": self.selectors["order_tab"],
            "cards": self.selectors["order_cards"],
            "empty": self.selectors["order_empty"],
            "emptyPattern": EMPTY_ORDER_PATTERN,
        }

    async def check_existing_orders(self):
        await self.open_orders()
        state = await self.page.evaluate(ORDER_STATE_JS, self.order_settings())
        if state["hasCards"]:
            raise NeedsAttention("账号存在未完成订单，请先在官方 App 处理")
        if not state["active"] or not state["empty"]:
            raise NeedsAttention("无法确认官方未完成订单为空，请人工核对")

    async def account_diagnostics(self) -> dict:
        """Only static labels and DOM structure; no cookies, form values or full HTML."""
        return await self.page.locator("body").evaluate("""root => {
            const visible = e => e.getClientRects().length;
            return {
                pathname: location.pathname,
                title: document.title,
                bodyLength: root.innerText.length,
                activeTabs: Array.from(root.querySelectorAll('#order_tab .active'))
                    .filter(visible).map(e => e.innerText.trim()),
                messages: Array.from(root.querySelectorAll('p')).filter(e =>
                    visible(e) && /^(网络|系统|请先登录|访问|操作|出错|服务|抱歉|页面)/.test(e.innerText.trim()) &&
                    e.innerText.trim().length < 100
                ).map(e => e.innerText.trim()),
                labels: Array.from(root.querySelectorAll('*')).filter(e =>
                    visible(e) && !e.children.length &&
                    /未完成|暂无|没有/.test(e.innerText || '') &&
                    e.innerText.trim().length < 100
                ).map(e => ({tag:e.tagName, id:e.id, class:e.className,
                    text:e.innerText.trim(), parentId:e.parentElement.id,
                    parentClass:e.parentElement.className,
                    grandparentId:e.parentElement.parentElement?.id,
                    grandparentClass:e.parentElement.parentElement?.className})),
                containers: Array.from(root.querySelectorAll('[id]')).filter(e =>
                    visible(e) && /order|Order|empty|Empty|train|Train/.test(e.id)
                ).map(e => ({tag:e.tagName, id:e.id, class:e.className})),
                tables: Array.from(root.querySelectorAll('table')).filter(visible).map(e => ({
                    id:e.id, class:e.className, parentId:e.parentElement.id, parentClass:e.parentElement.className,
                    headings:Array.from(e.querySelectorAll('th')).map(c => c.innerText.trim()),
                    rows:Array.from(e.querySelectorAll('tr')).filter(visible).map(r => ({
                        id:r.id, class:r.className,
                        cells:Array.from(r.querySelectorAll('td')).map(c => ({
                            class:c.className,
                            children:Array.from(c.children).map(x => ({tag:x.tagName,id:x.id,class:x.className})),
                            textLength:c.innerText.trim().length
                        }))
                    }))
                }))
            };
        }""")

    def match_order_card(self, card: dict, offer: Offer) -> OrderReceipt | None:
        rows = card.get("rows", [])
        names = []
        places = []
        total = Decimal(0)
        if len(rows) != len(self.config.journey.passengers):
            return None
        for row in rows:
            if len(row) != 5:
                return None
            trip, passenger, seat, fare, status = row
            # The route has text nodes around an icon, not separate station elements.
            # Preserve those text-node boundaries and also check the entire route line.
            route_line = re.sub(r"\s+", "", trip["text"].splitlines()[0])
            if route_line not in {
                f"{offer.origin}{offer.destination}{offer.train}",
                f"{offer.origin}→{offer.destination}{offer.train}",
            }:
                return None
            try:
                self.verify_trip("\n".join(trip.get("segments", [])), offer)
            except NeedsAttention:
                return None
            departure = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*开", trip["text"])
            if not departure or departure.groups() != (
                str(offer.travel_date),
                offer.departure.strftime("%H:%M"),
            ):
                return None
            passenger_lines = [x.strip() for x in passenger["text"].splitlines() if x.strip()]
            matched = [
                name for name in self.config.journey.passengers if passenger_lines.count(name) == 1
            ]
            if len(matched) != 1:
                return None
            names.extend(matched)
            seat_lines = [x.strip() for x in seat["text"].splitlines() if x.strip()]
            if len(seat_lines) != 2 or seat_lines[0] != offer.seat:
                return None
            place = re.fullmatch(r"\d{1,2}\s*车\s*\d{1,3}[A-F]?\s*号?", seat_lines[1])
            if not place or status["text"].strip() != "待支付":
                return None
            places.append(re.sub(r"\s+", "", place.group()))
            price = re.fullmatch(
                r"成人票\s+(\d+(?:\.\d{1,2})?)\s*元(?:\s+\d+(?:\.\d+)?折)?", fare["text"].strip()
            )
            if not price or Decimal(price.group(1)) <= 0:
                return None
            total += Decimal(price.group(1))
        if sorted(names) != sorted(self.config.journey.passengers) or len(set(places)) != len(
            places
        ):
            return None
        if total > self.config.preferences.max_total_price:
            return None
        return OrderReceipt(
            None, str(total), len(names), tuple(places), datetime.now(SHANGHAI).isoformat()
        )

    async def reconcile(self, offer: Offer) -> OrderReceipt | None:
        await self.open_orders()
        cards = await self.page.locator(self.selectors["order_cards"]).evaluate_all(ORDER_ROWS_JS)
        # Ambiguous/multiple unpaid orders always require human inspection.
        return self.match_order_card(cards[0], offer) if len(cards) == 1 else None
