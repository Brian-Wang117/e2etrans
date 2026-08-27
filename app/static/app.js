// UI orchestration: phone-call lifecycle, subtitle rendering and the
// post-call review panel. All event text is rendered via textContent.

import { RealtimeClient } from "./realtime.js";
import { RealtimeAudio } from "./realtime-audio.js?v=2";
import { SipBridge, loadSipConfig, saveSipConfig } from "./sip-bridge.js?v=7";
import {
  createInitialState,
  reduce,
  acceptAudioChunk,
  recordAudioChunk,
} from "./realtime-ui-state.js";
import { base64ToBytes } from "./pcm.js";

// External URL prefix when deployed behind one (e.g. nginx /v2); injected
// into the page by the server. Empty when served at the domain root.
const APP_BASE = window.APP_BASE || "";
const wsScheme = () => (location.protocol === "https:" ? "wss" : "ws");

const $ = (id) => document.getElementById(id);

const els = {
  healthBadge: $("health-badge"),
  phoneControls: $("phone-controls"),
  phoneStatus: $("phone-status"),
  phoneTimer: $("phone-timer"),
  sipServer: $("sip-server"),
  sipPort: $("sip-port"),
  sipTransport: $("sip-transport"),
  sipDomain: $("sip-domain"),
  sipUser: $("sip-user"),
  sipPassword: $("sip-password"),
  sipSave: $("sip-save"),
  phoneNumber: $("phone-number"),
  registerBtn: $("register-btn"),
  dialBtn: $("dial-btn"),
  hangupBtn: $("hangup-btn"),
  monitorMuteBtn: $("monitor-mute-btn"),
  scenarioSelect: $("scenario-select"),
  errorBanner: $("error-banner"),
  outboundBanner: $("outbound-banner"),
  transcript: $("transcript"),
  liveUser: $("live-user"),
  liveUserText: $("live-user-text"),
  liveUserTrans: $("live-user-translation"),
  liveAgent: $("live-agent"),
  liveAgentText: $("live-agent-text"),
  liveAgentTrans: $("live-agent-translation"),
  textInput: $("text-input"),
  textSend: $("text-send"),
  historyList: $("history-list"),
  reviewPanel: $("review-panel"),
  reviewTitle: $("review-title"),
  reviewTurns: $("review-turns"),
  ratingRow: $("rating-row"),
  exportLink: $("export-link"),
  deleteBtn: $("delete-btn"),
  closeReview: $("close-review"),
};

let uiState = createInitialState();
let client = null;
let audio = null;
let inCall = false;
let activeResponseId = null;
let currentSessionId = null;
const renderedTurnIds = new Set();

// -- phone state ---------------------------------------------------------
let sip = null;
let sipRegistered = false;
let monitorMuted = false;
let phoneTimerHandle = null;
let phoneSeconds = 0;

function setState(next) {
  uiState = next;
  renderLive();
}

function applyEvent(event) {
  if (event.type === "assistant.audio.chunk") {
    handleAudioChunk(event.payload || {});
    return;
  }
  if (event.type === "response.cancelled") {
    // The interruption was acknowledged: drop queued playback immediately
    // (manual barge-in and server-side ASR barge-in both land here).
    audio?.cancelPlayback();
  }
  if (event.type === "script.hit") {
    const payload = event.payload || {};
    showOutboundBanner(
      `命中固定话术（${payload.category || "未知类别"}），通话将自动结束`
    );
  }
  if (event.type === "result.reported") {
    const payload = event.payload || {};
    showOutboundBanner(
      `通话结果：${payload.result || "未知"} · ${payload.reason || ""}`
    );
  }
  setState(reduce(uiState, event));
  if (event.type === "session.ended") {
    void finishCall();
  }
}

function handleAudioChunk(payload) {
  activeResponseId = payload.response_id;
  if (!acceptAudioChunk(uiState, payload)) return;
  setState(recordAudioChunk(uiState, payload));
  audio?.enqueuePcm24k(base64ToBytes(payload.audio_b64 || ""));
}

function showError(message) {
  els.errorBanner.textContent = message || "";
  els.errorBanner.hidden = !message;
}

function showOutboundBanner(message) {
  els.outboundBanner.textContent = message || "";
  els.outboundBanner.hidden = !message;
}

function setPhoneStatus(text) {
  els.phoneStatus.textContent = text;
}

// -- live transcript ---------------------------------------------------------

function renderLive() {
  const line = uiState.userLine;
  els.liveUser.hidden = !line;
  if (line) {
    els.liveUserText.textContent = line.text || "…";
    els.liveUserTrans.textContent = line.translation
      ? line.translation.status === "done"
        ? line.translation.text
        : "（翻译不可用）"
      : "";
  }

  const responses = Object.entries(uiState.responses);
  const latest = responses
    .filter(([, response]) => !response.cancelled)
    .map(([id, response]) => ({ id, ...response }))
    .pop();
  els.liveAgent.hidden = !latest || !latest.text;
  if (latest) {
    activeResponseId = latest.id;
    els.liveAgentText.textContent = latest.text;
    els.liveAgentTrans.textContent = latest.translation
      ? latest.translation.status === "done"
        ? latest.translation.text
        : "（翻译不可用）"
      : "";
  }

  for (const turn of uiState.turns) {
    if (renderedTurnIds.has(turn.id)) continue;
    renderedTurnIds.add(turn.id);
    els.transcript.append(turnCard(turn));
  }
}

function turnCard(turn) {
  const card = document.createElement("div");
  card.className = `turn turn-${turn.speaker}`;
  const who = document.createElement("div");
  who.className = "turn-speaker";
  who.textContent = turn.speaker === "tester" ? "测试人员" : "英文客服";
  const src = document.createElement("div");
  src.className = "turn-text";
  src.textContent = turn.source_text || "";
  card.append(who, src);
  if (turn.translated_text) {
    const trans = document.createElement("div");
    trans.className = "turn-translation";
    trans.textContent = turn.translated_text;
    card.append(trans);
  } else if (turn.error_code === "subtitle_error") {
    const note = document.createElement("div");
    note.className = "turn-note";
    note.textContent = "字幕翻译失败，已保留原文";
    card.append(note);
  }
  if (turn.interrupted) {
    const note = document.createElement("div");
    note.className = "turn-note";
    note.textContent = "（已打断）";
    card.append(note);
  }
  return card;
}

// -- call lifecycle ----------------------------------------------------------

async function checkHealth() {
  try {
    const response = await fetch(`${APP_BASE}/api/health`);
    const health = await response.json();
    const realtime = health.realtime_provider === "doubao";
    els.healthBadge.textContent = realtime
      ? "豆包端到端语音已就绪"
      : "实时语音未启用（REALTIME_PROVIDER）";
    els.healthBadge.classList.toggle("badge-ok", realtime);
    if (!realtime) {
      showError("实时语音未启用，无法拨打电话会话");
    }
  } catch (error) {
    els.healthBadge.textContent = "服务不可用";
    showError(`健康检查失败：${error.message}`);
  }
}

async function loadScenarios() {
  const response = await fetch(`${APP_BASE}/api/scenarios`);
  const data = await response.json();
  for (const scenario of data.scenarios || []) {
    const option = document.createElement("option");
    option.value = scenario.id;
    option.textContent = scenario.label;
    els.scenarioSelect.append(option);
  }
}

// Fresh per-call UI state: a new call must never inherit the previous call's
// transcript, provisional responses, or audio dedup counters (server response
// ids restart at response-1 for every session).
function resetCallUi() {
  uiState = createInitialState();
  renderedTurnIds.clear();
  els.transcript.replaceChildren();
  els.liveUser.hidden = true;
  els.liveAgent.hidden = true;
  els.reviewPanel.hidden = true;
  activeResponseId = null;
}

async function finishCall() {
  if (!inCall) return;
  inCall = false;
  const sessionId = client?.sessionId || currentSessionId;
  currentSessionId = sessionId;
  await teardownCall();
  if (sessionId) {
    await openReview(sessionId);
  }
  await refreshHistory();
}

async function teardownCall() {
  if (client) {
    client.close();
    client = null;
  }
  if (audio) {
    await audio.stop();
    audio = null;
  }
  els.scenarioSelect.disabled = false;
  els.liveUser.hidden = true;
  els.liveAgent.hidden = true;
  resetPhoneUi();
}

// -- phone mode ---------------------------------------------------------------

function readSipForm() {
  return {
    server: els.sipServer.value.trim(),
    port: els.sipPort.value.trim(),
    transport: els.sipTransport.value,
    domain: els.sipDomain.value.trim(),
    user: els.sipUser.value.trim(),
    password: els.sipPassword.value,
  };
}

function fillSipForm(config) {
  els.sipServer.value = config.server;
  els.sipPort.value = config.port;
  els.sipTransport.value = config.transport;
  els.sipDomain.value = config.domain;
  els.sipUser.value = config.user;
  els.sipPassword.value = config.password;
}

function onSipState(state, detail) {
  if (state === "registering") {
    setPhoneStatus("正在注册 SIP 线路…");
  } else if (state === "registered") {
    setPhoneStatus("SIP 线路已注册，可以拨打");
  } else if (state === "registration-failed") {
    setPhoneStatus(`SIP 注册失败：${detail}`);
  } else if (state === "dialing") {
    setPhoneStatus(`正在拨打 ${detail}`);
  } else if (state === "call-failed") {
    showError(`呼叫失败：${detail}`);
    setPhoneStatus("呼叫失败");
  } else if (state === "call-ended") {
    setPhoneStatus(`通话结束${detail ? `：${detail}` : ""}`);
  }
}

async function registerSip() {
  showError("");
  const config = readSipForm();
  saveSipConfig(config);
  if (!sip) sip = new SipBridge({ onState: onSipState });
  els.registerBtn.disabled = true;
  try {
    await sip.register(config);
    sipRegistered = true;
    els.dialBtn.disabled = false;
  } catch (error) {
    sipRegistered = false;
    els.dialBtn.disabled = true;
    showError(error.message);
  } finally {
    els.registerBtn.disabled = false;
  }
}

// The caller must hear nothing until the AI voice track is swapped in.
function createSilentStream(context) {
  const destination = context.createMediaStreamDestination();
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  gain.gain.value = 0;
  oscillator.connect(gain);
  gain.connect(destination);
  oscillator.start();
  return destination.stream;
}

async function dialPhone() {
  const number = els.phoneNumber.value.trim();
  if (!number || !sip?.registered) return;
  showError("");
  resetCallUi();
  els.dialBtn.disabled = true;
  els.registerBtn.disabled = true;
  try {
    audio = new RealtimeAudio({
      // Continuous streaming: every captured chunk goes upstream; Doubao's
      // server-side VAD (input_mode "audio") handles turn boundaries.
      onChunk: (bytes) => {
        if (inCall) client?.sendAudio(bytes);
      },
      onPlaybackError: () => setPhoneStatus("播放队列已满，已丢弃音频"),
    });
    await audio.initializeOutput();
    sip.dial(number, {
      localStream: createSilentStream(audio.context),
      onConfirmed: (remoteStream) => void onPhoneConfirmed(remoteStream),
      onEnded: () => void onPhoneDisconnected(),
    });
    els.hangupBtn.disabled = false;
  } catch (error) {
    showError(`拨打失败：${error.message}`);
    resetPhoneUi();
    await teardownCall();
  }
}

async function onPhoneConfirmed(remoteStream) {
  try {
    if (!remoteStream) throw new Error("无法获取对方音频流");
    // Uplink: SIP remote audio -> worklet resample -> realtime gateway.
    await audio.prepareCapture({ stream: remoteStream });
    audio.beginCapture();
    // Downlink: mirror playback into the SIP send track.
    const phoneStream = audio.enablePhoneOutput();
    client = new RealtimeClient({
      url: `${wsScheme()}://${location.host}${APP_BASE}/ws/realtime`,
      onEvent: applyEvent,
      onState: (phase) => {
        if (phase === "disconnected" && inCall) {
          showError("连接已断开");
          hangupPhone();
        }
      },
    });
    await client.connect();
    client.startSession(els.scenarioSelect.value, "audio");
    inCall = true;
    currentSessionId = null;
    const swapped = sip.replaceSendTrack(phoneStream.getAudioTracks()[0]);
    if (!swapped) showError("发送轨替换失败，对方可能听不到 AI 语音");
    setPhoneStatus("通话已接通，AI 会话建立中…");
    startPhoneTimer();
    els.monitorMuteBtn.disabled = false;
  } catch (error) {
    showError(`电话会话启动失败：${error.message}`);
    sip.hangup();
    await teardownCall();
  }
}

async function onPhoneDisconnected() {
  els.hangupBtn.disabled = true;
  els.monitorMuteBtn.disabled = true;
  if (inCall) {
    try {
      await client?.endSession({ timeoutMs: 2000 });
    } catch (error) {
      void error;
    }
    await finishCall();
  } else {
    await teardownCall();
  }
}

function hangupPhone() {
  sip?.hangup(); // emits call-ended -> onPhoneDisconnected
}

function startPhoneTimer() {
  stopPhoneTimer();
  phoneSeconds = 0;
  els.phoneTimer.textContent = "00:00";
  phoneTimerHandle = setInterval(() => {
    phoneSeconds += 1;
    const minutes = String(Math.floor(phoneSeconds / 60)).padStart(2, "0");
    const seconds = String(phoneSeconds % 60).padStart(2, "0");
    els.phoneTimer.textContent = `${minutes}:${seconds}`;
  }, 1000);
}

function stopPhoneTimer() {
  if (phoneTimerHandle) {
    clearInterval(phoneTimerHandle);
    phoneTimerHandle = null;
  }
  els.phoneTimer.textContent = "";
}

function toggleMonitorMute() {
  if (!audio) return;
  monitorMuted = !monitorMuted;
  audio.setMonitorMuted(monitorMuted);
  els.monitorMuteBtn.textContent = monitorMuted ? "监听：关" : "监听：开";
}

function resetPhoneUi() {
  stopPhoneTimer();
  monitorMuted = false;
  els.monitorMuteBtn.textContent = "监听：开";
  els.monitorMuteBtn.disabled = true;
  els.hangupBtn.disabled = true;
  els.registerBtn.disabled = false;
  els.dialBtn.disabled = !sipRegistered;
}

// -- session events -----------------------------------------------------------

function onSessionReady(event) {
  currentSessionId = event.session_id;
  els.scenarioSelect.disabled = true;
  setPhoneStatus("AI 会话已就绪，对话进行中");
}

// -- text fallback -----------------------------------------------------------

function submitText() {
  const text = els.textInput.value.trim();
  if (!text || !inCall) return;
  if (client?.submitText(text) === false) {
    showError("发送失败：上行缓冲已满");
    return;
  }
  els.textInput.value = "";
}

// -- review panel ------------------------------------------------------------

async function openReview(sessionId) {
  const response = await fetch(`${APP_BASE}/api/sessions/${sessionId}`);
  if (!response.ok) return;
  const session = await response.json();
  els.reviewPanel.hidden = false;
  els.reviewTitle.textContent = `会话复盘 · ${session.id}`;
  els.reviewTurns.textContent = "";
  for (const turn of session.turns || []) {
    els.reviewTurns.append(reviewTurnCard(turn));
  }
  els.exportLink.href = `${APP_BASE}/api/sessions/${session.id}/export`;
  els.deleteBtn.dataset.sessionId = session.id;
  renderRating(session.rating);
}

function reviewTurnCard(turn) {
  const card = document.createElement("div");
  card.className = `turn turn-${turn.speaker}`;
  const head = document.createElement("div");
  head.className = "turn-speaker";
  head.textContent =
    (turn.speaker === "tester" ? "测试人员" : "英文客服") +
    (turn.latency_ms != null ? ` · 翻译 ${turn.latency_ms}ms` : "");
  const src = document.createElement("div");
  src.className = "turn-text";
  src.textContent = turn.source_text || "";
  card.append(head, src);
  if (turn.translated_text) {
    const trans = document.createElement("div");
    trans.className = "turn-translation";
    trans.textContent = turn.translated_text;
    card.append(trans);
  }
  return card;
}

function renderRating(current) {
  els.ratingRow.textContent = "";
  for (let value = 1; value <= 5; value += 1) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = String(value);
    button.className = current === value ? "rating-btn rating-active" : "rating-btn";
    button.addEventListener("click", async () => {
      const sessionId = els.deleteBtn.dataset.sessionId;
      await fetch(`${APP_BASE}/api/sessions/${sessionId}/rating?rating=${value}`, {
        method: "POST",
      });
      renderRating(value);
    });
    els.ratingRow.append(button);
  }
}

// -- history -----------------------------------------------------------------

async function refreshHistory() {
  const response = await fetch(`${APP_BASE}/api/sessions`);
  const data = await response.json();
  els.historyList.textContent = "";
  for (const session of data.sessions || []) {
    const item = document.createElement("li");
    const link = document.createElement("button");
    link.type = "button";
    link.className = "history-item";
    link.textContent = `${session.created_at || session.id} · ${session.scenario_id} · ${session.turn_count} 话轮`;
    link.addEventListener("click", () => void openReview(session.id));
    item.append(link);
    els.historyList.append(item);
  }
}

// -- wiring ------------------------------------------------------------------

const wrappedApplyEvent = applyEvent;
applyEvent = (event) => {
  wrappedApplyEvent(event);
  if (event.type === "session.ready") onSessionReady(event);
  if (event.type === "error") {
    showError(`[${event.payload?.code || "error"}] ${event.payload?.message || ""}`);
  }
};

els.registerBtn.addEventListener("click", () => void registerSip());
els.dialBtn.addEventListener("click", () => void dialPhone());
els.hangupBtn.addEventListener("click", hangupPhone);
els.monitorMuteBtn.addEventListener("click", toggleMonitorMute);
els.sipSave.addEventListener("click", () => {
  saveSipConfig(readSipForm());
  setPhoneStatus("SIP 配置已保存，重新注册后生效");
});
els.textSend.addEventListener("click", submitText);
els.textInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") submitText();
});
els.deleteBtn.addEventListener("click", async () => {
  const sessionId = els.deleteBtn.dataset.sessionId;
  if (!sessionId) return;
  await fetch(`${APP_BASE}/api/sessions/${sessionId}`, { method: "DELETE" });
  els.reviewPanel.hidden = true;
  await refreshHistory();
});
els.closeReview.addEventListener("click", () => {
  els.reviewPanel.hidden = true;
});

void checkHealth();
void loadScenarios();
void refreshHistory();
fillSipForm(loadSipConfig());
