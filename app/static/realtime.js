// Browser realtime protocol client: generations, sequences, heartbeat and
// bufferedAmount backpressure. Only events from the current connection
// generation with strictly increasing sequences reach the UI.

import { bytesToBase64 } from "./pcm.js";

export class RealtimeClient {
  constructor({
    url,
    WebSocketClass = globalThis.WebSocket,
    onEvent,
    onState,
    maxBufferedBytes = 262144,
    heartbeatMs = 15000,
    openTimeoutMs = 10000,
    idFactory = () =>
      (globalThis.crypto?.randomUUID?.() ?? `evt-${Date.now()}-${Math.random()}`),
  }) {
    this.url = url;
    this.WebSocketClass = WebSocketClass;
    this.onEvent = onEvent;
    this.onState = onState;
    this.maxBufferedBytes = maxBufferedBytes;
    this.heartbeatMs = heartbeatMs;
    this.openTimeoutMs = openTimeoutMs;
    this.idFactory = idFactory;
    this.socket = null;
    this.generation = 0;
    this.sessionId = null;
    this.clientSeq = 0;
    this.lastServerSeq = 0;
    this.heartbeatTimer = null;
    this.closed = false;
  }

  connect() {
    if (this.closed) return Promise.reject(new Error("client is closed"));
    this._teardownSocket();
    this.generation += 1;
    const generation = this.generation;
    this.clientSeq = 0;
    this.sessionId = null;
    this.lastServerSeq = 0;
    this.socket = new this.WebSocketClass(this.url);
    this.socket.onmessage = (event) => this._onMessage(generation, event);
    this.socket.onclose = () => {
      if (generation === this.generation) this._handleClose();
    };
    this.socket.onerror = () => {
      /* onclose follows */
    };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (generation === this.generation && this.socket?.readyState !== 1) {
          reject(new Error("connection open timed out"));
          this._teardownSocket();
        }
      }, this.openTimeoutMs);
      this.socket.onopen = () => {
        clearTimeout(timer);
        if (generation !== this.generation) return;
        this._startHeartbeat();
        this.onState?.("connected");
        resolve();
      };
    });
  }

  _startHeartbeat() {
    this._stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      try {
        this._sendEnvelope("ping", {});
      } catch (error) {
        void error;
      }
    }, this.heartbeatMs);
  }

  _stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  _nextClientSeq() {
    this.clientSeq += 1;
    return this.clientSeq;
  }

  _sendEnvelope(type, payload, extra = {}) {
    if (!this.socket || this.socket.readyState !== 1) {
      throw new Error("connection is not open");
    }
    const envelope = {
      v: 1,
      type,
      event_id: this.idFactory(),
      session_id: this.sessionId,
      turn_id: null,
      seq: this._nextClientSeq(),
      ts_ms: Date.now(),
      payload,
      ...extra,
    };
    const serialized = JSON.stringify(envelope);
    if (this.socket.bufferedAmount + serialized.length > this.maxBufferedBytes) {
      this.clientSeq -= 1;
      return false;
    }
    this.socket.send(serialized);
    return true;
  }

  // input_mode: "push_to_talk" (web simulator) or "audio" (phone bridge,
  // continuous streaming with server-side VAD). options carries optional
  // outbound personalization (opening_text / business_background / customer_id)
  // understood by the server session.start handler.
  startSession(scenarioId, inputMode = "push_to_talk", options = {}) {
    const payload = {
      scenario_id: scenarioId,
      input_mode: inputMode,
    };
    if (options.openingText) payload.opening_text = options.openingText;
    if (options.businessBackground) {
      payload.business_background = options.businessBackground;
    }
    if (options.customerId != null) payload.customer_id = options.customerId;
    if (options.gender) payload.gender = options.gender;
    if (options.speaker) payload.speaker = options.speaker;
    // Campaign-template persona overrides (bot_name / speaking_style) and the
    // template id used to merge template scripts into the matcher.
    if (options.botName) payload.bot_name = options.botName;
    if (options.speakingStyle) payload.speaking_style = options.speakingStyle;
    if (options.templateId != null) payload.template_id = options.templateId;
    return this._sendEnvelope("session.start", payload);
  }

  sendAudio(bytes) {
    return this._sendEnvelope("input_audio.append", {
      encoding: "pcm_s16le",
      sample_rate_hz: 16000,
      channels: 1,
      duration_ms: 40,
      audio_b64: bytesToBase64(bytes),
    });
  }

  commitAudio() {
    return this._sendEnvelope("input_audio.commit", {});
  }

  submitText(text) {
    return this._sendEnvelope("user.text.submit", { text });
  }

  cancelResponse(responseId) {
    return this._sendEnvelope("response.cancel", { response_id: responseId });
  }

  endSession({ timeoutMs = 2000 } = {}) {
    if (!this.socket || this.socket.readyState !== 1) {
      return Promise.resolve(null);
    }
    const generation = this.generation;
    return new Promise((resolve) => {
      const finish = (payload) => {
        this._stopHeartbeat();
        resolve(payload);
      };
      const timer = setTimeout(() => finish(null), timeoutMs);
      const previousOnEvent = this.onEvent;
      this.onEvent = (event) => {
        if (generation === this.generation && event.type === "session.ended") {
          clearTimeout(timer);
          this.onEvent = previousOnEvent;
          finish(event);
        } else {
          previousOnEvent?.(event);
        }
      };
      try {
        this._sendEnvelope("session.end", {});
      } catch (error) {
        clearTimeout(timer);
        this.onEvent = previousOnEvent;
        finish(null);
      }
    });
  }

  _onMessage(generation, event) {
    if (generation !== this.generation || this.closed) return;
    let parsed;
    try {
      parsed = JSON.parse(event.data);
    } catch (error) {
      return; // malformed server frame: ignore safely
    }
    if (!parsed || parsed.v !== 1 || typeof parsed.type !== "string") return;
    if (typeof parsed.seq !== "number" || parsed.seq <= this.lastServerSeq) return;
    this.lastServerSeq = parsed.seq;
    if (parsed.type === "session.ready") {
      this.sessionId = parsed.session_id ?? null;
    } else if (this.sessionId !== null && parsed.session_id !== this.sessionId) {
      return; // event from another session must never update this UI
    }
    this.onEvent?.(parsed);
  }

  _handleClose() {
    this._stopHeartbeat();
    if (!this.closed) this.onState?.("disconnected");
  }

  _teardownSocket() {
    this._stopHeartbeat();
    if (this.socket) {
      this.socket.onopen = null;
      this.socket.onmessage = null;
      this.socket.onclose = null;
      this.socket.onerror = null;
      try {
        this.socket.close();
      } catch (error) {
        void error;
      }
      this.socket = null;
    }
  }

  close() {
    this.closed = true;
    this._teardownSocket();
  }
}
