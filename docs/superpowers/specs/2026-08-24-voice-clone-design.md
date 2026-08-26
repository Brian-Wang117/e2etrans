# 音色复刻（Voice Clone）功能设计 — 2026-08-24

## 背景与目标

批量外呼目前使用官方音色（`.env` 配置，含性别交叉）。本功能允许对真人客服
声音采样 45 秒，通过火山引擎声音复刻（Seed-ICL 2.0 / `model_type=4`）训练
出专属音色，并在工作台选择后用于外呼通话。

复刻音色本质只是一个 `speaker_id`（`S_` 前缀），因此对现有拨号/会话链路是
零协议改动的配置项插入。

## 已确认的决策（用户拍板）

1. **用法**：工作台音色选择器，拨号前选定，随 `session.start` 下发。
2. **优先级**：复刻优先、与性别交叉二选一。选中复刻音色时全批次统一使用，
   不再按客户性别切换。
3. **存储**：复刻结果存浏览器 `localStorage`（键 `doubao_custom_voices`），
   与参考项目一致；选择器偏好同样存 `localStorage`。
4. **槽位**：内置两个一键填入按钮 `S_heWZZwa62` / `S_Ip5CTwa62`，与本项目
   `DOUBAO_APP_ID=8278104837` 同项目（已核实），同槽位重录覆盖旧音色。

## 火山接口（官方文档核实，V1 训练接口）

- 训练：`POST https://openspeech.bytedance.com/api/v1/mega_tts/audio/upload`
  - Header：`Authorization: Bearer; <token>`（分号分隔，勿带花括号）、
    `Resource-Id: seed-icl-2.0`（model_type=4）/ `seed-icl-1.0`
  - Body：`appid`、`speaker_id`、`audios: [{audio_bytes(base64), audio_format,
    text}]`、`source: 2`、`language: 0`、`model_type: 4`
  - `text` 为朗读对照文本，差异过大返回 **1109 WERError**
- 状态：`POST /api/v1/mega_tts/status`，`status` 为 2/4 时可用于合成
- 关键错误码：1109（录音与文本差异大）、1103/1104（声纹）、1106（ID 重复）、
  1122（无人声）、1123（同槽位超 10 次上传）

## 架构

```
工作台 ──录音45s──▶ POST /api/voice-clone/upload（服务端代理）──▶ 火山 upload
工作台 ──轮询────▶ POST /api/voice-clone/status（服务端代理）──▶ 火山 status
成功 ──▶ localStorage ──▶ 音色选择器 ──▶ bridge.dial.speaker ──▶ session.start.speaker
```

**密钥不出服务端**：浏览器请求不带任何凭证；服务端注入
`DOUBAO_ACCESS_KEY`（Bearer）与 `DOUBAO_APP_ID`，与豆包 WS 透传同一模式。

### 后端

- `app/voice_clone.py`（新）：`VoiceCloneRelay`（httpx.AsyncClient，可注入）
  - `upload(speaker_id, name, audio_b64)` → 校验 `S_` 前缀、组装请求体
    （含固定 `CLONE_TEXT`）、错误码映射成人话（1109 →"录音与朗读文本差异
    过大，请对照文本重录"；license 类 → 追加"槽位不属于当前 AppID"）
  - `status(speaker_id)` → 返回 `{status, create_time, ...}`
- `app/main.py`：`POST /api/voice-clone/upload`、`POST /api/voice-clone/status`

### speaker 链路（显式优先）

- `browser_protocol.py`：`session.start` 新增可选 `speaker`（仅允许
  `S_` 前缀或空）
- `gateway.py`：payload 显式 `speaker` 非空 → 直接使用，跳过性别交叉；
  否则维持现有 `_lookup_customer_gender` + `_speaker_for_gender`
- `runner.py`：`bridge.dial` 消息带 `speaker`；`workbench.js` →
  `realtime.js` → `session.start`
- `persona.py`：使用 `S_` 音色时系统提示词追加 `CLONE_GUARD`（禁止复述
  采样朗读文本，防复读）

### 前端

- `app/static/voice-clone.js`（新）：
  - `recordSample()`：复用 `RealtimeAudio` 麦克风采集（已是 16k mono
    PCM16），45 秒 + 进度回调
  - `uploadSample()`：PCM → WAV → base64 → `/api/voice-clone/upload`
  - 成功后轮询 status，`status ∈ {2,4}` 才写 localStorage 并触发观察者
- `workbench.html`：复刻面板（槽位一键填入、只读朗读文本框、采样按钮 +
  进度条、状态行）+ 音色选择器（默认/复刻音色）
- `workbench.js`：选择器状态 → 拨号时写入 `dial.speaker`

### 朗读文本（中性散文，非业务话术）

内置约 230 字中性散文作为 `CLONE_TEXT`，页面只读展示、跟读。不用业务
话术，因为火山会把训练文本注入模型，业务话术会串进真实对话。

## 错误处理

- 麦克风权限被拒 → 面板提示，不阻塞工作台其余功能
- 录音不足 40 秒 → 前端拦截，提示重录
- 上传失败 → 映射后的人话提示展示在状态行
- 轮询超时（120 秒未就绪）→ 提示稍后在工作台重试（不写 localStorage）

## 测试（不触真实网络）

- `tests/test_voice_clone.py`：注入 fake httpx transport，覆盖 S_ 校验、
  model_type→Resource-Id 路由、请求体组装、1109 错误文案、状态轮询判定
- `tests/test_gateway_outbound.py`：显式 `speaker` 优先于性别交叉；
  空值回退交叉逻辑
- 全量回归（基线 186 passed）
