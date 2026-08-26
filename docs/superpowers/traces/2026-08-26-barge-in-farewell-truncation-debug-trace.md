# 实时语音打断收尾调试 Trace：打断后衔接与挂断截断

- **日期**：2026-08-24 ~ 2026-08-26
- **范围**：实时语音网关（`app/realtime/gateway.py`）打断（barge-in）功能真机可用后的两轮收尾问题
- **结果**：2 个问题全部定位并修复，全量回归 201 → 203 passed，用户真机确认问题消失

---

## 背景

在此之前的三轮排查中，打断功能已从"完全无法打断"修复到真机可用（演进路线：① ASR final 触发 → ② interim 触发 → ③ 可听窗口 + 播放尾部清缓冲，测试基线 200 passed，全链路 `[bargein]` 埋点）。用户真机测试确认**打断本身已生效**，但随后暴露出两个与打断紧密相关的收尾问题。

挂断链路背景知识（排查时的关键时序链）：

```
导演 ScheduleHangup(delay) → _hangup_after → _stop_event.set()
→ 会话停止 → websocket 关闭 → 前端 onState: disconnected
→ sip.hangup()（BYE）→ onCallEnded → teardownCall
→ audio.stop() → cancelPlayback()（丢弃所有排队音频）
```

即：**一旦挂断链启动，浏览器端尚未播完的 AI 音频全部作废**。两个问题都出在这条链与音频缓冲的时序错位上。

---

## 问题 1：能打断，但打断之后下一句说不完全

- **时间**：2026-08-24（用户贴出真机浏览器控制台日志）
- **现象**：客户说话能立刻打断 AI 播报，但 AI 被打断后的新回复刚开口就被掐掉，"下一句说不完全"。
- **排查过程**：
  1. 分析用户贴的浏览器控制台日志，找到两条关键埋点：

     ```
     [bargein] flush playback rid=response-2 queued_left=12.08s
     [bargein] flush playback rid=response-2 queued_left=7.26s
     ```

     两次清缓冲都成功 → 打断本身没问题，问题出在打断**之后**。
  2. 对照 `_handle_upstream` 的 451（ASR）分支推演时序：客户打断时说的话，ASR 是流式的——打断触发后，这句话的残余识别（后续字、最终 final）仍会持续到达；此时模型新回复往往已经开口，残余识别又满足"AI 可听 + 有语音"条件，触发**二次打断**，把新回复截断。
- **原因**：残余/迟到的同句 ASR 识别对新回复造成二次打断。纯时间窗口抑制不够（长句残余可能晚于固定窗口到达）。
- **解决**（`app/realtime/gateway.py`）：
  1. **话轮作用域抑制**：记录触发打断的话轮 `_barge_in_turn_id`，该话轮的所有残余识别永不触发二次打断；话轮结束（459）后新话轮正常恢复打断能力；
  2. **0.8 秒时间宽限**（`BARGE_IN_GRACE_SECONDS`）：覆盖话轮刚结束、新回复刚开口的竞态窗口；
  3. 顺带修复顺序缺陷：话轮 ID 分配原在打断检查之后，首次打断时拿不到话轮 ID，已提前到 451 分支开头；
  4. 抑制时打印 `[bargein] suppressed (same_turn=... grace=...)` 便于真机观察。
- **验证**：新增 `test_residual_asr_after_barge_in_does_not_kill_fresh_reply`（同句残余不截断新回复；宽限期后新句仍能正常打断），全量 **201 passed**，服务重启真机验证。

## 问题 2：结束通话前，AI 最后一段话（告别语）播放不完全

- **时间**：2026-08-26（用户反馈"主要是触发打断过后"明显）
- **现象**：通话收尾时告别语被截断，尤其发生过打断的通话。
- **排查过程**：
  1. 梳理挂断链路（见背景），确认前端 `teardownCall → audio.stop()` 会立即丢弃排队音频 → 只要挂断早于播放完成，尾部必丢；
  2. 审查导演收尾路径（`app/outbound/call_director.py`）：告别语注入时 `ScheduleHangup(estimate_speech_seconds(...))` 按字数估算定时；但 359（TTS 合成结束）分支调用 `on_goodbye_played()` → `ScheduleHangup(GOODBYE_MARGIN_SECONDS=0.5)`；
  3. 审查 `_schedule_hangup`：无条件取消旧定时器 → 359 的 0.5 秒短定时器直接**顶掉**注入时的长估算定时器；
  4. 结合已证实的"上游 TTS 合成快于实时播放"（可听窗口排查时确认），359 时刻浏览器还排着数秒音频，0.5 秒后挂断 → 尾部全丢；
  5. 针对"打断后更明显"：收尾静音（`MuteInput`）只拦截了**上行动频**，但静音前已上传的音频仍在上游 ASR 管线里，其残余识别稍后经 451 到达，在告别语播放中再次触发打断（问题 1 的话轮抑制只覆盖同一话轮，静音后到达的识别可能是新话轮）。
- **原因（三个缺陷叠加）**：
  1. 359 的告别定时器不考虑播放尾部，0.5 秒余量只够网络延迟；
  2. `_schedule_hangup` 允许短定时器提前挂断；
  3. 收尾静音后，在途残余 ASR 仍能触发打断，截断正在播的告别语。
- **解决**（`app/realtime/gateway.py`）：
  1. 执行 `ScheduleHangup` 时：延迟 = `max(导演给的延迟, 播放尾部剩余时长 + 0.5s)`，播放尾部复用可听窗口 `_audible_until`；
  2. `_schedule_hangup` 只允许推迟不允许提前（新增 `_hangup_deadline`，更早的请求直接忽略）；
  3. 451 打断条件追加 `and not self._muted_input`：收尾静音后残余识别一律不触发打断。
- **验证**：新增两个回归测试——
  - `test_hangup_waits_for_farewell_playback_tail`：模拟 2 秒告别音频排队后合成结束，断言 1 秒后会话仍活着（修复前 0.05 秒就挂断，测试必失败）；
  - `test_residual_asr_after_mute_does_not_interrupt_farewell`：静音后到达的残余识别断言 `interrupts == 0` 且无 `response.cancelled`；

  全量 **203 passed**，服务重启，用户真机确认问题消失。

---

## 汇总

| # | 问题 | 类别 | 修复位置 | 新增测试 |
|---|---|---|---|---|
| 1 | 打断后下一句被残余识别二次打断 | 打断衔接 | `gateway.py` 话轮作用域抑制 + 0.8s 宽限 | `test_residual_asr_after_barge_in_does_not_kill_fresh_reply` |
| 2 | 挂断截断告别语尾部 | 挂断时序 | `gateway.py` 播放尾部等待 + 定时器只推迟 + 静音禁打断 | `test_hangup_waits_for_farewell_playback_tail`、`test_residual_asr_after_mute_does_not_interrupt_farewell` |

**通用教训**：

1. **流式 ASR 有拖尾**：打断由语音触发后，同一句话的残余识别会在打断之后继续到达，任何"语音即打断"的策略必须配话轮作用域的抑制，否则新回复会被自己的触发源掐死；
2. **合成完成 ≠ 播放完成**：TTS 流式合成快于实时播放，所有基于"合成结束"事件的定时动作（挂断、状态切换）都必须先折算浏览器端的排队播放时长；
3. **定时器单调性**：多个来源调度同一个"挂断"动作时，必须只允许推迟不允许提前，否则短余量定时器会覆盖长估算；
4. **静音要静音彻底**：收尾静音只能拦上行动，拦不住在途识别事件，识别事件消费侧也要同步检查静音状态；
5. **埋点先行值回票价**：问题 1 直接靠浏览器端 `[bargein] flush playback` 埋点定性（打断成功但重复触发），实时语音类时序问题没有埋点几乎无法远程诊断；
6. **时序依赖的 bug 必须钉成回归测试**：用 `FakeDoubao` 精确控制事件到达顺序，把"最坏时序"固定下来，真机偶发问题从此可确定性复现。
