# 批量外呼工作台真机调试 Trace

- **日期**：2026-08-20 ~ 2026-08-21
- **范围**：子系统 2「批量外呼调度器」7 个模块实现完成后的真机联调
- **结果**：5 个问题全部定位并修复，全量回归 180 passed，服务恢复正常运行

---

## 背景

子系统 2（批量外呼调度器，spec：`docs/superpowers/specs/2026-08-20-batch-outbound-scheduler-design.md`）完成全部 7 个模块实现、175 个离线测试通过后，进入真机调试阶段。使用真实 ocssaas SIP 线路 + 两部真实手机（15223555549 / 17305886130）跑 8 行模拟清单（`data/mock_customers.csv`），期间连续暴露出 5 个问题。

---

## 问题 1：服务启动失败，端口被旧进程占用

- **时间**：2026-08-20 晚间（首次启动工作台版本）
- **现象**：`python run.py` 报 `Errno 10048`（端口 8765 被占用）；`curl /workbench` 返回 404，说明占端口的是没有工作台功能的旧版进程。
- **原因**：之前调试单通链路时启动的服务进程未退出，仍持有 8765 端口。
- **解决**：
  1. `netstat -ano | findstr :8765` 定位旧进程 PID；
  2. `taskkill /F /PID <pid>` 强制结束（沙箱内权限不足时提权执行；注意必须带 `/F`，否则提示"只有关闭带 /F 选项才能强制终止"）；
  3. 重新启动并确认 `GET /workbench` 返回 200。
- **经验**：重启服务前先确认端口上的进程是当前代码版本，404/行为异常优先怀疑旧进程。

## 问题 2：Excel 编辑 CSV 后中文全部变成 `?`

- **时间**：2026-08-20 晚间（导入模拟清单阶段）
- **现象**：用 Excel 打开 `data/mock_customers.csv` 修改电话号码并保存后，所有中文姓名/城市/备注变成 `?`。
- **原因**：项目解析器按 `utf-8-sig` 解码；Excel 直接打开无 BOM 提示的 CSV 时按 ANSI(GBK) 解释 UTF-8 字节，保存时又把无法映射的中文字符**不可逆**地替换为 `0x3F`（`?`）。用 `read_bytes` 确认文件字节已损坏，非显示问题。
- **解决**：
  1. 用之前解析保留下来的电话号码重建 CSV（8 行，真实号码交替排列）；
  2. 约定后续编辑 CSV 一律用 VS Code（UTF-8）保存，或改用 `.xlsx` 格式导入（解析器已支持 openpyxl）。
- **经验**：Excel + 无 BOM UTF-8 CSV 是经典损坏组合；导入工具侧可考虑自动检测 `0x3F` 密集列并告警。

## 问题 3：非上海号码直拨失败，需加 `00` 前缀

- **时间**：2026-08-20 22:52 ~ 22:54（批次 B-20260820-0039 执行中）
- **现象**：清单中部分号码直拨返回 SIP 486/480，呼叫失败。
- **原因**：ocssaas 线路对非上海归属地号码要求 `00` 出局前缀才能路由成功。
- **解决**：在桥接页 `app/static/workbench.js` 实现自动重拨：
  - `executeDial` 记录 `callContext`（含 `retried`、`reported` 标志）；
  - `onCallEnded` 未接通且未重试过时，自动 `attemptDial("00" + phone)` 重拨一次；
  - `reported` 标志保证每通只向调度器上报一次终态，重拨对服务端完全透明。
- **验证**：后续批次日志出现 `直拨与 00 前缀重拨均失败` 类聚合原因，说明两次尝试链路工作正常；真实可接通号码不再受前缀影响。
- **遗留边界**：直拨振铃若超过 60 秒，服务端活动超时可能与重拨撞车，真机暂未复现，观察后再决定是否缩短振铃超时。

## 问题 4：电话接通但听不到 AI 声音

- **时间**：2026-08-20 晚间（首次成功接通后）
- **现象**：呼叫接通、ASR 能识别用户说话，但对方手机里完全没有 AI 语音。
- **原因**：`workbench.js` 的事件分发函数漏接了 `assistant.audio.chunk` 事件。单通页面 `app.js` 通过该事件调用 `audio.enqueuePcm24k()` 把 24kHz PCM 送入播放/发送链路；桥接页未转发，导致 AI 语音从未进入 SIP 发送轨。
- **解决**：在 `onRealtimeEvent` 中补上：

  ```js
  if (event.type === "assistant.audio.chunk") {
    audio?.enqueuePcm24k(base64ToBytes(event.payload?.audio_b64 || ""));
  }
  ```

  音频经 `phoneDestination` → `replaceSendTrack` 送入 SIP 下行。
- **验证**：重拨后对方可清晰听到 AI 语音，完整对话跑通。
- **经验**：已沉淀为长期记忆「WebRTC 链路需完整处理音频块事件」——新页面复用实时音频链路时，必须逐事件对照 `app.js` 的事件处理清单。

## 问题 5：点「停止」后一直"停止中…"，系统卡死

- **时间**：2026-08-20 22:55 卡死 → 2026-08-21 07:11 修复重启完成
- **现象**：批次进度 7/8，点停止后按钮显示"停止中…（打完当前这通再停）"，但一直没有打完也没有停止；多次 POST `/stop` 均返回 200 却无效。
- **诊断**：`GET /api/batches/latest` 显示 `state=preparing` 且长时间不变；`.env` 未配置 DashScope key 时 personalizer 为 None、prepare 瞬间返回——说明不是"在打电话"，而是**驱动任务已经死亡**，停止标志位无人读取。
- **根因（三个缺陷叠加）**：
  1. `runner._drive` 只捕获 `Exception`；任务被取消抛出的是 `CancelledError`（Python 3.8+ 起为 BaseException 子类），捕获不到 → 驱动任务静默死亡，批次永远停留在 running。
  2. `WorkbenchHub` 向 WebSocket 连接发送无超时保护。浏览器标签被异常关闭（无 TCP FIN）时 `send_json` 可能永久阻塞，把广播和驱动循环一起拖死。22:55 日志中"桥接页断开，通话丢失"与该场景吻合。
  3. `stop_batch` 只设置优雅停止标志位，没有针对"驱动任务已死"的兜底路径。
- **解决（五处结构性修复）**：

  | 文件 | 修复 |
  |---|---|
  | `app/batch/hub.py` | 所有 WebSocket 发送加 `asyncio.wait_for`（默认 5 秒超时），超时/异常即判定僵尸连接并注销 |
  | `app/batch/runner.py` | `_drive` 显式捕获 `CancelledError` 与异常，统一走 `_mark_stopped()` 落终态（内含 `scheduler.force_stop()`） |
  | `app/batch/runner.py` | `stop_batch` 兜底：执行停止动作后若 `task is None or task.done()`，立即 `force_stop` + 置批次 stopped + 广播 `batch.state` |
  | `app/batch/scheduler.py` | 新增 `force_stop()` 直接复位状态机（`_stop_requested=True`、清空 active、状态置 stopped） |
  | `app/storage.py` + `app/main.py` | 新增 `reset_active_customers()`：重启时把"进行中"客户重置回"待呼叫"，与 `reset_running_batches()` 一起在 lifespan 启动时执行 |

- **验证**：
  - 新增测试：僵尸连接超时（`tests/test_workbench_hub.py`）、取消/死亡驱动任务的停止兜底（`tests/test_batch_wiring.py`）、重启恢复（`tests/test_storage_batch.py`）；
  - 全量回归 **180 passed**；
  - 强杀卡死旧进程（PID 25036）→ 手动 `reset_running_batches()` → 重启服务；
  - 重启后确认：批次 B-20260820-0039 状态 `stopped`（7/8）、runner `idle`、桥接在线、卡死时"进行中"的最后一位客户已重置为"待呼叫"，可直接续打。

---

## 汇总

| # | 问题 | 类别 | 修复位置 |
|---|---|---|---|
| 1 | 端口被旧进程占用 | 运维 | 进程管理流程 |
| 2 | Excel 编辑 CSV 中文损坏 | 数据 | 重建清单 + 编辑约定 |
| 3 | 非上海号码需 00 前缀 | 线路适配 | `workbench.js` 自动重拨 |
| 4 | 接通后 AI 无声 | 音频链路 | `workbench.js` 补 `assistant.audio.chunk` |
| 5 | 停止卡死 | 可靠性 | hub 超时 + CancelledError + stop 兜底 + 重启恢复 |

**通用教训**：
1. 异步任务必须捕获 `CancelledError`（BaseException），否则任务死亡无人收尸；
2. 任何向 WebSocket 的发送都要带超时，防御浏览器标签被杀产生的僵尸连接；
3. "优雅停止"必须配"强制兜底"：标志位 + 对执行者存活性的检查缺一不可；
4. 前端新页面复用实时链路时，逐事件对照既有页面核对事件处理完整性。
