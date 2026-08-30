// AI outbound workbench: operator dashboard (import / start / stop / progress /
// customer table) AND the dial bridge. The page declares role=bridge on
// /ws/workbench; bridge.dial commands drive the same phone-mode chain as
// app.js (silent-stream dial -> confirmed -> capture remote stream -> realtime
// session -> swap in the AI voice track).

import { SipBridge, loadSipConfig, saveSipConfig } from "./sip-bridge.js";
import { RealtimeClient } from "./realtime.js";
import { RealtimeAudio } from "./realtime-audio.js";
import { base64ToBytes } from "./pcm.js";
import {
  CLONE_SLOTS,
  READY_CLONED_VOICES,
  VoiceCloneStore,
  fetchCloneMeta,
  recordSample,
  uploadClone,
} from "./voice-clone.js?v=11";

const OUTBOUND_SCENARIO = "outbound_default";
const PAGE_SIZE = 50;

const els = {
  wsBadge: document.getElementById("wb-ws-badge"),
  bridgeLamp: document.getElementById("bridge-lamp"),
  bridgeLampText: document.getElementById("bridge-lamp-text"),
  batchState: document.getElementById("batch-state"),
  batchStateText: document.getElementById("batch-state-text"),
  sipServer: document.getElementById("sip-server"),
  sipPort: document.getElementById("sip-port"),
  sipTransport: document.getElementById("sip-transport"),
  sipDomain: document.getElementById("sip-domain"),
  sipUser: document.getElementById("sip-user"),
  sipPassword: document.getElementById("sip-password"),
  sipSave: document.getElementById("sip-save"),
  registerBtn: document.getElementById("register-btn"),
  sipStatus: document.getElementById("sip-status"),
  importFile: document.getElementById("import-file"),
  uploadBtn: document.getElementById("upload-btn"),
  startBtn: document.getElementById("start-btn"),
  stopBtn: document.getElementById("stop-btn"),
  exportBtn: document.getElementById("export-btn"),
  progressText: document.getElementById("progress-text"),
  progressFill: document.getElementById("progress-fill"),
  currentLine: document.getElementById("current-line"),
  previewCard: document.getElementById("preview-card"),
  previewBatchId: document.getElementById("preview-batch-id"),
  previewTotal: document.getElementById("preview-total"),
  previewInvalid: document.getElementById("preview-invalid"),
  phoneColumnSelect: document.getElementById("phone-column-select"),
  previewHead: document.getElementById("preview-head"),
  previewBody: document.getElementById("preview-body"),
  confirmBtn: document.getElementById("confirm-btn"),
  wbError: document.getElementById("wb-error"),
  customerBody: document.getElementById("customer-body"),
  prevPage: document.getElementById("prev-page"),
  nextPage: document.getElementById("next-page"),
  pageInfo: document.getElementById("page-info"),
  cloneSlot: document.getElementById("clone-slot"),
  cloneName: document.getElementById("clone-name"),
  cloneText: document.getElementById("clone-text"),
  cloneRecordBtn: document.getElementById("clone-record-btn"),
  cloneProgressFill: document.getElementById("clone-progress-fill"),
  cloneStatus: document.getElementById("clone-status"),
  speakerSelect: document.getElementById("speaker-select"),
  cloneVoiceList: document.getElementById("clone-voice-list"),
};

// -- mutable state -------------------------------------------------------------

let ws = null;
let wsReconnectTimer = null;
let wsStopped = false;

let sip = null;
let sipRegistered = false;

// Per-dial call context; `reported` guarantees one terminal bridge event per
// dial so a late onEnded never poisons the next customer's call phase.
let callContext = null;
let audio = null;
let client = null;
let callActive = false;
// response_ids acknowledged as cancelled; late in-flight audio chunks for
// them must be dropped or the interrupted voice resumes after the flush.
let cancelledResponses = new Set();
let droppedLateChunks = 0;

let batch = null; // current batch record
let runnerState = "idle";
let customers = [];
let page = 1;
let lastProgress = null;

// -- helpers -------------------------------------------------------------------

function showError(message) {
  els.wbError.textContent = message || "";
}

function setLamp(lampEl, cls) {
  lampEl.classList.remove("dot-ok", "dot-warn", "dot-busy", "dot-err");
  if (cls) lampEl.classList.add(cls);
}

function nameOf(customer) {
  const raw = customer.raw_data || {};
  for (const key of Object.keys(raw)) {
    if (/姓名|客户名|name/i.test(key)) return String(raw[key] || "");
  }
  return "";
}

function formatDuration(seconds) {
  if (seconds == null) return "";
  const value = Math.round(Number(seconds));
  if (Number.isNaN(value)) return "";
  return `${value}s`;
}

// External URL prefix when deployed behind one (e.g. nginx /v2); injected
// into the page by the server. Empty when served at the domain root.
const APP_BASE = window.APP_BASE || "";

async function api(path, options = {}) {
  const response = await fetch(APP_BASE + path, {
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `请求失败（HTTP ${response.status}）`);
  }
  return data;
}

// -- workbench WebSocket -------------------------------------------------------

function connectWorkbench() {
  clearTimeout(wsReconnectTimer);
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${protocol}://${location.host}${APP_BASE}/ws/workbench`);
  ws.onopen = () => {
    wsStopped = false;
    els.wsBadge.textContent = "工作台已连接";
    els.wsBadge.classList.add("badge-ok");
    // Declare this page the dial bridge; in_call lets the scheduler resume a
    // paused batch exactly where a page refresh interrupted it.
    ws.send(
      JSON.stringify({
        type: "workbench.hello",
        role: "bridge",
        in_call: Boolean(sip?.inCall),
      })
    );
  };
  ws.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    void handleServerMessage(message);
  };
  ws.onclose = (event) => {
    els.wsBadge.textContent = "工作台连接断开";
    els.wsBadge.classList.remove("badge-ok");
    // 1013/1008 are deliberate server rejections (feature disabled / origin);
    // retrying them would only spam.
    if (wsStopped || event.code === 1013 || event.code === 1008) return;
    wsReconnectTimer = setTimeout(connectWorkbench, 3000);
  };
}

function sendToServer(message) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(message));
  }
}

async function handleServerMessage(message) {
  const type = message.type;
  if (type === "hello") {
    runnerState = message.runner?.state || "idle";
    renderBatchState();
    await refreshLatestBatch();
  } else if (type === "bridge.dial") {
    await executeDial(message);
  } else if (type === "bridge.hangup") {
    sip?.hangup();
  } else if (type === "bridge.status") {
    // Broadcast reaches every page including the bridge itself.
    renderBridgeStatus(message.online);
  } else if (type === "batch.progress") {
    lastProgress = message;
    runnerState = message.state;
    renderProgress();
    renderBatchState();
    renderCurrentLine();
  } else if (type === "batch.state") {
    runnerState = message.status;
    if (batch && message.batch_id === batch.id) batch.status = message.status;
    renderBatchState();
    renderControls();
    renderCurrentLine();
    if (message.status === "completed" || message.status === "stopped") {
      await refreshCustomers();
    }
  } else if (type === "customer.updated") {
    applyCustomerUpdate(message);
  } else if (type === "countdown") {
    els.currentLine.textContent = `${message.seconds} 秒后拨打下一位…`;
  }
}

// -- bridge executor (dial chain) ----------------------------------------------

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

async function executeDial(message) {
  const customerId = message.customer_id;
  const phone = String(message.phone || "");
  callContext = {
    customerId,
    phone,
    message,
    confirmed: false,
    reported: false,
    retried: false,
  };
  if (!sip?.registered) {
    await failCurrentCall("SIP 线路未注册，无法拨打");
    return;
  }
  if (sip.inCall) {
    await failCurrentCall("桥接页已有通话在进行");
    return;
  }
  try {
    audio = new RealtimeAudio({
      onChunk: (bytes) => {
        if (callActive) client?.sendAudio(bytes);
      },
      onPlaybackError: () => setBridgeBusy("播放队列已满，已丢弃音频"),
    });
    await audio.initializeOutput();
    attemptDial(phone);
  } catch (error) {
    await failCurrentCall(`拨打失败：${error.message}`);
  }
}

function attemptDial(number) {
  setBridgeBusy(`正在呼叫 ${number}…`);
  sip.dial(number, {
    localStream: createSilentStream(audio.context),
    onConfirmed: (remoteStream) =>
      void onCallConfirmed(remoteStream, callContext?.message || {}),
    onEnded: (detail) => void onCallEnded(detail),
  });
}

async function onCallConfirmed(remoteStream, message) {
  try {
    if (!remoteStream) throw new Error("无法获取对方音频流");
    if (!callContext) return;
    callContext.confirmed = true;
    cancelledResponses = new Set();
    // Uplink: SIP remote audio -> worklet resample -> realtime gateway.
    await audio.prepareCapture({ stream: remoteStream });
    audio.beginCapture();
    // Downlink: mirror playback into the SIP send track.
    const phoneStream = audio.enablePhoneOutput();
    client = new RealtimeClient({
      url: `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${APP_BASE}/ws/realtime`,
      onEvent: onRealtimeEvent,
      onState: (phase) => {
        if (phase === "disconnected" && callActive) sip?.hangup();
      },
    });
    await client.connect();
    client.startSession(OUTBOUND_SCENARIO, "audio", {
      openingText: message.opening_text || "",
      businessBackground: message.business_background || "",
      customerId: message.customer_id,
      gender: message.gender || "",
      speaker: cloneStore.getSelected(),
      botName: message.bot_name || "",
      speakingStyle: message.speaking_style || "",
      templateId: message.template_id ?? null,
    });
    callActive = true;
    const swapped = sip.replaceSendTrack(phoneStream.getAudioTracks()[0]);
    if (!swapped) setBridgeBusy("发送轨替换失败，对方可能听不到 AI 语音");
    else setBridgeBusy(`通话中：${message.phone}`);
    sendToServer({ type: "bridge.call_connected", customer_id: message.customer_id });
  } catch (error) {
    await failCurrentCall(`AI 会话启动失败：${error.message}`);
    sip?.hangup();
  }
}

async function onCallEnded(detail) {
  const context = callContext;
  callActive = false;
  setBridgeBusy("");
  if (!context) {
    await teardownCall();
    return;
  }
  if (!context.confirmed) {
    // SIP failure before answer (busy / no answer / rejected / 4xx). The
    // ocssaas trunk needs the "00" prefix for non-local mobile numbers, so
    // retry once with it before declaring the customer failed.
    if (!context.retried && context.phone && !context.phone.startsWith("00")) {
      context.retried = true;
      try {
        attemptDial(`00${context.phone}`);
        return;
      } catch (error) {
        void error; // fall through and report the original failure
      }
    }
    reportCallTerminal({
      type: "bridge.call_failed",
      customer_id: context.customerId,
      reason: context.retried
        ? `直拨与 00 前缀重拨均失败：${detail || "呼叫失败"}`
        : detail || "呼叫失败",
    });
  } else {
    try {
      await client?.endSession({ timeoutMs: 2000 });
    } catch (error) {
      void error;
    }
    // The terminal result still arrives via the gateway call_finished event;
    // call_ended only tells the scheduler the line is free.
    reportCallTerminal({ type: "bridge.call_ended", customer_id: context.customerId });
  }
  await teardownCall();
}

async function failCurrentCall(reason) {
  const context = callContext;
  setBridgeBusy("");
  if (context) {
    reportCallTerminal({
      type: "bridge.call_failed",
      customer_id: context.customerId,
      reason,
    });
  }
  try {
    if (sip?.inCall) sip.hangup();
  } catch (error) {
    void error;
  }
  await teardownCall();
}

function reportCallTerminal(message) {
  if (!callContext || callContext.reported) return;
  callContext.reported = true;
  callContext = null;
  sendToServer(message);
}

async function teardownCall() {
  callActive = false;
  if (client) {
    client.close();
    client = null;
  }
  if (audio) {
    try {
      await audio.stop();
    } catch (error) {
      void error;
    }
    audio = null;
  }
}

function onRealtimeEvent(event) {
  if (!event || typeof event.type !== "string") return;
  if (event.type === "assistant.audio.chunk") {
    // Downlink: AI voice chunks must be fed into the phone destination, or
    // the caller hears nothing (the SIP send track plays this stream).
    // Chunks belonging to a cancelled response arrive late after the flush
    // and would resume the interrupted sentence — drop them.
    if (cancelledResponses.has(event.payload?.response_id)) {
      // [bargein] late chunk after flush — proves the drop guard is needed.
      if (droppedLateChunks === 0 || ++droppedLateChunks % 25 === 0) {
        console.info(`[bargein] dropped late chunk #${droppedLateChunks} rid=${event.payload?.response_id}`);
      }
      return;
    }
    audio?.enqueuePcm24k(base64ToBytes(event.payload?.audio_b64 || ""));
  } else if (event.type === "response.cancelled") {
    // Barge-in acknowledged: drop queued playback so the caller stops hearing
    // the interrupted sentence immediately.
    if (event.payload?.response_id) {
      cancelledResponses.add(event.payload.response_id);
    }
    console.info(
      `[bargein] flush playback rid=${event.payload?.response_id} queued_left=${
        audio?.context ? Math.max(0, audio.nextStartTime - audio.context.currentTime).toFixed(2) : "?"
      }s`
    );
    droppedLateChunks = 0;
    audio?.cancelPlayback();
  } else if (event.type === "session.ready") {
    setBridgeBusy("AI 会话已就绪，对话进行中");
  } else if (event.type === "error") {
    showError(`[${event.payload?.code || "error"}] ${event.payload?.message || ""}`);
  }
}

// -- SIP registration ----------------------------------------------------------

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
    els.sipStatus.textContent = "正在注册 SIP 线路…";
  } else if (state === "registered") {
    els.sipStatus.textContent = "SIP 线路已注册，等待调度";
    setLamp(els.bridgeLamp, "dot-ok");
    els.bridgeLampText.textContent = "桥接已就绪";
  } else if (state === "registration-failed") {
    els.sipStatus.textContent = `SIP 注册失败：${detail}`;
    setLamp(els.bridgeLamp, "dot-err");
    els.bridgeLampText.textContent = "桥接注册失败";
  } else if (state === "call-failed") {
    els.sipStatus.textContent = `呼叫失败：${detail}`;
  }
}

function setBridgeBusy(text) {
  if (text) {
    setLamp(els.bridgeLamp, "dot-busy");
    els.bridgeLampText.textContent = `通话中`;
    els.sipStatus.textContent = text;
  } else if (sipRegistered) {
    setLamp(els.bridgeLamp, "dot-ok");
    els.bridgeLampText.textContent = "桥接已就绪";
    els.sipStatus.textContent = "SIP 线路已注册，等待调度";
  }
}

function renderBridgeStatus(online) {
  if (!online) {
    setLamp(els.bridgeLamp, "dot-err");
    els.bridgeLampText.textContent = "桥接已离线";
  } else if (!sip?.inCall) {
    setLamp(els.bridgeLamp, sipRegistered ? "dot-ok" : "dot-warn");
    els.bridgeLampText.textContent = sipRegistered ? "桥接已就绪" : "桥接在线·未注册";
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
    setLamp(els.bridgeLamp, "dot-ok");
    els.bridgeLampText.textContent = "桥接已就绪";
  } catch (error) {
    sipRegistered = false;
    showError(error.message);
  } finally {
    els.registerBtn.disabled = false;
    renderControls();
  }
}

// -- batch REST ----------------------------------------------------------------

async function refreshLatestBatch() {
  try {
    const data = await api("/api/batches/latest");
    batch = data.batch || null;
    if (data.runner?.state) runnerState = data.runner.state;
    page = 1;
    if (batch) {
      if (batch.status === "draft") {
        showDraftConfirm(batch);
      } else {
        els.previewCard.hidden = true;
        await refreshCustomers();
      }
    } else {
      els.progressText.textContent = "尚未导入批次";
      els.progressFill.style.width = "0%";
    }
    renderBatchState();
    renderControls();
    renderProgress();
  } catch (error) {
    showError(error.message);
  }
}

async function uploadFile(file) {
  showError("");
  els.uploadBtn.disabled = true;
  try {
    const form = new FormData();
    form.append("file", file);
    const data = await api("/api/batches/import", { method: "POST", body: form });
    renderImportPreview(data);
    batch = {
      id: data.batch_id,
      status: "draft",
      total: data.total,
      done: 0,
      columns: data.columns,
      phone_column: data.phone_column,
    };
    renderControls();
    renderProgress();
  } catch (error) {
    showError(error.message);
  } finally {
    els.uploadBtn.disabled = false;
    els.importFile.value = "";
  }
}

function renderImportPreview(data) {
  els.previewCard.hidden = false;
  els.previewBatchId.textContent = data.batch_id;
  els.previewTotal.textContent = String(data.total);
  els.previewInvalid.textContent = data.invalid_rows
    ? `· 跳过 ${data.invalid_rows} 行（无有效电话）`
    : "";
  els.phoneColumnSelect.replaceChildren();
  for (const column of data.columns || []) {
    const option = document.createElement("option");
    option.value = column;
    option.textContent = column;
    option.selected = column === data.phone_column;
    els.phoneColumnSelect.append(option);
  }
  const columns = data.columns || [];
  const headRow = document.createElement("tr");
  for (const column of columns) {
    const th = document.createElement("th");
    th.textContent = column;
    headRow.append(th);
  }
  els.previewHead.replaceChildren(headRow);
  els.previewBody.replaceChildren();
  for (const row of (data.preview || []).slice(0, 3)) {
    const tr = document.createElement("tr");
    for (const column of columns) {
      const td = document.createElement("td");
      td.textContent = String(row.raw_data?.[column] ?? "");
      tr.append(td);
    }
    els.previewBody.append(tr);
  }
}

// A draft batch surviving a page reload still needs explicit confirmation.
function showDraftConfirm(draftBatch) {
  els.previewCard.hidden = false;
  els.previewBatchId.textContent = draftBatch.id;
  els.previewTotal.textContent = String(draftBatch.total ?? 0);
  els.previewInvalid.textContent = "";
  els.phoneColumnSelect.replaceChildren();
  for (const column of draftBatch.columns || []) {
    const option = document.createElement("option");
    option.value = column;
    option.textContent = column;
    option.selected = column === draftBatch.phone_column;
    els.phoneColumnSelect.append(option);
  }
  els.previewHead.replaceChildren();
  els.previewBody.replaceChildren();
}

async function confirmImport() {
  if (!batch) return;
  showError("");
  els.confirmBtn.disabled = true;
  try {
    const data = await api(`/api/batches/${batch.id}/confirm`, {
      method: "POST",
      body: JSON.stringify({ phone_column: els.phoneColumnSelect.value }),
    });
    batch = data.batch;
    els.previewCard.hidden = true;
    page = 1;
    await refreshCustomers();
    renderControls();
    renderProgress();
  } catch (error) {
    showError(error.message);
  } finally {
    els.confirmBtn.disabled = false;
  }
}

async function startBatch() {
  if (!batch) return;
  showError("");
  els.startBtn.disabled = true;
  try {
    await api(`/api/batches/${batch.id}/start`, { method: "POST" });
  } catch (error) {
    showError(error.message);
  } finally {
    renderControls();
  }
}

async function stopBatch() {
  if (!batch) return;
  showError("");
  try {
    await api(`/api/batches/${batch.id}/stop`, { method: "POST" });
    els.currentLine.textContent = "停止中…（打完当前这通再停）";
  } catch (error) {
    showError(error.message);
  }
}

// -- customer table --------------------------------------------------------------

async function refreshCustomers() {
  if (!batch) return;
  try {
    const data = await api(
      `/api/batches/${batch.id}/customers?page=${page}&size=${PAGE_SIZE}`
    );
    batch = { ...batch, ...data.batch };
    customers = data.customers || [];
    renderCustomerTable();
    els.prevPage.disabled = page <= 1;
    els.nextPage.disabled = customers.length < PAGE_SIZE;
    els.pageInfo.textContent = `第 ${page} 页`;
    renderProgress();
  } catch (error) {
    showError(error.message);
  }
}

function renderCustomerTable() {
  els.customerBody.replaceChildren();
  for (const customer of customers) {
    els.customerBody.append(customerRow(customer));
  }
}

function customerRow(customer) {
  const tr = document.createElement("tr");
  tr.dataset.customerId = String(customer.id);
  const status = customer.status || "待呼叫";
  if (status === "进行中") tr.classList.add("row-active");
  const cells = [
    String(customer.row_number ?? ""),
    nameOf(customer),
    String(customer.phone || ""),
    status,
    formatDuration(customer.duration_seconds),
    customer.result || "",
    customer.reason || "",
  ];
  cells.forEach((text, index) => {
    const td = document.createElement("td");
    td.textContent = text;
    if (index === 3) td.className = `wb-status-${status}`;
    tr.append(td);
  });
  tr.addEventListener("click", () => void toggleDetail(tr, customer));
  return tr;
}

function applyCustomerUpdate(message) {
  // Broadcasts carry no batch id; they always refer to the running batch.
  const local = customers.find((entry) => Number(entry.id) === Number(message.customer_id));
  if (local) {
    local.status = message.status;
    local.result = message.result || "";
    local.reason = message.reason || "";
    local.duration_seconds = message.duration_seconds;
    renderCustomerTable();
  } else if (batch && runnerState !== "idle") {
    // The updated row sits on another page; refresh to stay consistent.
    void refreshCustomers();
  }
  renderProgress();
}

async function toggleDetail(tr, customer) {
  const existing = tr.nextElementSibling;
  if (existing?.classList.contains("wb-transcript-row")) {
    existing.remove();
    return;
  }
  const detailRow = document.createElement("tr");
  detailRow.className = "wb-transcript-row";
  const td = document.createElement("td");
  td.colSpan = 7;
  td.textContent = "加载中…";
  detailRow.append(td);
  tr.after(detailRow);
  td.replaceChildren(await buildDetail(customer));
}

async function buildDetail(customer) {
  const container = document.createElement("div");
  container.className = "wb-transcript";
  const editable = !["进行中", "已完成"].includes(customer.status || "");
  // Inline edit of raw data fields (pending/failed customers only).
  if (editable) {
    const form = document.createElement("div");
    const raw = customer.raw_data || {};
    const inputs = new Map();
    for (const [column, value] of Object.entries(raw)) {
      if (column.startsWith("_")) continue;
      const label = document.createElement("label");
      label.textContent = `${column}：`;
      const input = document.createElement("input");
      input.type = "text";
      input.value = String(value ?? "");
      label.append(input);
      form.append(label);
      inputs.set(column, input);
    }
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.textContent = "保存修改";
    saveBtn.addEventListener("click", async () => {
      const edits = {};
      for (const [column, input] of inputs.entries()) {
        edits[column] = input.value;
      }
      try {
        const data = await api(`/api/batches/${batch.id}/customers/${customer.id}`, {
          method: "PATCH",
          body: JSON.stringify({ raw_data: edits }),
        });
        Object.assign(customer, data.customer);
        renderCustomerTable();
        showError("");
      } catch (error) {
        showError(error.message);
      }
    });
    form.append(saveBtn);
    container.append(form);
  }
  // Conversation transcript (only present after a completed AI call).
  try {
    const data = await api(
      `/api/batches/${batch.id}/customers/${customer.id}/transcript`
    );
    const turns = data.turns || [];
    if (turns.length === 0) {
      const note = document.createElement("div");
      note.textContent = "暂无对话记录";
      container.append(note);
    } else {
      for (const turn of turns) {
        container.append(turnCard(turn));
      }
    }
  } catch (error) {
    const note = document.createElement("div");
    note.textContent = `对话记录加载失败：${error.message}`;
    container.append(note);
  }
  return container;
}

function turnCard(turn) {
  const card = document.createElement("div");
  card.className = `turn turn-${turn.speaker}`;
  const head = document.createElement("div");
  head.className = "turn-speaker";
  head.textContent = turn.speaker === "agent" ? "英文客服" : "客户";
  const body = document.createElement("div");
  body.className = "turn-text";
  body.textContent = turn.source_text || "";
  card.append(head, body);
  if (turn.translated_text) {
    const translation = document.createElement("div");
    translation.className = "turn-translation";
    translation.textContent = turn.translated_text;
    card.append(translation);
  }
  return card;
}

// -- progress / controls rendering -----------------------------------------------

function renderProgress() {
  if (!batch) return;
  const total = Number(batch.total || 0);
  const done = Number(batch.done || 0);
  const index = lastProgress?.index;
  const indexText = index != null ? `第 ${index} 位 / ` : "";
  els.progressText.textContent = `${indexText}共 ${total} 位 · 已完成 ${done}`;
  els.progressFill.style.width = total > 0 ? `${Math.round((done / total) * 100)}%` : "0%";
}

function renderCurrentLine() {
  const state = runnerState;
  if (state === "stopped" || state === "completed") {
    els.currentLine.textContent = state === "completed" ? "批次已完成" : "批次已停止";
  } else if (state === "paused") {
    els.currentLine.textContent = "桥接离线，批次暂停中…";
  } else if (!["dialing", "in_call", "waiting", "preparing"].includes(state)) {
    els.currentLine.textContent = "";
  }
}

const STATE_LABELS = {
  idle: "批次空闲",
  preparing: "准备中…",
  dialing: "拨号中",
  in_call: "通话中",
  waiting: "等待下一位",
  paused: "已暂停",
  stopped: "已停止",
  completed: "已完成",
  running: "运行中",
  ready: "待启动",
  draft: "草稿",
};

function renderBatchState() {
  const label = STATE_LABELS[runnerState] || runnerState;
  els.batchStateText.textContent = `批次：${label}`;
  if (["dialing", "in_call", "preparing", "waiting", "running"].includes(runnerState)) {
    setLamp(els.batchState, "dot-busy");
  } else if (runnerState === "paused") {
    setLamp(els.batchState, "dot-warn");
  } else if (runnerState === "completed") {
    setLamp(els.batchState, "dot-ok");
  } else if (runnerState === "stopped") {
    setLamp(els.batchState, "dot-err");
  } else {
    setLamp(els.batchState, "");
  }
}

function renderControls() {
  const runnable = batch && ["ready", "stopped"].includes(batch.status);
  const running = ["preparing", "dialing", "in_call", "waiting", "paused", "running"].includes(
    runnerState
  );
  els.startBtn.disabled = !(runnable && !running && sipRegistered);
  els.startBtn.title = sipRegistered ? "" : "请先注册 SIP 线路";
  els.stopBtn.disabled = !running;
  els.exportBtn.disabled = !batch;
  els.uploadBtn.disabled = running;
}

// -- wiring ----------------------------------------------------------------------

els.uploadBtn.addEventListener("click", () => els.importFile.click());
els.importFile.addEventListener("change", () => {
  const file = els.importFile.files?.[0];
  if (file) void uploadFile(file);
});
els.confirmBtn.addEventListener("click", () => void confirmImport());
els.startBtn.addEventListener("click", () => void startBatch());
els.stopBtn.addEventListener("click", () => void stopBatch());
els.exportBtn.addEventListener("click", () => {
  if (!batch) return;
  showError("");
  // Plain anchor download: server returns utf-8-sig CSV, Excel opens it cleanly.
  const link = document.createElement("a");
  link.href = `${APP_BASE}/api/batches/${encodeURIComponent(batch.id)}/export`;
  link.download = `${batch.id}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
});
els.registerBtn.addEventListener("click", () => void registerSip());
els.sipSave.addEventListener("click", () => {
  saveSipConfig(readSipForm());
  els.sipStatus.textContent = "SIP 配置已保存，重新注册后生效";
});
els.prevPage.addEventListener("click", () => {
  if (page > 1) {
    page -= 1;
    void refreshCustomers();
  }
});
els.nextPage.addEventListener("click", () => {
  page += 1;
  void refreshCustomers();
});

fillSipForm(loadSipConfig());
connectWorkbench();
void refreshLatestBatch();

// -- voice clone panel ------------------------------------------------------------

const cloneStore = new VoiceCloneStore();
let cloneMeta = null;
let cloneBusy = false;

function setCloneStatus(message, isError = false) {
  els.cloneStatus.textContent = message || "";
  els.cloneStatus.style.color = isError ? "#f85149" : "";
}

function renderSpeakerSelector() {
  const current = cloneStore.getSelected();
  els.speakerSelect.innerHTML = "";
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = "默认（性别交叉音色）";
  els.speakerSelect.append(defaultOption);
  // Ready-made voices first, then locally trained ones (de-duplicated).
  const voices = [
    ...READY_CLONED_VOICES,
    ...cloneStore
      .getVoices()
      .filter(
        (voice) =>
          !READY_CLONED_VOICES.some((v) => v.speaker_id === voice.speaker_id),
      ),
  ];
  for (const voice of voices) {
    const option = document.createElement("option");
    option.value = voice.speaker_id;
    option.textContent = `复刻 · ${voice.name}（${voice.speaker_id}）`;
    els.speakerSelect.append(option);
  }
  els.speakerSelect.value = voices.some((v) => v.speaker_id === current)
    ? current
    : "";

  els.cloneVoiceList.innerHTML = "";
  for (const voice of cloneStore.getVoices()) {
    const row = document.createElement("div");
    row.className = "clone-voice-row";
    const label = document.createElement("span");
    label.textContent = `${voice.name} · ${voice.speaker_id}`;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.textContent = "删除";
    removeBtn.addEventListener("click", () => cloneStore.removeVoice(voice.speaker_id));
    row.append(label, removeBtn);
    els.cloneVoiceList.append(row);
  }
}

cloneStore.subscribe(renderSpeakerSelector);
els.speakerSelect.addEventListener("change", () => {
  cloneStore.setSelected(els.speakerSelect.value);
});

for (const [index, slot] of CLONE_SLOTS.entries()) {
  const option = document.createElement("option");
  option.value = slot;
  option.textContent = `槽位 ${index + 1}（${slot}）`;
  els.cloneSlot.append(option);
}

async function runCloneSampling() {
  if (cloneBusy) return;
  const speakerId = els.cloneSlot.value;
  const name = els.cloneName.value.trim() || speakerId;
  const seconds = cloneMeta?.sample_seconds || 45;
  cloneBusy = true;
  els.cloneRecordBtn.disabled = true;
  els.cloneProgressFill.style.width = "0%";
  try {
    setCloneStatus(`请对照上方文本朗读 ${seconds} 秒…`);
    const pcm = await recordSample({
      seconds,
      onProgress: (ratio) => {
        els.cloneProgressFill.style.width = `${Math.round(ratio * 100)}%`;
      },
    });
    await uploadClone({
      speakerId,
      name,
      pcm,
      store: cloneStore,
      onStatus: setCloneStatus,
    });
    cloneStore.setSelected(speakerId);
  } catch (error) {
    setCloneStatus(error.message || "音色复刻失败", true);
  } finally {
    cloneBusy = false;
    els.cloneRecordBtn.disabled = false;
  }
}

els.cloneRecordBtn.addEventListener("click", () => void runCloneSampling());

async function initClonePanel() {
  try {
    cloneMeta = await fetchCloneMeta();
    els.cloneText.value = cloneMeta.clone_text || "";
    if (!cloneMeta.enabled) {
      els.cloneRecordBtn.disabled = true;
      setCloneStatus("音色复刻未启用（缺少豆包凭证）", true);
    }
  } catch (error) {
    els.cloneRecordBtn.disabled = true;
    setCloneStatus(error.message || "音色复刻服务不可用", true);
  }
  renderSpeakerSelector();
}

void initClonePanel();
