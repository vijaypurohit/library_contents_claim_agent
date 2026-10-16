// Web client: live sweep (camera or replayed video), upload, and settings.
import { Microphone, Speaker } from "/web/audio.js";

const $ = (id) => document.getElementById(id);
const token = new URLSearchParams(location.search).get("token");
const withToken = (path) => (token ? `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : path);
let config = {};
let socket = null, mic = null, speaker = null, frameTimer = null, recognition = null, speaksWarnings = true;
let agentTurn = null, userTurn = null;

// ---- Tabs ----
document.querySelectorAll("nav button").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b === button));
  ["session", "upload", "settings"].forEach((t) => { $(`tab-${t}`).hidden = t !== button.dataset.tab; });
}));

function setStatus(text) { $("status").textContent = text; }

// ---- Sources: cameras, plus "Replay a video" which plays a recorded file instead of the camera ----
async function listSources() {
  // The fallback choices come first, so the list is usable even if device listing never answers.
  const select = $("source");
  select.innerHTML = "";
  select.add(new Option("Default camera", ""));
  select.add(new Option("Replay a video…", "replay"));
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    devices.filter((d) => d.kind === "videoinput" && d.deviceId)
      .forEach((d, i) => select.add(new Option(d.label || `Camera ${i + 1}`, d.deviceId), select.options.length - 1));
  } catch { /* no camera access yet: the default camera is still offered */ }
}

async function openSource() {
  const video = $("preview");
  if ($("source").value === "replay") {
    const file = await new Promise((resolve) => { $("replay-file").onchange = (e) => resolve(e.target.files[0]); $("replay-file").click(); });
    if (!file) throw new Error("no video chosen");
    video.srcObject = null;
    video.src = URL.createObjectURL(file);
    video.loop = false;
  } else {
    const deviceId = $("source").value;
    video.srcObject = await navigator.mediaDevices.getUserMedia({
      video: deviceId ? { deviceId: { exact: deviceId }, width: { ideal: 1920 } } : { facingMode: "environment", width: { ideal: 1920 } },
    });
  }
  await video.play();
}

function grabFrame() {
  // Evidence frame: long side 1280 px, JPEG.
  const video = $("preview");
  if (!video.videoWidth || video.paused || video.ended) return null;
  const scale = Math.min(1, 1280 / Math.max(video.videoWidth, video.videoHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.85).split(",")[1];
}

// ---- Transcript ----
function addTurn(kind, text) {
  const div = document.createElement("div");
  div.className = `turn ${kind}`;
  div.textContent = text;
  $("transcript").appendChild(div);
  $("transcript").scrollTop = $("transcript").scrollHeight;
  return div;
}

function speak(text) {
  if (!("speechSynthesis" in window)) return;
  speechSynthesis.speak(new SpeechSynthesisUtterance(text));
}

function showWarning(message) {
  const box = $("warning");
  box.textContent = message;
  box.style.display = "block";
  clearTimeout(showWarning.timer);
  showWarning.timer = setTimeout(() => { box.style.display = "none"; }, 5000);
}

// ---- Live session ----
function handle(message) {
  switch (message.type) {
    case "ready":
      speaksWarnings = message.speaks_warnings;
      setStatus(`recording · ${message.sweep_id}`);
      $("result").textContent = "";
      break;
    case "audio": speaker?.play(message.data); break;
    case "interrupted": speaker?.interrupt(); agentTurn = null; break;
    case "transcript": {
      const kind = message.speaker === "user" ? "user" : "agent";
      if (kind === "agent") { agentTurn = agentTurn || addTurn("agent", ""); agentTurn.textContent = message.text; }
      else { userTurn = userTurn || addTurn("user", ""); userTurn.textContent = message.text; }
      if (message.final) {
        if (kind === "agent" && $("voice").value === "cascaded") speak(message.text);
        if (kind === "agent") agentTurn = null; else userTurn = null;
      }
      break;
    }
    case "turn_complete": agentTurn = null; userTurn = null; break;
    case "warning":
      showWarning(message.message);
      if (message.speak) speak(message.message);
      break;
    case "inventory":
      $("count").textContent = message.spines;
      $("items").textContent = message.items.length ? `Also in view: ${message.items.join(", ")}` : "";
      break;
    case "tool": addTurn("tool", `${message.name.replaceAll("_", " ")}${message.args?.text ? `: ${message.args.text}` : ""}`); break;
    case "status":
      setStatus(message.status);
      if (message.status === "processing") stopCapture();
      if (message.summary) {
        $("result").innerHTML = "";
        $("result").append(message.summary + " ");
        if (!speaksWarnings) speak(message.summary);
      }
      if (message.report) {
        const link = document.createElement("a");
        link.href = withToken(message.report); link.target = "_blank"; link.textContent = "Open the claim report";
        $("result").append(link);
      }
      if (message.error) $("result").textContent = `Processing failed: ${message.error}`;
      break;
    case "error": addTurn("tool", `⚠ ${message.message}`); break;
  }
}

function startRecognition() {
  // Cascaded voice: the browser turns speech into text and sends it as a typed turn.
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) { addTurn("tool", "This browser has no speech recognition: type to the agent instead."); return; }
  recognition = new Recognition();
  recognition.continuous = true;
  recognition.interimResults = false;
  recognition.lang = "en-IN";
  recognition.onresult = (e) => {
    const text = e.results[e.results.length - 1][0].transcript.trim();
    if (text) { speechSynthesis.cancel(); socket?.send(JSON.stringify({ type: "text", text })); }
  };
  recognition.onend = () => { if (socket) recognition.start(); };
  recognition.start();
}

async function start() {
  $("start").disabled = true;
  try {
    await openSource();
    const voice = $("voice").value;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(withToken(`${scheme}://${location.host}/ws/session?voice=${voice}`));
    socket.onmessage = (e) => handle(JSON.parse(e.data));
    socket.onclose = () => { setStatus($("status").textContent.startsWith("recording") ? "disconnected" : $("status").textContent); stopCapture(); socket = null; };
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    if (voice === "gemini") {
      speaker = new Speaker();
      mic = new Microphone((data) => socket?.readyState === 1 && socket.send(JSON.stringify({ type: "audio", data })));
      await mic.start();
    } else {
      startRecognition();
    }
    frameTimer = setInterval(() => {
      const data = grabFrame();
      if (data && socket?.readyState === 1) socket.send(JSON.stringify({ type: "frame", data }));
    }, 1000 / (config.frame_fps || 2));
    $("end").disabled = false;
  } catch (error) {
    addTurn("tool", `⚠ Could not start: ${error.message || error}`);
    $("start").disabled = false;
  }
}

function stopCapture() {
  clearInterval(frameTimer);
  mic?.stop(); mic = null;
  if (recognition) { const r = recognition; recognition = null; r.onend = null; r.stop(); }
  const video = $("preview");
  video.srcObject?.getTracks?.().forEach((t) => t.stop());
  video.pause();
  $("end").disabled = true;
  $("start").disabled = false;
}

$("start").addEventListener("click", start);
$("end").addEventListener("click", () => socket?.send(JSON.stringify({ type: "end" })));
const sendText = () => {
  const text = $("text").value.trim();
  if (text && socket) { socket.send(JSON.stringify({ type: "text", text })); if ($("voice").value === "gemini") addTurn("user", text); $("text").value = ""; }
};
$("send").addEventListener("click", sendText);
$("text").addEventListener("keydown", (e) => { if (e.key === "Enter") sendText(); });

// ---- Upload ----
$("upload").addEventListener("click", async () => {
  const files = $("upload-files").files;
  if (!files.length) return;
  const form = new FormData();
  [...files].forEach((f) => form.append("files", f));
  $("upload-status").textContent = "Uploading…";
  const response = await fetch(withToken("/api/sweeps/upload"), { method: "POST", body: form });
  const body = await response.json();
  if (!response.ok) { $("upload-status").textContent = `Upload failed: ${body.detail}`; return; }
  const poll = async () => {
    const status = await (await fetch(withToken(`/api/sweeps/${body.sweep_id}/status`))).json();
    if (status.status === "processing") { $("upload-status").textContent = `Processing ${body.sweep_id}…`; setTimeout(poll, 3000); return; }
    $("upload-status").innerHTML = status.report ? `Ready: <a target="_blank" href="${withToken(status.report)}">open the claim report</a>`
      : `Finished with status ${status.status}${status.error ? `: ${status.error}` : ""}`;
  };
  poll();
});

// ---- Settings ----
async function loadConfig() {
  config = await (await fetch(withToken("/api/config"))).json();
  $("voice").value = config.voice;
  $("country").value = config.country;
  $("profile").innerHTML = "";
  config.profiles.forEach((p) => $("profile").add(new Option(p, p, false, p === config.profile)));
  $("config").textContent = `Gemini key ${config.has_gemini_key ? "present" : "missing"} · ` +
    (config.cache_only ? "cache-only mode: new sweeps need the server started with --allow-calls" : "model calls allowed");
}
$("save").addEventListener("click", async () => {
  const currency = { IN: "INR", US: "USD" }[$("country").value];
  await fetch(withToken("/api/settings"), { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ country: $("country").value, currency, profile: $("profile").value, voice: $("voice").value }) });
  await loadConfig();
});

listSources();
loadConfig();
