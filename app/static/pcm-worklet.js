// AudioWorklet processor: captures device-rate mono floats, resamples to
// 16kHz PCM16LE and posts complete 40ms / 1280-byte chunks. Microphone audio
// is never connected to the destination.

class StreamingResamplerLocal {
  constructor(inputRate, outputRate) {
    this.ratio = inputRate / outputRate;
    this.nextOut = 0;
    this.offset = 0;
  }

  reset() {
    this.nextOut = 0;
    this.offset = 0;
  }

  push(input) {
    const start = this.offset;
    const end = start + input.length;
    const out = [];
    while (this.nextOut < end - 1) {
      const position = this.nextOut;
      const local = position < start ? start : position;
      const index = Math.floor(local - start);
      const fraction = local - start - index;
      const a = input[index];
      const b = input[index + 1];
      out.push(a + (b - a) * fraction);
      this.nextOut += this.ratio;
    }
    this.offset = end;
    return out;
  }
}

class RealtimePcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.resampler = null;
    this.capturing = false;
    this.generation = 0;
    this.pending = new Int16Array(0);
    this.CHUNK_SAMPLES = 640; // 40ms at 16kHz -> 1280 bytes
    this.port.onmessage = (event) => this.handleMessage(event.data);
  }

  handleMessage(message) {
    if (!message || typeof message.type !== "string") return;
    if (message.type === "capture.start") {
      this.generation = message.generation || 0;
      this.pending = new Int16Array(0);
      if (this.resampler) this.resampler.reset();
      this.capturing = true;
    } else if (message.type === "capture.stop") {
      const generation = message.generation || 0;
      this.capturing = false;
      // Drop any incomplete tail; only complete 40ms chunks are sent.
      this.pending = new Int16Array(0);
      this.port.postMessage({ type: "capture.stopped", generation });
    }
  }

  process(inputs) {
    if (!this.capturing || !inputs || !inputs[0] || !inputs[0][0]) {
      return true;
    }
    if (!this.resampler) {
      this.resampler = new StreamingResamplerLocal(sampleRate, 16000);
    }
    const resampled = this.resampler.push(inputs[0][0]);
    if (resampled.length === 0) return true;

    const merged = new Int16Array(this.pending.length + resampled.length);
    merged.set(this.pending, 0);
    for (let i = 0; i < resampled.length; i += 1) {
      const clamped = Math.max(-1, Math.min(1, resampled[i]));
      merged[this.pending.length + i] =
        clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }

    const fullChunks = Math.floor(merged.length / this.CHUNK_SAMPLES);
    if (fullChunks > 0) {
      const complete = merged.subarray(0, fullChunks * this.CHUNK_SAMPLES);
      this.pending = merged.slice(fullChunks * this.CHUNK_SAMPLES);
      const buffer = complete.buffer.slice(
        complete.byteOffset,
        complete.byteOffset + complete.byteLength
      );
      this.port.postMessage(
        { type: "capture.chunk", generation: this.generation, buffer },
        [buffer]
      );
    } else {
      this.pending = merged;
    }
    return true;
  }
}

registerProcessor("realtime-pcm-capture", RealtimePcmCapture);
