// Microphone capture as 16 kHz PCM16, and playback of the agent's 24 kHz PCM16 with barge-in.

export function floatTo16kPcm(input, inputRate) {
  // Resample by averaging the samples that fall into each 16 kHz slot, then convert to 16-bit integers.
  const ratio = inputRate / 16000;
  const length = Math.floor(input.length / ratio);
  const out = new Int16Array(length);
  for (let i = 0; i < length; i += 1) {
    const start = Math.floor(i * ratio), end = Math.min(Math.floor((i + 1) * ratio), input.length);
    let sum = 0;
    for (let j = start; j < end; j += 1) sum += input[j];
    const sample = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
    out[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return out;
}

export function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

export class Microphone {
  // Calls onChunk(base64 PCM16) about ten times a second.
  constructor(onChunk) { this.onChunk = onChunk; }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    this.context = new AudioContext();
    const source = this.context.createMediaStreamSource(this.stream);
    // ScriptProcessor is old but works everywhere without a separate worklet file.
    this.node = this.context.createScriptProcessor(4096, 1, 1);
    this.node.onaudioprocess = (e) => {
      const pcm = floatTo16kPcm(e.inputBuffer.getChannelData(0), this.context.sampleRate);
      this.onChunk(toBase64(pcm.buffer));
    };
    source.connect(this.node);
    this.node.connect(this.context.destination);
  }

  stop() {
    this.node?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.context?.close();
  }
}

export class Speaker {
  // Queues 24 kHz PCM16 chunks back to back; interrupt() drops whatever is still queued.
  constructor() { this.context = new AudioContext({ sampleRate: 24000 }); this.next = 0; this.sources = []; }

  play(base64) {
    const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
    const pcm = new Int16Array(bytes.buffer);
    const buffer = this.context.createBuffer(1, pcm.length, 24000);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    const start = Math.max(this.context.currentTime, this.next);
    source.start(start);
    this.next = start + buffer.duration;
    this.sources.push(source);
    source.onended = () => { this.sources = this.sources.filter((s) => s !== source); };
  }

  interrupt() {
    this.sources.forEach((s) => { try { s.stop(); } catch { /* already stopped */ } });
    this.sources = [];
    this.next = 0;
  }
}
