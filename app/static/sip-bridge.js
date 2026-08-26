// JsSIP bridge: registers against a SIP websocket gateway, dials real phone
// numbers, and hands the remote/local media over to the realtime pipeline.
// Depends on the vendored classic script (window.JsSIP).

const CONFIG_KEY = "e2etrans-sip-config-v1";

export const DEFAULT_SIP_CONFIG = {
  server: "lark1.ocssaas.com",
  port: "15122",
  transport: "wss",
  domain: "dntest.ocssaas.com",
  user: "10015",
  password: "arvato@123",
};

export function loadSipConfig() {
  try {
    const raw = localStorage.getItem(CONFIG_KEY);
    if (!raw) return { ...DEFAULT_SIP_CONFIG };
    return { ...DEFAULT_SIP_CONFIG, ...JSON.parse(raw) };
  } catch (error) {
    return { ...DEFAULT_SIP_CONFIG };
  }
}

export function saveSipConfig(config) {
  localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
}

function jssip() {
  if (!globalThis.JsSIP) {
    throw new Error("JsSIP 未加载");
  }
  return globalThis.JsSIP;
}

// Maps JsSIP internal causes to operator-friendly hints (Chinese UI).
const CAUSE_HINTS = {
  Unavailable:
    "线路侧返回 4xx（480/408/410/430）：被叫号码可能不存在、未注册或不符合该线路的路由规则",
  "Authentication Error": "鉴权失败：账号或密码不正确",
  "Not Found": "被叫号码不存在（404）",
  "Busy": "对方忙线",
  "No Answer": "对方无应答",
  Rejected: "对方/线路拒接",
  "Connection Error": "与 SIP 服务器的 WebSocket 连接中断",
  "Request Timeout": "等待线路响应超时（408）",
};

function formatFailure(data) {
  const cause = data?.cause || "呼叫失败";
  // JsSIP maps 480/410/408/430 to the generic "Unavailable" cause and drops
  // the status code; surface it (plus the server reason phrase) so operators
  // can tell which 4xx the trunk actually returned.
  const status = data?.message?.status_code;
  const reason = data?.message?.reason_phrase;
  const sipPart = status ? `（SIP ${status}${reason ? ` ${reason}` : ""}）` : "";
  const hint = CAUSE_HINTS[cause] ? ` · ${CAUSE_HINTS[cause]}` : "";
  return `${cause}${sipPart}${hint}`;
}

export class SipBridge {
  // onState receives: registering, registered, registration-failed,
  // dialing, in-call, call-ended, call-failed, unregistered, error.
  constructor({ onState }) {
    this.onState = onState || (() => {});
    this.ua = null;
    this.session = null;
    this.registered = false;
    this.callNumber = null;
    this.domain = null;
  }

  _emit(state, detail = "") {
    this.onState(state, detail);
  }

  // Resolves when registration succeeds, rejects on failure or timeout.
  register(config) {
    const JsSIP = jssip();
    // Keep JsSIP signaling logs in the console for call-failure diagnosis
    // (INVITE/reply exchange is otherwise invisible in DevTools network tab).
    try {
      JsSIP.debug.enable("JsSIP:*");
    } catch (error) {
      void error;
    }
    this.unregister();
    this._emit("registering");
    return new Promise((resolve, reject) => {
      try {
        const socketUrl = `${config.transport}://${config.server}:${config.port}`;
        const socket = new JsSIP.WebSocketInterface(socketUrl);
        // JsSIP exposes no configuration.domain; keep the account domain here
        // so dial() can build a valid Request-URI.
        this.domain = config.domain;
        this.ua = new JsSIP.UA({
          sockets: [socket],
          uri: `sip:${config.user}@${config.domain}`,
          password: config.password,
          display_name: config.user,
          authorization_user: config.user,
          // Pin the digest realm to the account domain: some trunks challenge
          // INVITEs with a realm that mismatches the UA default and reject
          // the re-authenticated request as "Authentication Error".
          realm: config.domain,
          register: true,
          register_expires: 300,
          connection_recovery_min_interval: 2,
          connection_recovery_max_interval: 30,
        });
      } catch (error) {
        this._emit("registration-failed", error.message);
        reject(error);
        return;
      }
      const timer = setTimeout(() => {
        this.registered = false;
        this._emit("registration-failed", "连接超时");
        reject(new Error("SIP 注册超时"));
      }, 15000);
      this.ua.on("registered", () => {
        clearTimeout(timer);
        this.registered = true;
        this._emit("registered");
        resolve();
      });
      this.ua.on("registrationFailed", (data) => {
        clearTimeout(timer);
        this.registered = false;
        const cause = data?.cause || "";
        this._emit("registration-failed", String(cause));
        reject(new Error(`SIP 注册失败：${cause}`));
      });
      // Inbound calls are out of scope for the AI bridge: decline politely.
      this.ua.on("newRTCSession", (data) => {
        if (data.originator === "remote") {
          data.session.terminate({ status_code: 486 });
        }
      });
      this.ua.start();
    });
  }

  unregister() {
    if (this.session) {
      try {
        this.session.terminate();
      } catch (error) {
        void error;
      }
      this.session = null;
    }
    if (this.ua) {
      try {
        this.ua.stop();
      } catch (error) {
        void error;
      }
      this.ua = null;
    }
    this.registered = false;
    this.callNumber = null;
    this.domain = null;
  }

  // Places an outbound call. localStream (optional) replaces the default
  // microphone so the caller hears nothing until the AI track is swapped in.
  dial(number, { localStream = null, onConfirmed, onEnded } = {}) {
    if (!this.ua || !this.registered) {
      throw new Error("SIP 尚未注册");
    }
    if (this.session) {
      throw new Error("已有通话在进行");
    }
    this.callNumber = number;
    this._emit("dialing", number);
    const options = {
      mediaConstraints: { audio: true, video: false },
    };
    // JsSIP >= 3.9 lets us provide the outgoing stream directly.
    if (localStream && typeof localStream.getAudioTracks === "function") {
      options.rtcMediaStreamFactory = () => Promise.resolve(localStream);
    }
    const session = this.ua.call(`sip:${number}@${this.domain}`, options);
    this.session = session;
    session.on("progress", () => this._emit("dialing", "对方振铃中…"));
    session.on("confirmed", () => {
      const remoteStream =
        session.connection?.getRemoteStreams?.()[0] || null;
      this._emit("in-call", number);
      onConfirmed?.(remoteStream);
    });
    const finish = (detail) => {
      if (this.session !== session) return;
      this.session = null;
      this._emit("call-ended", detail || "");
      onEnded?.(detail || "");
    };
    session.on("ended", () => finish("对方已挂断"));
    session.on("failed", (data) => {
      const detail = formatFailure(data);
      this._emit("call-failed", detail);
      finish(detail);
    });
    return session;
  }

  // Swaps the outbound audio track (silent or mic) for the AI voice stream.
  replaceSendTrack(track) {
    const connection = this.session?.connection;
    if (!connection || !track) return false;
    const sender = connection
      .getSenders()
      .find((entry) => entry.track && entry.track.kind === "audio");
    if (!sender) return false;
    sender.replaceTrack(track);
    return true;
  }

  hangup() {
    if (this.session) {
      try {
        this.session.terminate();
      } catch (error) {
        void error;
      }
      this.session = null;
      this._emit("call-ended", "已挂断");
    }
  }

  get inCall() {
    return this.session !== null;
  }
}
