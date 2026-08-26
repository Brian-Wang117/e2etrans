// Pure provisional-turn reducer for the streaming transcript. No DOM or I/O.

export function createInitialState() {
  return {
    session: null,
    userLine: null, // {turnId, text, final, translation}
    responses: {}, // responseId -> {text, translation, cancelled}
    lastAudioSeq: {}, // responseId -> last accepted chunk_seq
    turns: [],
    status: "idle",
    error: null,
  };
}

export function reduce(state, event) {
  const next = {
    ...state,
    responses: { ...state.responses },
    lastAudioSeq: { ...state.lastAudioSeq },
    turns: state.turns,
  };
  const payload = event.payload || {};
  switch (event.type) {
    case "session.ready":
      next.session = {
        sessionId: event.session_id,
        scenarioId: payload.scenario_id,
        createdAt: payload.created_at,
        subtitles: payload.subtitles ?? "disabled",
      };
      next.status = "ready";
      next.error = null;
      return next;
    case "session.state":
      next.status = payload.phase || state.status;
      return next;
    case "asr.partial":
      next.userLine = {
        turnId: event.turn_id,
        text: payload.text || "",
        final: false,
        translation: state.userLine?.turnId === event.turn_id
          ? state.userLine.translation
          : null,
      };
      return next;
    case "asr.final":
      next.userLine = {
        turnId: event.turn_id,
        text: payload.text || "",
        final: true,
        translation: state.userLine?.turnId === event.turn_id
          ? state.userLine.translation
          : null,
      };
      return next;
    case "user.translation.done":
      if (next.userLine && next.userLine.turnId === event.turn_id) {
        next.userLine = {
          ...next.userLine,
          translation: { text: payload.text, status: "done" },
        };
      }
      return next;
    case "user.translation.unavailable":
      if (next.userLine && next.userLine.turnId === event.turn_id) {
        next.userLine = {
          ...next.userLine,
          translation: { text: "", status: payload.reason || "failed" },
        };
      }
      return next;
    case "assistant.text.delta": {
      const response = next.responses[payload.response_id];
      if (response?.cancelled) return state;
      next.responses[payload.response_id] = {
        text: (response?.text || "") + (payload.delta || ""),
        translation: response?.translation ?? null,
        cancelled: false,
      };
      return next;
    }
    case "assistant.text.done": {
      const response = next.responses[payload.response_id];
      if (response?.cancelled) return state;
      next.responses[payload.response_id] = {
        text: payload.text || response?.text || "",
        translation: response?.translation ?? null,
        cancelled: false,
      };
      return next;
    }
    case "assistant.translation.done": {
      const response = next.responses[payload.response_id];
      next.responses[payload.response_id] = {
        text: response?.text || "",
        cancelled: response?.cancelled ?? false,
        translation: { text: payload.text, status: "done" },
      };
      return next;
    }
    case "assistant.translation.unavailable": {
      const response = next.responses[payload.response_id];
      next.responses[payload.response_id] = {
        text: response?.text || "",
        cancelled: response?.cancelled ?? false,
        translation: { text: "", status: payload.reason || "failed" },
      };
      return next;
    }
    case "response.cancelled":
    case "local.response.cancelled": {
      const response = next.responses[payload.response_id];
      next.responses[payload.response_id] = {
        text: response?.text || "",
        translation: response?.translation ?? null,
        cancelled: true,
      };
      return next;
    }
    case "turn.completed": {
      const turn = payload.turn;
      if (!turn) return state;
      const turns = next.turns.slice();
      // Replace a provisional user line when its persisted turn arrives.
      if (turn.speaker === "tester" && next.userLine) {
        next.userLine = null;
      }
      turns.push(turn);
      next.turns = turns;
      return next;
    }
    case "session.ended":
      next.status = "ended";
      return next;
    case "error":
      next.error = { code: payload.code, message: payload.message };
      return next;
    case "pong":
      return state;
    default:
      return state;
  }
}

// Audio chunks must be monotonically ordered per response and never accepted
// for cancelled responses.
export function acceptAudioChunk(state, payload) {
  const response = state.responses[payload.response_id];
  if (response?.cancelled) return false;
  const last = state.lastAudioSeq[payload.response_id] || 0;
  if (payload.chunk_seq <= last) return false;
  return true;
}

export function recordAudioChunk(state, payload) {
  return {
    ...state,
    lastAudioSeq: {
      ...state.lastAudioSeq,
      [payload.response_id]: payload.chunk_seq,
    },
  };
}
