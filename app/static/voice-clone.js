// Voice cloning: 45s mic sampling -> server relay upload -> training poll ->
// localStorage store. The clone result is just a speaker id, consumed by the
// workbench speaker selector and sent with session.start.

import { RealtimeAudio } from "./realtime-audio.js";

export const VOICES_KEY = "doubao_custom_voices";
export const SELECTED_KEY = "doubao_selected_speaker";
export const MODEL_TYPE_ICL_V2 = 4;
export const CLONE_SLOTS = ["S_heWZZwa62", "S_Ip5CTwa62", "S_HxRPesDd2"];

// Already-trained voices provisioned outside the in-app sampling flow
// (e.g. created in the Volcengine console). Shown in the speaker selector
// without training; managed here instead of per-browser localStorage.
export const READY_CLONED_VOICES = [
  { speaker_id: "S_HxRPesDd2", name: "已就绪复刻音色", model_type: MODEL_TYPE_ICL_V2 },
];

const MIN_SAMPLE_SECONDS = 40;
const STATUS_POLL_MS = 2000;
const STATUS_TIMEOUT_MS = 120000;

// External URL prefix when deployed behind one (e.g. nginx /v2); injected
// into the page by the server. Empty when served at the domain root.
const APP_BASE = window.APP_BASE || "";

export async function fetchCloneMeta() {
  const response = await fetch(`${APP_BASE}/api/voice-clone/meta`);
  if (!response.ok) throw new Error("音色复刻服务不可用");
  return response.json();
}

// 16k mono PCM16LE bytes -> minimal WAV -> base64 for the relay endpoint.
export function pcmToWavBase64(pcmBytes, sampleRate = 16000) {
  const dataSize = pcmBytes.length;
  const header = new DataView(new ArrayBuffer(44));
  const writeStr = (offset, text) => {
    for (let i = 0; i < text.length; i += 1) header.setUint8(offset + i, text.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  header.setUint32(4, 36 + dataSize, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  header.setUint32(16, 16, true);
  header.setUint16(20, 1, true); // PCM
  header.setUint16(22, 1, true); // mono
  header.setUint32(24, sampleRate, true);
  header.setUint32(28, sampleRate * 2, true);
  header.setUint16(32, 2, true);
  header.setUint16(34, 16, true);
  writeStr(36, "data");
  header.setUint32(40, dataSize, true);
  const wav = new Uint8Array(44 + dataSize);
  wav.set(new Uint8Array(header.buffer), 0);
  wav.set(pcmBytes, 44);
  let binary = "";
  const step = 0x8000;
  for (let i = 0; i < wav.length; i += step) {
    binary += String.fromCharCode(...wav.subarray(i, i + step));
  }
  return btoa(binary);
}

// localStorage-backed store with an observer list (same shape as the
// reference project): entries {speaker_id, name, model_type, time}.
export class VoiceCloneStore {
  constructor() {
    this.listeners = new Set();
  }

  getVoices() {
    try {
      const parsed = JSON.parse(localStorage.getItem(VOICES_KEY) || "[]");
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  saveVoice({ speaker_id: speakerId, name, model_type: modelType = MODEL_TYPE_ICL_V2 }) {
    const voices = this.getVoices().filter((voice) => voice.speaker_id !== speakerId);
    voices.push({
      speaker_id: speakerId,
      name: name || speakerId,
      model_type: modelType,
      time: Date.now(),
    });
    localStorage.setItem(VOICES_KEY, JSON.stringify(voices));
    this.notify();
  }

  removeVoice(speakerId) {
    const voices = this.getVoices().filter((voice) => voice.speaker_id !== speakerId);
    localStorage.setItem(VOICES_KEY, JSON.stringify(voices));
    if (this.getSelected() === speakerId) this.setSelected("");
    this.notify();
  }

  getSelected() {
    return localStorage.getItem(SELECTED_KEY) || "";
  }

  // "" = default (gender crossing / DOUBAO_TTS_SPEAKER), else a clone id.
  setSelected(speakerId) {
    if (speakerId) localStorage.setItem(SELECTED_KEY, speakerId);
    else localStorage.removeItem(SELECTED_KEY);
    this.notify();
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  notify() {
    for (const listener of this.listeners) listener(this);
  }
}

// Samples the microphone for `seconds` using the existing 16k mono PCM16
// capture pipeline. Resolves with the concatenated PCM bytes.
export async function recordSample({ seconds, onProgress }) {
  const chunks = [];
  const audio = new RealtimeAudio({
    onChunk: (bytes) => chunks.push(bytes),
    onPlaybackError: () => {},
  });
  try {
    await audio.prepareCapture();
  } catch (error) {
    await audio.stop();
    throw new Error("麦克风不可用：请检查浏览器权限后重试");
  }
  if (!audio.beginCapture()) {
    await audio.stop();
    throw new Error("采样启动失败，请重试");
  }
  const started = Date.now();
  try {
    while (Date.now() - started < seconds * 1000) {
      await new Promise((resolve) => setTimeout(resolve, 200));
      onProgress?.((Date.now() - started) / (seconds * 1000));
    }
  } finally {
    await audio.endCapture();
    await audio.stop();
  }
  onProgress?.(1);
  const total = chunks.reduce((size, chunk) => size + chunk.length, 0);
  const pcm = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    pcm.set(chunk, offset);
    offset += chunk.length;
  }
  if (total < MIN_SAMPLE_SECONDS * 16000 * 2) {
    throw new Error("采样时长不足，请按住朗读完整文本后重试");
  }
  return pcm;
}

// Upload + poll until the training finishes (status 2/4). Only a ready voice
// lands in localStorage. onStatus surfaces human-readable progress.
export async function uploadClone({ speakerId, name, pcm, store, onStatus }) {
  const audioB64 = pcmToWavBase64(pcm);
  onStatus?.("上传采样音频…");
  const uploadResponse = await fetch(`${APP_BASE}/api/voice-clone/upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ speaker_id: speakerId, name, audio_wav_b64: audioB64 }),
  });
  if (!uploadResponse.ok) {
    const detail = await uploadResponse.json().catch(() => ({}));
    throw new Error(detail.detail || `上传失败（HTTP ${uploadResponse.status}）`);
  }
  const deadline = Date.now() + STATUS_TIMEOUT_MS;
  onStatus?.("已提交训练，等待音色就绪…");
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, STATUS_POLL_MS));
    const statusResponse = await fetch(`${APP_BASE}/api/voice-clone/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speaker_id: speakerId }),
    });
    if (!statusResponse.ok) {
      const detail = await statusResponse.json().catch(() => ({}));
      throw new Error(detail.detail || `状态查询失败（HTTP ${statusResponse.status}）`);
    }
    const result = await statusResponse.json();
    if (result.ready) {
      store.saveVoice({ speaker_id: speakerId, name, model_type: MODEL_TYPE_ICL_V2 });
      onStatus?.(`音色 ${name || speakerId} 训练完成，可在音色选择中使用`);
      return result;
    }
  }
  throw new Error("训练超时：请稍后在音色选择里重新查询该槽位状态");
}
