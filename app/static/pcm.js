// Pure PCM helpers: float<->PCM16LE conversion, streaming resampling, base64.
// Little-endian order and unaligned byte views are handled explicitly.

export function floatToPcm16LeBytes(samples) {
  if (!(samples instanceof Float32Array) || samples.length === 0) {
    throw new TypeError("non-empty Float32 input is required");
  }
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }
  return bytes;
}

export function pcm16LeBytesToFloat(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length === 0) {
    throw new TypeError("non-empty byte input is required");
  }
  if (bytes.length % 2 !== 0) {
    throw new TypeError("PCM16 byte count must be even");
  }
  const count = bytes.length / 2;
  const out = new Float32Array(count);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let i = 0; i < count; i += 1) {
    const sample = view.getInt16(i * 2, true);
    out[i] = sample < 0 ? sample / 0x8000 : sample / 0x7fff;
  }
  return out;
}

// Linear-interpolation streaming resampler that carries phase across quanta.
// Output positions live on a fixed grid, so arbitrarily split input produces
// exactly the same samples as one continuous stream.
export class StreamingResampler {
  constructor(inputRate, outputRate) {
    if (!(inputRate > 0) || !(outputRate > 0)) {
      throw new TypeError("positive sample rates are required");
    }
    this.inputRate = inputRate;
    this.outputRate = outputRate;
    this.ratio = inputRate / outputRate;
    this.nextOut = 0;
    this.offset = 0;
  }

  reset() {
    this.nextOut = 0;
    this.offset = 0;
  }

  push(input) {
    if (!(input instanceof Float32Array) || input.length === 0) {
      throw new TypeError("non-empty Float32 input is required");
    }
    const start = this.offset;
    const end = start + input.length;
    const out = [];
    // Need both interpolation samples inside this push, so stop before end-1.
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
    return Float32Array.from(out);
  }
}

export function bytesToBase64(bytes) {
  if (!(bytes instanceof Uint8Array)) {
    throw new TypeError("Uint8Array is required");
  }
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

export function base64ToBytes(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}
