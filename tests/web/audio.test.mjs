// The browser's PCM encoder: run with `node --test tests/web/`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { floatTo16kPcm, toBase64 } from "../../web/audio.js";

test("48 kHz audio becomes a third as many 16 kHz samples", () => {
  assert.equal(floatTo16kPcm(new Float32Array(4800), 48000).length, 1600);
});

test("full-scale samples map to the 16-bit limits and are clipped", () => {
  const out = floatTo16kPcm(Float32Array.from([1, 1, 1, -1, -1, -1, 2, 2, 2]), 48000);
  assert.deepEqual([...out], [32767, -32768, 32767]);
});

test("base64 of raw bytes", () => {
  assert.equal(toBase64(new Uint8Array([0, 1, 255]).buffer), "AAH/");
});
