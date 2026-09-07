# 12306 Ticket Runner V2 · 候车控制台

个人使用的网页控制台：在页面选择行程、发车时段、车次和席别，内嵌实时流程、日志及官方浏览器扫码窗口。支持 Docker，原有配置文件与命令行仍可使用。

支持两种查询引擎：`browser` 保留网页查询，`api` 直接读取官网余票接口，**下单仍由浏览器完成**。网页“查票方式”可切换，原配置默认不变。接口版不是全接口下单；SSE 仅推送本地状态，不会因此提高官网查询频率或获得优先票源。

**曾按用户授权在官网生成真实待支付订单，未付款；尚未证明全自动抢稀缺票。** 当次最终确认由浏览器接管完成，旧版自动回查未完成。目前通过 143 项自动化测试；生产适配器另有 9 个离线 Chromium 页面场景及完整断网浏览器下单/回查/重启防重测试，混合模式也通过了生产解析器与浏览器的合成全流程。新回查逻辑仍缺一次真实订单的完整验证，本轮接口重构没有提交真实订单。不能把测试通过、点击成功或有票时下单等同于抢票成功率提升。[原可行性调研](docs/feasibility.md)和[接口可行性实测](docs/api-feasibility.md)记录了证据和限制。

支持成人直达有座票、多日期、多车次/席别优先级、严格上下车站和总预算。暂不自动购买学生票、儿童票、卧铺、无座，不自动付款，不自动支付候补。无票时建议同时在官方 App 提交候补。

## 快速开始

```bash
cp config.example.yaml config.yaml
cp .env.example .env
# 编辑 config.yaml 的真实行程、成人乘车人、起售时间和预算。
# 初始配置保持 auto_submit: false；网页启动时另行明确确认。
docker compose build
docker compose run --rm -e ENABLE_DESKTOP=0 runner --config /config/config.yaml validate
docker compose up -d
```

打开 **http://127.0.0.1:8080**。启动容器、打开页面、保存偏好都不会启动抢票；服务器重启也不会自动恢复任务。

1. 选择出发/到达站、出行日期区间、发车时段、成人乘车人、席别和总预算。
2. 点“查询可选车次”，勾选希望监控的车次；**无票车次也可选择**。同一车次适用于整个日期区间，按勾选顺序优先。站名支持本地自动补全，异常时可在高级设置填写官方代码。
3. 保存后核对页面计划。只读模式发现匹配票就停止；要创建订单，选择“有票自动提交”，明确勾选授权后再开始。
4. 右侧内嵌小窗实时显示登录、循环查票、选人选席别、提交、回查等事件；下方“官方浏览器”提供扫码与人工核验入口。
5. 确认生成待支付订单后停止，不付款。结果不确定时停止并阻止重复提交；必须先去官方核对。关闭网页不会停止已经启动的任务，请点击“暂停任务”。

席别是二等座、一等座等；暂不支持指定靠窗/过道或具体座号，车厢、座号由 12306 分配。页面保存在私人数据卷 `/data/ui-config.yaml`，单次任务配置另存 `/data/tasks/`，均不改写宿主机只读的 `config.yaml`。请勿上传这些文件或数据卷。

远程部署时，在自己的电脑执行：

```bash
ssh -N -L 8080:127.0.0.1:8080 -L 6080:127.0.0.1:6080 user@your-server
```

然后访问本机 `127.0.0.1:8080`。两个端口都只绑定服务器回环地址；不要直接暴露到公网。控制台依靠本机/SSH 信任边界，并做 Host、同源、操作令牌校验，不是多人公网服务。

示例日期不是动态日期，起售时刻也不是所有车站通用值。请按官方查询填写。车站名称/代码从官方查询页面 URL 的 `fs`、`ts` 参数取得，例如 `fs=深圳北,IOQ`。配置中乘车人必须已经存在于该 12306 账号中，并完成核验。

## 官方二维码登录

V2 推荐直接在控制台点“打开扫码登录”，在内嵌浏览器扫码。以下命令行操作需先 `docker compose stop runner`，避免与控制台抢占浏览器和数据目录：

```bash
docker compose run --rm --service-ports runner --config /config/config.yaml --data-dir /data login
```

本机打开 `http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale`，用官方 12306 App 扫描浏览器内的二维码，按手机提示确认。二维码失效时脚本刷新；若有额外验证，在同一远程浏览器中手动完成。登录成功后命令退出，会话保存在数据卷。

在远程服务器部署时，先在你自己的电脑建立隧道：

```bash
ssh -N -L 6080:127.0.0.1:6080 user@your-server
```

然后仍打开本机 `127.0.0.1:6080`。Compose 只把远程桌面绑定到服务器本机地址；不要改成直接公开浏览器控制端口。

二维码还会写入数据卷的 `/data/login-qr.png`。它属于登录凭据，不会上传到通知渠道，登录成功后删除。会话可能失效，Docker 持久化不等于永久免登录。

浏览器目录之外，程序还将当前专用浏览器的官方 Cookie 原子保存到 `/data/session-cookies.json`（权限 `600`），启动时恢复，以支持会话型 Cookie 跨进程使用。不会修改有效期，也不能延长官网服务端登录有效期。该文件同样是敏感凭据，只留在私人数据卷，勿分享或上传。

恢复时先访问需要登录的官方订单页，确认可见“退出”入口；不能仅凭本地存在状态文件判定登录成功，也不直接重新打开扫码页。若官方会话已失效，仍需人工扫码。

## 查票验证与正式运行

只读查询配置中的第一个日期，输出经过筛选的候选项，不登录、不点击预订、不提交订单：

```bash
docker compose run --rm runner --config /config/config.yaml --data-dir /data probe
```

**`probe` 查询成功仅验证查询链路。** 如需验证登录后的订单页面，先使用自己的账号扫码；页面不匹配会停止并报告需要适配，不猜测点击。

```bash
docker compose run --rm --service-ports runner --config /config/config.yaml --data-dir /data check-account
```

`check-account` 仅检查登录和未完成订单，不点击预订、不下单。有未完成订单时会要求先处理。

订单页适配失败时，可在命令末尾加 `--diagnostics`，输出少量可见提示和元素结构；不导出 Cookie、输入框值或整页内容。

`probe --all-dates` 会按配置间隔依次查询全部日期。`check-checkout` 会选择首个符合配置的候选项，点击“预订”并核对乘车人、成人票、席别和报价，但**不点击“提交订单”或最终“确认”**；用于登录后的表单联调，即使配置 `auto_submit: true` 也不提交。

不使用网页、单独启动命令行计划任务（先停控制台）：

```bash
docker compose stop runner
docker compose run --rm --service-ports runner --config /config/config.yaml --data-dir /data run --keep-alive
```

`auto_submit: false`：运行到发现匹配车票后进入 `DRY_RUN`，不点击预订。

`auto_submit: true`：允许按配置创建真实待支付订单。订票提交仍可能被官网核验/页面变化中断，付款必须在官方 App 完成。没有完成订单核对，不报告成功。

同一账号只使用一个实例/数据卷。不要用多个不同数据卷同时登录同一账号。执行器对单个数据目录加排他锁，不能跨不同机器协调账号。

查询优先级：日期配置顺序 → 车次配置顺序 → 席别配置顺序 → 发车时间 → 价格。所有日期共用串行的“查询开始到开始”最小间隔，查询加载时间计入其中；不并发、不补发积压请求。“有”表示票量未明确，最终是否够所有人仍由官网确认。预算是所有乘车人合计；未知票价不参与自动购买。

## 低延迟与速度统计

### 接口查票与只读验证

在网页选择“接口查票 · 浏览器复核下单”，保存后生效；仅保存不会开始抢票。也可以在配置顶层写 `query_backend: api`。

首次初始化时，从当前官网页面读取查询入口，不固定猜测 `queryG/queryU` 等地址。后续同一会话直接取余票 JSON，按官网当前字段含义解析车次、席别、库存与价格；不加载查询页面，不逐车次请求票价。接口客户端与专用浏览器共用 Cookie，令牌和原始余票行不输出到日志。接口出错遵循原有退避，不自动切换引擎掩盖错误。

命中后会重新打开官方查询页，确认相同日期、车次、站点、时刻、席别、人数与预算仍可满足，再进入原有预订流程。页面复核未通过就不预订；进入提交后结果不明就停止，不换通道重复下单。此模式减少的是等待无票期间的页面开销，不能保证一次命中后的下单耗时更短。

```bash
# 匿名接口验证：不打开浏览器、不读取账号会话、不进入预订
uv run ticket-runner --config config.yaml probe-api

# 复用专用会话，并按配置间隔顺序对照接口与网页；先停同数据目录的控制台
docker compose stop runner
docker compose run --rm --service-ports runner --config /config/config.yaml --data-dir /data probe-api --with-session --compare-browser --all-dates
docker compose up -d runner
```

`probe-api` 即使配置了 `auto_submit: true` 也只读；不会注册抢票任务或修改旧订单状态。`--with-session` 只检测已有会话，不自动扫码、读取乘车人或提交订单。`--compare-browser` 的顺序样本用于核对解析结果，不是可靠的速度排行榜。

### 查询节拍

示例配置采用 5 秒查询启动间隔（配置默认值仍为 30 秒）。两个日期轮询且单次查询少于 5 秒时，同一日期约每 10 秒检查一次；不是每个日期每 5 秒查询。命中后直接选人、选席别、检查并提交，不等待下一轮。页面结果以 50 毫秒本地 DOM 检查就绪，**不代表 50 毫秒请求官网**。

```bash
docker compose run --rm -e ENABLE_DESKTOP=0 runner --data-dir /data timings
```

显示本地阶段平均/P95/最大耗时和结果类别，不访问官网。速度设计、旧/新节拍对照和断网全流程测试见 [性能说明](docs/performance.md)。不能保证快过每个手动用户，验证码、网络、官网处理和库存仍决定结果；网站限流时自动退避。

## 状态与恢复

```bash
docker compose exec runner ticket-runner --data-dir /data status
```

| 状态 | 含义 |
| --- | --- |
| WAITING / QUERYING / BACKOFF | 等待、查询、失败退避 |
| DRY_RUN | 演练发现符合条件的票，未点击预订 |
| ATTENTION | 登录、乘车人或页面结构需要人工处理 |
| SUBMITTING / UNKNOWN | 已开始提交或结果不明，禁止再次自动下单 |
| ORDER_CREATED | 已核对到符合配置的待支付订单，需你核对付款 |
| EXPIRED / DONE | 已过期或你已标记处理完毕 |

只回查已经记录的提交意图，不查余票、不再次提交、不付款：

```bash
docker compose run --rm --service-ports runner --config /config/config.yaml --data-dir /data check-order
```

运行前先停止同一数据卷的其他执行器。只有唯一待支付订单的行程、出发时刻、逐人成人票、席别、已分配席位和总预算均可验证时才更新 `ORDER_CREATED`。当前官网列表不显示订单号时，保存核对凭据 `receipt`，`order_id` 保持空值，不编造号码。该状态记录当时核对成功，不保证订单后来未超时；当前有效性以官网为准。

V2 控制台重启后不自动执行任何账号操作，`SUBMITTING`、`UNKNOWN` 仍会阻止新任务。网页可点“只回查订单”；命令行 `run` 的恢复路径也只核对上次订单，即使查不到也不会直接重复提交。其他任务也会被尚未处理的订单阻止。

需要恢复时先停止执行器，在官方 App 检查是否已经有票/待支付订单。只有确认不存在订单才能使用 `no-order`：

```bash
docker compose stop runner
docker compose run --rm -e ENABLE_DESKTOP=0 runner --data-dir /data resolve --task-id trip-2026-10-01 --outcome no-order
docker compose up -d
```

如果已经付款或任务不再需要，将 `--outcome no-order` 改为 `--outcome done`。不要把尚不确定的订单标记为 `no-order`。

同一个 `task_id` 的配置不可静默变更，修改行程/模式后应在处理完旧任务的订单后使用新的 `task_id`。网页为每次明确启动生成独立任务 ID；命令行需自行设置。控制台运行时可直接使用网页登录和回查；使用独立命令行操作前先停容器。

## 通知

`.env` 中的 `TICKET_NOTIFY_WEBHOOK` 应指向你控制的 HTTPS 接收端，接收 JSON：

```json
{"title":"12306 ORDER_CREATED","body":"请在官方 App 核对并付款","text":"12306 ORDER_CREATED\n请在官方 App 核对并付款","task_id":"your-trip"}
```

这是通用 Webhook 格式，不保证直接兼容各家机器人。接收端可转发到你的手机。通知不包含姓名、身份证、登录二维码、Cookie 或支付凭据。失败通知留在 SQLite 发件箱中并退避重试；未配置 Webhook 时只记录本地日志并保留待发送记录。

## 本地开发和离线验证

```bash
uv sync
uv run ticket-runner --config config.example.yaml validate
uv run ticket-runner demo
uv run pytest -q
uv run ruff check src tests scripts docker
uv run ticket-runner --config config.example.yaml --data-dir data/preview serve --demo --port 8081
```

`demo` 完全离线，模拟“提交后响应丢失 → 重启 → 核对订单”，最终总提交次数应为 1。它不启动浏览器，也不会发送 Webhook。

`serve --demo` 是独立的网页演示：打开 `http://127.0.0.1:8081`，模拟先无票、再匹配并提交，所有记录位于指定目录下的 `ui-demo/`。不使用真实账号、数据库、浏览器或 Webhook；模拟结果不代表真实购票成功。实时流程也可以通过 `/monitor` 独立打开。

车站补全表来自 [12306 官方公开车站表](https://kyfw.12306.cn/otn/resources/js/framework/station_name.js)，于 2026-09-07 收录 3384 站；这不是余票数据接口。站点更新后需要更新静态表，或手填正确站名/代码。

本机运行需要安装项目匹配的浏览器：

```bash
uv run playwright install chromium
uv run ticket-runner --config config.yaml --data-dir data login
uv run ticket-runner --config config.yaml --data-dir data probe
```

## 页面适配与限制

查询/登录/预订表单、确认弹窗和待支付订单卡片选择器来自 2026-09-06 官网观察；新待支付解析逻辑已用观察到的结构完成合成浏览器验证，尚待新的真实订单验证。结构不匹配、无法确认报价或人数时会暂停，不会无条件点击提交。

预订行程标题为 `#ticket_tit_id`；最终确认窗口为 `#content_checkticketinfo_id`，不能把其中只有乘车人数据的 `#check_ticketInfo_id` 当成整个确认窗口。当前官方确认弹窗未展示合计票价，程序先逐行核对姓名、人数、成人票与席别，再从仍在页面上的已选席别价格逐人合计、检查总预算；缺少报价仍会暂停。生成订单后请在官方页面核对实际应付金额，不自动付款。

未完成订单的标签和空列表提示已用用户账号验证：必须选中 `#order_tab` 的“未完成订单”，并在可见的 `.order-empty .empty-txt p` 中识别到明确空状态。隐藏提示、其他标签页的提示和单纯没有订单元素，都不作为“无未完成订单”的依据。

可以通过 `--selectors selectors.json` 覆盖 `src/ticket_runner/browser.py` 中的已定义选择器。覆盖后仍会执行车次、日期、站点、成人票、乘车人和预算检查。不要通过扩大选择器范围来绕过检查。

不要将配置、浏览器会话、二维码或数据库提交到 Git。它们已在忽略规则中；Docker 构建上下文也排除了这些文件。更换服务器会重新受到官方登录/访问检查，没有“换到服务器就更快”的保证。

查票和自动点击只能减少人工操作，不能改变官方库存或候补顺序。官方限制未经认可的自动化访问，自用和低频不等同于官方许可。具体依据见调研文档。
