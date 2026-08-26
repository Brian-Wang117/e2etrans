# 豆包端到端语音 Demo

网页模拟外呼通话：测试人员说中文 → 豆包端到端模型直接理解中文音频 → 输出英文客服文本 + 英文语音（24kHz PCM）。Qwen `qwen-flash` 仅作为双语字幕翻译旁路，只翻译最终文本，不阻塞语音首包。

在单通链路之上，已实现**批量外呼工作台**（子系统 2）：导入客户清单 → 逐客自动外呼 → AI 端到端语音对话 → 结果看板与复盘。设计与实现依据见下文「批量外呼工作台」一节。

实现依据：`docs/superpowers/plans/2026-08-02-doubao-e2e-voice-demo.md` 与 `docs/superpowers/specs/2026-08-02-doubao-e2e-voice-demo-design.md`，参考脚本位于 `demo/`。

## 运行

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

服务监听 `HOST:PORT`（`.env.test` 中为 `127.0.0.1:8765`），浏览器打开 `http://127.0.0.1:8765/`。

## 配置

- `.env.test`：凭证层（豆包 APP_ID/ACCESS_KEY/APP_KEY、DASHSCOPE_API_KEY、HOST/PORT），勿提交到自己的仓库。
- `.env`：开关层，加载顺序为 `.env.test` → `.env`，同名键 `.env` 优先。启用实时链路需要：

  ```
  REALTIME_PROVIDER=doubao
  QWEN_SUBTITLE_ENABLED=true
  ```

- 启用批量外呼工作台需要在 `.env` 中设置 `OUTBOUND_ENABLED=true`（可选 `OUTBOUND_SCENARIO`，默认 `outbound_default`）。配置了 `DASHSCOPE_API_KEY` 时会为每位客户生成个性化开场白与业务背景，未配置则自动降级为通用话术。

- 真实进程环境变量优先级最高，可覆盖一切。
- `DOUBAO_MODEL` 留空则不传给豆包（使用服务端默认模型）；`DOUBAO_TTS_SPEAKER` 若不以 `en_` 开头仅记录警告（当前 `.env.test` 为中文音色，如需英文语音请替换为英文音色）。

## 使用流程

1. 选择场景（产品回访 / 快递地址确认 / 满意度调研），点击「开始通话」。浏览器会请求麦克风权限；拒绝后仍可用文本输入。
2. 点击「按住说话」开始收音（push-to-talk），再次点击提交本轮语音；说话期间若客服正在播报，会先打断（ClientInterrupt）。
3. 左侧实时显示 ASR 识别、客服英文文本与双语字幕；音频经 WebSocket 下行实时播放。
4. 点击「挂断」结束会话，自动进入复盘面板：话轮文本、翻译、评分、导出 JSON、删除。

## 批量外呼工作台（本次更新）

浏览器打开 `http://127.0.0.1:8765/workbench`（需 `OUTBOUND_ENABLED=true`）。该页面同时充当**调度看板**与**SIP 桥接执行器**：

1. **导入清单**：上传 CSV/XLSX，系统解析并预览（电话列自动识别，手机号格式校验），确认后生成批次；
2. **开始批量**：调度器按行序逐客外呼——桥接页通过 JsSIP 拨号，接通后把电话音频接入豆包端到端会话，AI 语音经 SIP 下行送达对方；
3. **停止/续打**：停止为优雅语义（打完当前这通再停）；停止或重启后可「开始批量」续打剩余待呼叫客户；
4. **结果看板**：实时进度、客户状态（待呼叫/进行中/已完成/失败）、通话时长、结论与原因，展开行可查看完整对话记录并在线编辑结论；
5. **导出清单**：一键导出当前批次客户清单（utf-8-sig CSV，Excel 直接打开不乱码），包含原始导入列 + 拨打号码/状态/结果/原因/时长/完成时间。

本次更新的要点：

- **状态机 + 动作指令架构**：`BatchScheduler` 为纯状态机只产出动作，`BatchRunner` 执行 I/O，便于离线测试；
- **00 前缀自动重拨**：直拨失败（接通前）自动以 `00+号码` 重拨一次，适配非上海号码的线路出局规则，对调度器透明；
- **可靠性加固**：WebSocket 发送全部带 5 秒超时防僵尸连接拖死；驱动任务捕获 `CancelledError` 自动落终态；停止接口对死亡任务有强制兜底；服务重启时把 running 批次置 stopped、进行中客户重置为待呼叫；
- **个性化开场白**：按客户字段（姓名/城市/订单/会员等级/备注）逐客生成开场白与业务背景，AI 服务不可用时降级不阻塞外呼。

真机调试的问题记录与解决方案见 `docs/superpowers/traces/2026-08-21-batch-outbound-workbench-debug-trace.md`。

## 本轮更新（2026-08-24）

- **外呼通话打断（barge-in）**：修复真机测试中客户说话无法打断 AI 播报的问题。流式模式下桥接页从不发 `input_audio.commit`，现以 ASR 识别文本作为打断信号，且 **interim（中间结果）一出现就触发**——客户刚开口即停声，不再等到说完；网关取消当前生成并下发 `response.cancelled`，桥接页清空播放缓冲并丢弃晚到的残留音频块（按 response_id 记账），防止被打断的句子"复活"；
- **按性别交叉音色**：从导入清单的性别列识别客户性别（兼容「性别/性別/gender/sex」列名与 男/女/male/female/m/f 取值），男客户听女声、女客户听男声。音色在 `.env` 配置 `OUTBOUND_TTS_SPEAKER_MALE` / `OUTBOUND_TTS_SPEAKER_FEMALE`，未配置时回退默认 `DOUBAO_TTS_SPEAKER`；
- **移除保存语音功能**：不再把通话音频落盘为 WAV，删除 `/audio` 回放端点、复盘面板播放器与 `data/audio` 写入逻辑（历史文件仍保留，可自行删除）；话轮文本与翻译记录不受影响；
- **音色复刻（真人客服声音克隆）**：工作台新增「音色复刻与选择」面板——对照内置中性朗读文本（约 230 字/45 秒）麦克风采样，经服务端代理（密钥不出服务端，`/api/voice-clone/*`）上传到火山 Seed-ICL 2.0 训练，轮询就绪后存入浏览器 localStorage。选定复刻音色后整批外呼统一使用该音色（优先于性别交叉），并自动为 persona 追加防复读采样文本的提示。槽位（`S_` 开头）从火山控制台音色库获取，上游错误码映射为人话（如 1109 → 对照文本重录、1123 → 槽位上传次数超限）。设计见 `docs/superpowers/specs/2026-08-24-voice-clone-design.md`。

## 本轮更新（2026-08-26）

- **外呼模板管理（toB）**：新增 `http://127.0.0.1:8765/templates` 页面（工作台头部入口），B 端使用者可录入并管理多套外呼配置模板：公司名称、业务背景与卖点、开场白（支持 `{name}`/`{title}` 占位符，留空按客户姓名自动生成）、固定话术（行编辑表格，保存前即时校验：回复 20-40 字、结束通话必须含“再见”、禁写死价格、优先级 0-10）与 AI 人设（名字 ≤20 字、语气风格）。批次确认时绑定模板（未选择用默认模板，首次启动自动种入一条镜像 `.env` 配置的“内置默认”），拨号链路按模板注入业务背景（此时跳过 LLM 客户个性化）、开场白与人设；模板话术与内置 3 条合规话术（身份否认/投诉免打扰/听不清，始终生效不可删）合并进脚本匹配器。已确认批次保留模板名快照；模板删除后历史批次可读、未拨客户降级回 `.env` 老行为。REST：`/api/templates` CRUD + `/{id}/default`。设计见 `docs/superpowers/specs/2026-08-26-campaign-templates-design.md`。

## 测试

```powershell
.venv\Scripts\Activate.ps1
pytest -q
```

离线测试覆盖（228 passed）：豆包二进制协议精确字节向量与往返、会话状态机、浏览器协议校验、假豆包驱动的网关端到端流程、批次导入/导出/调度器/存储、工作台 hub 与装配接线、音色复刻代理与端点错误映射、外呼模板存储/校验/批次绑定/拨号载荷与话术合并。不需要真实网络凭证。

## 目录结构

```
app/
  config.py              配置加载（.env.test → .env 分层，密钥不进 repr）
  storage.py             SQLite 会话/话轮存储
  main.py                FastAPI 应用、REST 复盘 API、/ws/realtime
  realtime/
    doubao_protocol.py   豆包 v3 二进制帧编解码（含 gzip 响应）
    doubao.py            豆包 WebSocket 客户端（鉴权/事件/收发循环）
    qwen.py              Qwen 字幕翻译旁路（compatible-mode）
    browser_protocol.py  浏览器 JSON envelope 校验
    state.py             序号/generation/打断 状态机
    persistence.py       单 worker 保序落盘 + 翻译旁路
    gateway.py           会话编排（TaskGroup 三循环）
  batch/                 批量外呼调度器
    import_parser.py     CSV/XLSX 清单解析与校验
    scheduler.py         纯状态机（产出动作，不做 I/O）
    runner.py            动作执行与驱动循环（含停止兜底）
    hub.py               工作台 WebSocket 广播（发送带超时）
    personalizer.py      逐客个性化开场白/背景生成（可降级）
    events.py            动作与事件类型定义
  static/                前端（pcm/worklet/realtime-audio/realtime/UI）
    workbench.html/js    批量外呼工作台（看板 + SIP 桥接执行器）
    templates.html/js    外呼模板管理（录入/编辑/删除/默认模板）
    voice-clone.js       音色复刻：45 秒采样/上传/训练轮询/本地音色存储
    sip-bridge.js        JsSIP 呼叫封装（含 00 前缀重拨）
run.py                   uvicorn 入口
tests/                   pytest 离线测试
data/mock_customers.csv  模拟客户清单（请用 VS Code 编辑，勿用 Excel 直接保存）
docs/superpowers/traces/ 真机调试问题记录
```
