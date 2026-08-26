# 子系统 2：批量外呼调度器 设计文档

- 日期：2026-08-20
- 状态：待评审
- 前置：子系统 1（单通对话引擎）已实现并通过全量测试
- 需求来源：`docs/superpowers/plans/voice_outbound_bot_requirements.md` 第二章（需求 2.1–2.5）与第三章的结果落库要求（需求 3.2–3.3）

## 1. 范围与决策记录

### 1.1 本子系统的范围

- 客户清单导入（CSV + Excel）与电话列自动识别
- 批次与客户的数据模型、客户资料在线编辑
- 逐客个性化生成（规则开场白 + LLM 业务背景）
- 串行批量调度器（含失败不中断、10 秒间隔、60 秒活动超时、停止/续打）
- 实时状态推送通道与最小工作台界面（含通话后的对话记录与分析展示）

### 1.2 明确不做（YAGNI）

- 失败自动重拨（运营可编辑后手动重拨单个客户）
- 多批次并行调度
- 定时自动启动批次
- 结果导出（子系统 4）、话术热更新（子系统 3）、复盘大屏（子系统 4）

### 1.3 关键决策（已与用户确认）

| 决策点 | 结论 |
|---|---|
| 拨号自动化形态 | A2 全自动，但载体为**常驻工作台页面**（普通 Chrome 打开一次，服务端经 WebSocket 指挥拨号），不用 Playwright 无头浏览器 |
| 导入格式 | CSV + Excel（新增 openpyxl 依赖） |
| 电话号码列 | 自动识别 + 导入预览界面确认/改选 |
| 界面程度 | 含最小工作台界面（导入预览、启停、进度、客户状态表、在线编辑） |
| 页面布局 | 新建独立页面 `/workbench`，现有 `/` 面板不动 |
| 停止语义 | 打完当前这通再停，界面显示"停止中…" |

## 2. 总体架构

服务端当"大脑"（调度器），工作台页面当"手"（拨号执行器 + 运营看板），两者经新增工作台 WebSocket 双向通信。

```
┌─ 服务端（FastAPI，新增 app/batch/ 包）─────────────┐
│  import_parser   清单解析（CSV/Excel）+ 电话列识别    │
│  personalizer    逐客个性化（规则开场白 + LLM 业务背景）│
│  scheduler       批次调度状态机（纯决策，动作指令模式， │
│                  复用子系统1 CallDirector 的设计手法） │
│  events          CallEventBus 进程内事件总线           │
└──────────────┬─────────────────────────────────────┘
               │ /ws/workbench（新增通道，双向）
               │  ↓ 下行广播：bridge.dial / batch.progress ...
               │  ↑ 上行事件：bridge.call_connected / call_failed ...
┌──────────────┴───────────────────────────────────────┐
│ 工作台页面 /workbench（新页面）                        │
│  · 运营看板：导入、启停、进度、客户状态表、对话记录      │
│  · 桥接执行器：复用现有 sip-bridge.js + realtime.js +   │
│    realtime-audio.js，收到指令自动完成                  │
│    注册→拨打→建AI会话→通话→挂断                        │
└──────────────────────────────────────────────────────┘
```

关键机制：

1. **通话结束判定回路**：网关（子系统 1）在通话结果落库时经 `CallEventBus` 发布事件，调度器订阅后决定倒计时 10 秒拨下一个；SIP 层失败（无人接听/拒接）由桥接页上报 `bridge.call_failed`，同样触发"下一位"。
2. **60 秒活动超时**：调度器维护活动计时，网关每产生一句新对话（用户或客服任意一句终稿）即发布 `activity` 事件重置计时；超时视为异常，指令桥接页挂断，记失败并继续下一位。
3. **单路串行**：一个批次同一时刻只有一通电话，规避线路并发问题。
4. **现有系统零破坏**：`/` 现有面板完全不动；子系统 1 的网关只加事件发布挂接点。

## 3. 数据模型

延续子系统 1 的 SQLite 模式：`CREATE TABLE IF NOT EXISTS`，迁移用 `PRAGMA table_info` 检查 + `ALTER TABLE`。

### 3.1 `batches`（批次表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 批次号，格式 `B-YYYYMMDD-xxxx`（日期 + 4 位随机） |
| `created_at` | TEXT | 上传时间（ISO 8601） |
| `total` | INTEGER | 总人数 |
| `done` | INTEGER | 已结束人数（`已完成` 或 `失败` 均计 +1，需求 3.3 的进度口径） |
| `columns` | TEXT | 原始列名清单（JSON 数组） |
| `phone_column` | TEXT | 运营确认过的电话号码列名 |
| `status` | TEXT | `draft` / `ready` / `running` / `stopped` / `completed` |

### 3.2 `customers`（客户表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增主键 |
| `batch_id` | TEXT | 所属批次 |
| `row_number` | INTEGER | 原始行号（从 1 起，按原始表格计，跳过的空行也占号） |
| `raw_data` | TEXT | 整行原始数据（JSON，保留上传时全部列；在线编辑改这里） |
| `phone` | TEXT | 导入时从 phone_column 提取的电话号码 |
| `status` | TEXT | `待呼叫` / `进行中` / `已完成` / `失败`（需求 3.4 四态） |
| `result` | TEXT | 营销结果（感兴趣/不感兴趣/中立，可空） |
| `reason` | TEXT | 判定原因（可空） |
| `duration_seconds` | REAL | 通话时长（可空） |
| `session_id` | TEXT | 关联实时会话 ID（用于拉取 turns 对话记录，可空） |
| `finished_at` | TEXT | 完成时间（可空） |

索引：`(batch_id, row_number)` 唯一；`(batch_id, status)` 普通索引。

### 3.3 `call_results` 扩展

新增可空列 `customer_id`（迁移添加，老数据不受影响），把子系统 1 的通话结果记录与批次客户绑定。

### 3.4 存储分工（对话数据与业务结论分离）

- 对话记录本身继续存在 `turns` 表（经 session_id 关联），不冗余存储
- `call_results` 是每通的原始上报（含子系统 1 的幂等保证）
- `customers` 是批次视角的汇总快照：导出表格（子系统 4）直接从 `customers` + `raw_data` 拼
- 通话结束后，对话记录与分析结论（结果/原因/时长）必须落到客户记录；工作台状态表点某客户可展开"客服：…/客户：…"逐行对话文本

## 4. 清单导入流程（需求 2.1）

### 4.1 `POST /api/batches/import`（multipart 上传）

处理管线四步：

1. **解析**：CSV 用标准库 `csv`（UTF-8；检测失败报错提示改用 UTF-8 另存）；Excel 用 `openpyxl`。第一行为列名；空行跳过但行号按原始表格计。
2. **电话列自动识别**（按优先级）：
   1. 列名含 `电话/手机/号码/phone/mobile/tel` → 直接命中
   2. 否则统计各列内容：去掉空格/横线后全为 11 位数字（或 3-4 位区号座机格式）的列 → 命中
   3. 都不满足 → 返回 `phone_column: null`，由预览界面手动指定后重新确认
3. **入库**：生成批次号 → 整批客户写入 `customers`（状态全部 `待呼叫`）→ `batches` 记列名、电话列、总人数，状态 `draft`。
4. **预览响应**：返回批次号、总人数、列名清单、前 3 条预览（含识别出的电话列标注）。运营在界面确认（`POST /api/batches/{id}/confirm`，可携带改选的 `phone_column`）后状态置 `ready`。

### 4.2 校验规则

- 单批上限 1 万行，超过报错不建批次
- 电话列为空的行跳过，计入响应的 `invalid_rows` 提示
- 同一批次内不去重
- 文件损坏/格式错误在解析阶段报错，不留脏批次

### 4.3 其他 REST API

| 方法 路径 | 说明 |
|---|---|
| `GET /api/batches/latest` | 恢复查看最近一次批次（需求 2.1） |
| `GET /api/batches/{id}/customers?page=&size=` | 拉取某批次全部客户（分页） |
| `PATCH /api/batches/{id}/customers/{cid}` | 在线编辑客户资料（需求 2.2，改 raw_data 字段，保存即生效） |
| `POST /api/batches/{id}/start` | 启动批量（幂等：已 running 返回当前进度） |
| `POST /api/batches/{id}/stop` | 停止批量（打完当前这通再停） |
| `GET /api/batches/{id}/customers/{cid}/transcript` | 拉取某客户对话记录（从 turns 格式化） |

## 5. 逐客个性化生成（需求 2.3）

呼叫每位客户之前即时生成，随 `session.start` 的 `opening_text` / `business_background` / `customer_id` 下发（子系统 1 已支持的通道）。

### 5.1 个性化开场白——规则生成，不走大模型

- 找姓名：候选列名 `姓名/名字/客户姓名/称呼/name/customer_name`，取第一个存在且非空的
- 找性别：候选列名 `性别/gender/sex`；值含"女"→女士，含"男"→先生；无性别列时尝试从称谓类字段（如"王女士"）兜底提取
- 拼接规则：
  - 有姓名有性别：`您好，请问是{姓名}{女士/先生}吗？`
  - 有姓名无性别：`您好，请问是{姓名}吗？`
  - 找不到姓名：`您好，请问是本机主人吗？`
- 开场白经 `say_hello` 直接 TTS 播报，不经大模型，保证首句最快

### 5.2 逐客业务背景——轻量 LLM 生成

- 输入：全局业务背景模板（`OUTBOUND_BUSINESS_BACKGROUND` 或默认）+ 该客户 `raw_data` 整行 JSON
- 独立的 `Personalizer` 类，复用终裁器同款 httpx + DashScope 客户端模式，模型用 Qwen-flash（与终裁器同配置）
- 提示词约束：≤300 字、只许使用给定资料、不许编造价格优惠
- 失败兜底：超时/报错退回全局模板背景，不阻塞拨打
- 结果缓存进 `customers.raw_data` 的特殊键 `_personalized_background`，重拨直接用，不重复花 token

### 5.3 性能

LLM 生成在"上一通结束 → 10 秒倒计时"窗口内并行完成；倒计时结束仍未生成完则直接用全局模板拨打。

## 6. 调度器状态机

`BatchScheduler` 是纯状态机，只产出动作指令；`BatchRunner`（挂在 FastAPI lifespan）执行 I/O。与子系统 1 `CallDirector` 同构，可完全单测。

### 6.1 批次状态流转

```
draft ──confirm──► ready ──start──► running ──┬─ 逐位循环 ──► completed
                                      │        │
                                      │        └─ stop ──► stopped（断点=下一个待呼叫，可续打）
                                      └─ 桥接离线 ──► paused（重连自动恢复）
```

注：`paused` 是调度器的**运行时状态**，不落库（库里仍是 `running`）；服务端重启时统一按 6.4 的规则置 `stopped`。

### 6.2 单客户循环（动作指令）

1. `Personalize(customer)`
2. `Dial(customer, opening_text, business_background)` → 经工作台 WS 发 `bridge.dial`，客户状态置 `进行中` 并广播
3. 等待结束，三个来源任一触发：
   - 事件总线 `call_finished`（子系统 1 正常上报）→ 客户 `已完成`
   - 桥接页 `bridge.call_failed`（SIP 失败）→ 客户 `失败` + 原因，不中断批次（需求 2.4-4）
   - 活动超时 60 秒 → `Hangup` 指令 + 客户 `失败`"通话异常超时"
4. 非最后一位 → `Wait(10)` 倒计时（进度广播给界面）→ 回到 1

### 6.3 CallEventBus（进程内事件总线）

极小的 asyncio 订阅/发布组件。发布方：子系统 1 网关（通话结果落库、每句新对话终稿）；订阅方：调度器。网关与调度器零耦合，只加发布挂接点。

### 6.4 并发安全

- 同一时刻只允许一个批次 running；启动新批次时若有 running 的先要求停止
- 停止语义：打完当前这通再停，界面提示"停止中…"
- 服务端重启：`running` 批次自动置 `stopped`，断点即下一个 `待呼叫` 客户，运营点"开始"续打
- start 幂等：已 running 直接返回当前进度

## 7. 实时推送（需求 2.5）

### 7.1 `GET /ws/workbench` 通道

- 下行广播（发给所有连接页面）：
  - `batch.progress`：第 N 位 / 共 M 位、批次状态
  - `customer.updated`：客户状态/结果/时长/原因
  - `countdown`：下一位倒计时秒数
  - `bridge.status`：桥接页在线/离线/通话中
  - `bridge.dial`：拨号指令（定向发给在线桥接页）
- 上行事件：`bridge.call_connected` / `bridge.call_failed`（含 JsSIP 错误码映射后的原因）/ `bridge.call_ended` / `bridge.status`
- 运营命令（启停）走 REST，WS 只做事件，职责清晰

### 7.2 WorkbenchHub

连接注册表；心跳 ping/pong；异常连接自动清理，不影响其他页面（需求 2.5-3）。调度器依赖该 Hub 判断桥接是否在线：无在线桥接页时禁止启动批次。

## 8. 工作台页面与桥接执行器

### 8.1 页面结构（`/workbench`，新文件 `workbench.html` + `workbench.js`）

复用现有 `sip-bridge.js` / `realtime.js` / `realtime-audio.js`，不复制代码。

```
┌ 线路与桥接状态条 ────────────────────────────────┐
│ SIP 配置(折叠，复用 localStorage 预填) [注册线路]   │
│ 桥接状态灯: ● 已就绪 / ● 未注册 / ● 通话中         │
├ 批次操作区 ──────────────────────────────────────┤
│ [上传清单] → 预览卡(批次号/人数/列名/前3条/电话列    │
│ 下拉改选) [确认导入]                               │
│ [开始批量] [停止]  进度: 第 N 位 / 共 M 位 ▓▓▓░     │
│ 当前: 正在呼叫 王女士 138****（倒计时 7s 后下一位）  │
├ 客户状态表 ──────────────────────────────────────┤
│ 行号 | 姓名 | 电话 | 状态 | 时长 | 结果 | 原因      │
│ （当前呼叫行高亮，WS 推送即时刷新；点行展开对话记录） │
└─────────────────────────────────────────────────┘
```

### 8.2 桥接执行器行为

1. 页面加载 → 连 `/ws/workbench` → 服务端感知"桥接在线"，允许启动批次（无桥接页时"开始批量"置灰并提示）
2. 收到 `bridge.dial` → 自动执行现有电话模式完整链路：确认注册 → `sip.dial(phone)` → 接通后建 AI 会话（scenario=outbound_default，携带 opening_text / business_background / customer_id，input_mode=audio）→ 音频轨交换。与现有 `startPhoneCall` 同一条已验证链路，驱动方从按钮换成 WS 指令
3. 通话生命周期事件上报：`call_connected` / `call_failed` / `call_ended`
4. 只拨出不接听；页面刷新重连后上报 `bridge.status`，调度器从 paused 恢复
5. 页面顶部提示：首次打开需人工点一次"注册线路"（浏览器安全策略要求用户手势解锁音频），之后全程自动

## 9. 错误处理矩阵

| 故障 | 处理 |
|---|---|
| SIP 拨打失败（无人接听/拒接/4xx） | 客户记 `失败` + 具体原因，10 秒后下一位，批次不中断 |
| 工作台 WS 断线（通话中） | 调度器置 `paused`；SIP 与 AI 会话在浏览器内独立存活，当前这通正常打完；重连后恢复调度；当前通话 60 秒内无活动事件则按超时失败 |
| 桥接页整个关闭 | 置 `paused`，其他页面显示"桥接已离线"；重新打开即恢复 |
| 服务端重启 | `running` 批次置 `stopped`，断点续打 |
| 个性化 LLM 失败 | 退回全局模板，不阻塞拨打 |
| 上传文件损坏/格式错 | 导入前校验，报错不建批次 |
| 重复点"开始" | 幂等返回当前进度 |

## 10. 测试策略

| 模块 | 手法 |
|---|---|
| `import_parser` | 单测：CSV/Excel 解析、电话列识别三路径、行号计数、空行跳过、上限校验 |
| `personalizer` | 单测：姓名/性别候选列组合、httpx.MockTransport 测 LLM、失败兜底 |
| `scheduler` | 单测（重头）：纯状态机喂假事件——串行推进、失败不中断、10 秒倒计时、60 秒活动超时、停止语义、断点续打、单批次互斥 |
| 存储扩展 | 单测：batches/customers CRUD、done 计数、call_results.customer_id 关联 |
| REST + WS | 集成测试：FakeWebSocket 驱动——导入预览确认、启动批次 → FakeBridge 收 dial → 回报结束 → 自动推进下一位、推送广播到多连接 |
| 回归 | 全量 pytest，子系统 1 的 105 条零回归 |

依赖变更：`requirements.txt` 新增 `openpyxl`。

## 11. 风险与真机验证点

1. **ocssaas 线路外呼手机号能力**：现有电话模式拨的是分机/测试号；批量拨打真实手机号需线路支持出局呼叫，真机验证第一步
2. **浏览器音频解锁**：工作台页面刷新后音频自动播放策略可能要求再次用户手势；页面需显著提示
3. **桥接页长时间挂机稳定性**：JsSIP 注册保活、内存增长需长时间实测
4. **60 秒活动超时与子系统 1 沉默超时的关系**：两者独立——沉默超时处理"接通后客户不说话"，活动超时处理"接通后整个会话卡死"；真机观察是否有误伤
