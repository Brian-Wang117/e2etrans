// Microphone capture lifecycle and bounded 24kHz streaming playback.

import { pcm16LeBytesToFloat } from "./pcm.js";

export class RealtimeAudio {
  constructor({
    onChunk,
    onPlaybackError,
    maxScheduledSeconds = 30,
    AudioContextClass = globalThis.AudioContext,
    AudioWorkletNodeClass = globalThis.AudioWorkletNode,
    mediaDevices = globalThis.navigator?.mediaDevices,
    workletUrl = "/static/pcm-worklet.js",
  }) {
    this.onChunk = onChunk;
    this.onPlaybackError = onPlaybackError;
    this.maxScheduledSeconds = maxScheduledSeconds;
    this.AudioContextClass = AudioContextClass;
    this.AudioWorkletNodeClass = AudioWorkletNodeClass;
    this.mediaDevices = mediaDevices;
    this.workletUrl = workletUrl;
    this.context = null;
    this.stream = null;
    this.ownsStream = false;
    this.mediaSource = null;
    this.captureNode = null;
    this.capturing = false;
    this.captureAvailable = false;
    this.captureGeneration = 0;
    this.captureStoppedResolvers = new Map();
    this.sources = new Set();
    this.nextStartTime = 0;
    this.muted = false;
    this.phoneDestination = null;
    this.monitorGain = null;
    this.monitorMuted = false;
    this.streamProbe = null;
    this.stopped = false;
  }

  async initializeOutput() {
    if (this.context) return;
    this.context = new this.AudioContextClass();
    await this.context.resume();
  }

  // Capture source defaults to the microphone; phone mode passes the SIP
  // remote stream instead (never owned/stopped by this instance).
  async prepareCapture({ stream = null } = {}) {
    await this.initializeOutput();
    try {
      await this.context.audioWorklet.addModule(this.workletUrl);
      if (stream) {
        this.stream = stream;
        this.ownsStream = false;
        this._attachStreamProbe(stream);
      } else {
        this.stream = await this.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        this.ownsStream = true;
      }
    } catch (error) {
      // Capture source unavailable: output playback stays usable, text input
      // remains.
      this.captureAvailable = false;
      throw error;
    }
    this.mediaSource = this.context.createMediaStreamSource(this.stream);
    this.captureNode = new this.AudioWorkletNodeClass(
      this.context,
      "realtime-pcm-capture"
    );
    this.captureNode.port.onmessage = (event) => this._onWorkletMessage(event.data);
    this.mediaSource.connect(this.captureNode);
    this.captureAvailable = true;
  }

  // Chrome delivers no real samples for a remote WebRTC stream that is only
  // wired into WebAudio nodes: capture stays all-zero silence until an
  // <audio> element consumes the stream. Attach a hidden, muted probe as the
  // consumer whenever we capture an externally provided stream (SIP remote).
  _attachStreamProbe(stream) {
    if (typeof document === "undefined") return;
    if (!this.streamProbe) {
      const probe = document.createElement("audio");
      probe.autoplay = true;
      probe.style.display = "none";
      document.body.appendChild(probe);
      this.streamProbe = probe;
    }
    this.streamProbe.srcObject = stream;
    this.streamProbe.muted = true;
    this.streamProbe.volume = 0;
    this.streamProbe.play().catch(() => {});
  }

  _detachStreamProbe() {
    if (!this.streamProbe) return;
    this.streamProbe.pause();
    this.streamProbe.srcObject = null;
    this.streamProbe.remove();
    this.streamProbe = null;
  }

  _onWorkletMessage(message) {
    if (!message) return;
    if (message.type === "capture.chunk") {
      if (this.capturing && message.generation === this.captureGeneration) {
        this.onChunk(new Uint8Array(message.buffer));
      }
    } else if (message.type === "capture.stopped") {
      const resolve = this.captureStoppedResolvers.get(message.generation);
      if (resolve) {
        this.captureStoppedResolvers.delete(message.generation);
        resolve();
      }
    }
  }

  beginCapture() {
    if (!this.captureNode) return false;
    this.captureGeneration += 1;
    this.capturing = true;
    this.captureNode.port.postMessage({
      type: "capture.start",
      generation: this.captureGeneration,
    });
    return true;
  }

  // Resolves only after the worklet acknowledgement, so every final onChunk
  // call precedes the caller's commit.
  async endCapture({ flush = true } = {}) {
    if (!this.captureNode || !this.capturing) return;
    void flush; // complete chunks are always kept; incomplete tails dropped
    const generation = this.captureGeneration;
    const acknowledged = new Promise((resolve) => {
      this.captureStoppedResolvers.set(generation, resolve);
      setTimeout(resolve, 500); // safety: never hang the UI on a lost ack
    });
    this.capturing = false;
    this.captureNode.port.postMessage({ type: "capture.stop", generation });
    await acknowledged;
  }

  // Phone downlink: playback is mirrored into a MediaStream whose track
  // replaces the SIP send track; local monitoring runs through a gain node.
  enablePhoneOutput() {
    if (!this.context) throw new Error("audio output is not initialized");
    if (!this.phoneDestination) {
      this.phoneDestination = this.context.createMediaStreamDestination();
      this.monitorGain = this.context.createGain();
      this.monitorGain.gain.value = this.monitorMuted ? 0 : 1;
      this.monitorGain.connect(this.context.destination);
    }
    return this.phoneDestination.stream;
  }

  setMonitorMuted(muted) {
    this.monitorMuted = Boolean(muted);
    if (this.monitorGain) {
      this.monitorGain.gain.value = this.monitorMuted ? 0 : 1;
    }
  }

  enqueuePcm24k(bytes) {
    if (!this.context || this.stopped) return false;
    const samples = pcm16LeBytesToFloat(bytes);
    const buffer = this.context.createBuffer(1, samples.length, 24000);
    buffer.copyToChannel(samples, 0);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    if (this.phoneDestination) {
      source.connect(this.phoneDestination);
      source.connect(this.monitorGain);
    } else {
      source.connect(this.context.destination);
    }
    const startAt = Math.max(this.context.currentTime + 0.03, this.nextStartTime);
    if (
      startAt + buffer.duration - this.context.currentTime >
      this.maxScheduledSeconds
    ) {
      this.onPlaybackError?.(new Error("audio playback queue is full"));
      return false;
    }
    if (!this.muted) {
      source.start(startAt);
      this.nextStartTime = startAt + buffer.duration;
    }
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
    return true;
  }

  get hasPendingPlayback() {
    if (!this.context) return false;
    return this.nextStartTime > this.context.currentTime + 0.05;
  }

  cancelPlayback() {
    for (const source of this.sources) {
      try {
        source.onended = null;
        source.stop();
      } catch (error) {
        void error;
      }
    }
    this.sources.clear();
    this.nextStartTime = this.context ? this.context.currentTime : 0;
  }

  setMuted(muted) {
    this.muted = Boolean(muted);
    if (this.muted) this.cancelPlayback();
  }

  async stop() {
    if (this.stopped) return;
    this.stopped = true;
    this.capturing = false;
    this.cancelPlayback();
    this._detachStreamProbe();
    if (this.captureNode) {
      try {
        this.captureNode.port.onmessage = null;
        this.captureNode.disconnect();
      } catch (error) {
        void error;
      }
      this.captureNode = null;
    }
    if (this.mediaSource) {
      try {
        this.mediaSource.disconnect();
      } catch (error) {
        void error;
      }
      this.mediaSource = null;
    }
    if (this.stream) {
      if (this.ownsStream) {
        for (const track of this.stream.getTracks()) {
          track.stop();
        }
      }
      this.stream = null;
      this.ownsStream = false;
    }
    if (this.monitorGain) {
      try {
        this.monitorGain.disconnect();
      } catch (error) {
        void error;
      }
      this.monitorGain = null;
      this.phoneDestination = null;
    }
    if (this.context) {
      try {
        await this.context.close();
      } catch (error) {
        void error;
      }
      this.context = null;
    }
  }
}
