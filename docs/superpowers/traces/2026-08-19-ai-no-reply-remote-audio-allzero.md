# Trace：电话模式 AI 听而不回（SIP 远端音频采集全零）

- **记录时间**：2026-08-19
- **项目**：e2etrans_NEW（AI 外呼模拟器）
- **状态**：✅ 已修复并验证通过

---

## 一、症状

启动服务后，电话模式（SIP 拨打）接通通话，电话另一端说话，**AI 能"听"但从不回复**：

- 页面无识别字幕、无 AI 回复语音；
- 上行采集包在稳定流动，但内容实际为全零静音；
- 网页模式（浏览器麦克风）不受影响。

## 二、排查过程

| 阶段 | 假设 | 验证 | 结论 |
|---|---|---|---|
| 1 | 怀疑 SIP 注册/拨打失败 | 检查报错文案，确认 REGISTER 成功、通话 confirmed | ❌ 信令正常 |
| 2 | 怀疑"Unavailable"类呼叫错误 | 反查 JsSIP 源码，确认 `Unavailable` 对应 SIP 480/408/410/430，与本症状无关 | ❌ 排除 |
| 3 | 怀疑后端豆包会话未建立 | 检查健康检查与网关代码路径 | ❌ 后端链路正常 |
| 4 | 对照历史 trace（双向同传 v2"反向采音全零"排障） | 本项目电话模式采音路径与其完全同构：`session.connection.getRemoteStreams()[0]` → `createMediaStreamSource` → AudioWorklet 重采样 16k → 上行 | ✅ 命中 |

### 采集链路（出问题的一段）

```
SIP 远端流 (WebRTC)
  → audio.prepareCapture({ stream: remoteStream })
  → context.createMediaStreamSource(stream)   ← 只接了 WebAudio 节点
  → AudioWorklet "realtime-pcm-capture"（重采样 16k、PCM16 分包）
  → RealtimeClient.sendAudio → 网关 → 豆包
```

**页面上没有任何 `<audio>` 元素消费这条远端流。**

## 三、根因

**Chrome 消费者机制**：WebRTC 远端音频流若只接入 WebAudio 节点（`createMediaStreamSource`）而没有 `<audio>` 元素消费者，Chrome 不向该流送真实采样，AudioWorklet 采集到的 PCM **恒为全零静音** → 豆包收到全程静音 → 服务端 VAD/ASR 永不触发 → AI 不回复。

辅助经验（来自历史 trace）：

- `getStats()` 的 `audioLevel=0` 有误导性，不能据此断定"平台发的是静音"；
- 下"平台侧问题"结论前必须做干净对照实验：只挂 `<audio>` 元素播放远端流的普通通话，能听到就说明平台下行正常；
- `getUserMedia` 本地麦克风路径不受此机制影响（因此网页模式正常）。

## 四、修复方案

### 1. 核心修复：给外部流挂隐藏静音 `<audio>` 探针

文件：`app/static/realtime-audio.js`

- 新增 `_attachStreamProbe(stream)`：当 `prepareCapture({ stream })` 收到外部流（SIP 远端流）时，创建隐藏 `<audio>` 元素挂上该流，`muted=true`、`volume=0`（不出声，仅作消费者激活采样），`autoplay` 并 `play().catch()`；
- 新增 `_detachStreamProbe()`：`stop()` 时 `pause()`、清空 `srcObject` 并从 DOM 移除，避免残留；
- 构造函数增加 `this.streamProbe = null` 字段；
- 网页模式（`getUserMedia`）不创建探针，行为不变。

```js
_attachStreamProbe(stream) {
  if (typeof document === "undefined") return;
  if (!this.streamProbe) {
    const probe = document.createElement("audio");
    probe.autoplay = true;
    probe.style.display = "none";
    document.body.appendChild(probe);
    this.streamProbe = probe;
  }
  this.streamProbe.srcObject = stream;
  this.streamProbe.muted = true;
  this.streamProbe.volume = 0;
  this.streamProbe.play().catch(() => {});
}
```

### 2. 强制绕过浏览器缓存

- `app/static/index.html`：`app.js?v=6` → `app.js?v=8`
- `app/static/app.js`：`realtime-audio.js` → `realtime-audio.js?v=2`

（浏览器会按模块 URL 缓存 ES Module，不加版本号修复不会生效。）

### 3. 生效步骤

1. 重启后端：`.venv\Scripts\python.exe run.py`（端口 8765）；
2. 浏览器 `Ctrl+F5` 强刷后再注册拨打。

## 五、插曲：修复曾一度丢失

修复后次日验证仍失败，检查发现 `realtime-audio.js` 的探针代码**已不在磁盘上**（疑似编辑器回滚/未保存），而 `sip-bridge.js` 的修复仍在。重新应用全部改动并逐项验证落盘后生效。

**教训**：验证修复前先确认代码保存状态（`Select-String` 检查关键符号是否存在于文件），避免在"已修复"的错误前提下重复排障。

## 六、验证清单

- [x] `node --check realtime-audio.js` 语法通过
- [x] `_attachStreamProbe` / `_detachStreamProbe` 均已落盘（4 处引用）
- [x] 缓存版本号已提升（`app.js?v=8`、`realtime-audio.js?v=2`）
- [x] 重启 + 强刷后，电话接通 AI 正常识别并回复（用户确认"成功"）

## 七、遗留风险与后续预案

1. 若个别 Chrome 版本对 `muted + volume=0` 的探针仍不送采样（电平再次归零），改微音量或改用 `MediaStreamTrackProcessor` 直接读取音轨；
2. 需要量化确认时，可在 worklet 侧加每 2 秒 PCM16 峰值电平日志（`峰值/32767` 不再为 0 即为通路），稳定后清理；
3. `getStats()` inbound-rtp 诊断（包增量 + audioLevel）仍可作为区分"平台未发 RTP / 消费问题 / 采集问题"的手段。

## 八、涉及文件

| 文件 | 改动 |
|---|---|
| `app/static/realtime-audio.js` | 探针挂接/清理（核心修复） |
| `app/static/app.js` | import 版本号 `realtime-audio.js?v=2` |
| `app/static/index.html` | `app.js?v=8` |
| `app/static/sip-bridge.js` | （同期修复，未丢失）SIP 失败状态码透传 + JsSIP 调试日志 |
