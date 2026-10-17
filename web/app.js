// Web client: live session (chat or voice) with a claim sheet and progress, History, Upload and Settings.
import { Microphone, Speaker } from "/web/audio.js";

const $ = (id) => document.getElementById(id);
const token = new URLSearchParams(location.search).get("token");
const withToken = (path) => (token ? `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : path);
const api = async (path, options) => {
  const response = await fetch(withToken(path), options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `${response.status}`);
  return body;
};
const el = (tag, className, text) => { const e = document.createElement(tag); if (className) e.className = className; if (text !== undefined) e.textContent = text; return e; };

// The nine pipeline stages, in order, with a plain name for each.
const STAGES = [
  ["keyframes", "Pick the sharp frames"], ["scale", "Look for a scale reference"], ["detect", "Find books and items"],
  ["inventory", "Count each book once"], ["identify", "Read the spines"], ["measure", "Measure"],
  ["price", "Look up prices"], ["room", "Room size"], ["packet", "Build the report"],
];

let config = {};
let socket = null, mic = null, speaker = null, frameTimer = null, recognition = null;
let voiceMode = "chat", speaksWarnings = false, sweepId = null;
let agentTurn = null, userTurn = null;
let processingStart = null, tickTimer = null, issues = {};

// ---- Tabs ----
const TABS = ["session", "history", "upload", "settings"];
function showTab(name) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  TABS.forEach((t) => { $(`tab-${t}`).hidden = t !== name; });
  if (name === "history") loadHistory();
}
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

function setStatus(text, kind = "") { const chip = $("status"); chip.textContent = text; chip.className = `chip ${kind}`; }

// ---- Camera sources; "Replay a video" plays a recording in place of the camera ----
async function listSources() {
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
  // Evidence frame: long side config.evidence_side (default 1920) px, JPEG. Spine crops are cut from these.
  const video = $("preview");
  if (!video.videoWidth || video.paused || video.ended) return null;
  const scale = Math.min(1, (config.evidence_side || 1920) / Math.max(video.videoWidth, video.videoHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.85).split(",")[1];
}

// ---- Conversation ----
function addTurn(kind, text, box = $("transcript")) {
  const div = el("div", `turn ${kind}`, text);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

function addReportLink(url, text) {
  const div = el("div", "turn result");
  div.append(text + " ");
  const a = el("a", "", "Open the claim report");
  a.href = withToken(url); a.target = "_blank"; a.rel = "noopener";
  div.append(a);
  $("transcript").appendChild(div);
  $("transcript").scrollTop = $("transcript").scrollHeight;
}

function speak(text) {
  if (voiceMode !== "cascaded" || !("speechSynthesis" in window)) return;   // chat mode is silent
  speechSynthesis.speak(new SpeechSynthesisUtterance(text));
}

function showWarning(message) {
  const box = $("warning");
  box.textContent = message;
  box.style.display = "block";
  clearTimeout(showWarning.timer);
  showWarning.timer = setTimeout(() => { box.style.display = "none"; }, 5000);
}

// ---- Claim sheet ----
function addToList(id, text) {
  const list = $(id);
  list.querySelectorAll(".pending").forEach((li) => li.remove());
  list.appendChild(el("li", "", text));
}

function noteIssue(kind) {
  issues[kind] = (issues[kind] || 0) + 1;
  const box = $("sheet-issues");
  box.innerHTML = "";
  const names = { blur: "blurry", glare: "glare", motion: "panned too fast" };
  Object.entries(issues).forEach(([k, n]) => box.append(el("span", "tag", `${names[k] || k} ×${n}`)));
}

function resetSheet() {
  issues = {};
  [["sheet-obs", "nothing yet"], ["sheet-facts", "nothing yet"], ["sheet-refilm", "nothing asked"]].forEach(([id, text]) => {
    $(id).innerHTML = ""; $(id).appendChild(el("li", "pending", text));
  });
  $("sheet-issues").innerHTML = '<span class="pending">none so far</span>';
  $("count").textContent = "–"; $("items").textContent = "other items: none seen yet"; $("items").className = "pending";
  $("sheet-result").hidden = true; $("sheet-stamp").innerHTML = "";
  $("report-card").hidden = true; $("progress-card").hidden = true; $("report-frame").hidden = true;
  $("report-toggle").textContent = "Show it here"; $("regenerate").hidden = true;
}

async function fillResult() {
  try {
    const packet = await api(`/api/sweeps/${sweepId}/packet`);
    const t = packet.totals, cur = packet.sweep.currency;
    const money = (v) => (v == null ? "–" : new Intl.NumberFormat(undefined, { style: "currency", currency: cur, maximumFractionDigits: 0 }).format(v));
    const rows = [
      ["Books counted", t.book_count], ["With a title", t.books_identified], ["Could not be read", t.books_unidentified],
      ["Books, replacement cost", money(t.books_replacement_cost)],
      ["Other items", `${money(t.items_replacement_cost_low)} to ${money(t.items_replacement_cost_high)}`],
      ["Points for a person to check", packet.review_queue.length],
    ];
    const box = $("sheet-totals");
    box.innerHTML = "";
    rows.forEach(([label, value]) => { const p = el("p", "field"); p.append(el("span", "", label), el("b", "", String(value))); box.append(p); });
    $("sheet-result").hidden = false;
    $("sheet-stamp").innerHTML = '<span class="stamp">REPORT READY</span>';
  } catch { /* the report itself still opens */ }
}

// ---- Progress ----
function buildSteps() {
  const list = $("steps");
  list.innerHTML = "";
  STAGES.forEach(([key, label]) => {
    const li = el("li");
    li.dataset.stage = key;
    li.append(el("span", "dot", "○"), el("span", "", label), el("span", "t", ""));
    list.append(li);
  });
  $("bar").style.width = "0";
}

function showProgress() {
  $("progress-card").hidden = false;
  $("progress-title").textContent = "Processing your sweep";
  buildSteps();
  processingStart = Date.now();
  const slow = config.profile === "dev";
  $("elapsed").textContent = "";
  $("progress-card").dataset.eta = slow
    ? "Expect about 10 minutes: this profile reads every spine through the local claude command."
    : "Usually 1 to 3 minutes with hosted models.";
  clearInterval(tickTimer);
  tickTimer = setInterval(tick, 1000);
  tick();
}

function tick() {
  if (!processingStart) return;
  const s = Math.floor((Date.now() - processingStart) / 1000);
  $("elapsed").textContent = `Working for ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}. ${$("progress-card").dataset.eta || ""}`;
}

function updateStep(event) {
  const li = $("steps").querySelector(`[data-stage="${event.stage}"]`);
  if (!li) return;
  li.className = event.state === "done" ? "done" : "running";
  li.querySelector(".dot").innerHTML = event.state === "done" ? "✓" : '<span class="spin">◌</span>';
  if (event.state === "done") li.querySelector(".t").textContent = `${event.elapsed_s.toFixed(1)} s`;
  const done = $("steps").querySelectorAll("li.done").length;
  $("bar").style.width = `${Math.round((100 * done) / STAGES.length)}%`;
}

function finishProgress(ok) {
  clearInterval(tickTimer); processingStart = null;
  $("progress-title").textContent = ok ? "Processing finished" : "Processing failed";
  if (ok) { $("bar").style.width = "100%"; $("steps").querySelectorAll("li").forEach((li) => { li.className = "done"; li.querySelector(".dot").textContent = "✓"; }); }
}

// ---- Live session messages ----
function handle(message) {
  switch (message.type) {
    case "ready":
      speaksWarnings = message.speaks_warnings;
      sweepId = message.sweep_id;
      setStatus("recording", "live");
      $("sheet-id").textContent = sweepId; $("sheet-id").className = "";
      $("sheet-locale").textContent = `${config.country} · ${config.currency}`;
      addTurn("note", `Session ${sweepId} is saved as you go (History tab).`);
      break;
    case "audio": speaker?.play(message.data); break;
    case "interrupted": speaker?.interrupt(); agentTurn = null; break;
    case "transcript": {
      const kind = message.speaker === "user" ? "user" : "agent";
      if (kind === "agent") { agentTurn = agentTurn || addTurn("agent", ""); agentTurn.textContent = message.text; }
      else { userTurn = userTurn || addTurn("user", ""); userTurn.textContent = message.text; }
      if (message.final) {
        if (kind === "agent") speak(message.text);
        if (kind === "agent") agentTurn = null; else userTurn = null;
      }
      $("transcript").scrollTop = $("transcript").scrollHeight;
      break;
    }
    case "turn_complete": agentTurn = null; userTurn = null; break;
    case "warning":
      showWarning(message.message); addTurn("warn", `⚠ ${message.message}`); noteIssue(message.kind);
      if (message.speak) speak(message.message);
      break;
    case "inventory":
      $("count").textContent = message.spines;
      $("items").textContent = message.items.length ? `other items in view: ${message.items.join(", ")}` : "other items: none in this view";
      $("items").className = "";
      break;
    case "tool": {
      const a = message.args || {};
      const labels = { log_observation: "Logged", request_recapture: "Asked to film again", record_user_fact: "Noted", end_sweep: "Sweep ended", sweep_status: "Checked status" };
      addTurn("note", `${labels[message.name] || message.name}${a.text ? `: ${a.text}` : a.where ? `: ${a.where}` : ""}`);
      if (message.name === "log_observation") addToList("sheet-obs", `${a.text}${a.where ? ` (${a.where})` : ""}`);
      if (message.name === "record_user_fact") addToList("sheet-facts", `${a.text}${a.target ? ` [${a.target}]` : ""}`);
      if (message.name === "request_recapture") addToList("sheet-refilm", `${a.where}: ${a.reason}`);
      break;
    }
    case "progress": updateStep(message); break;
    case "stale": $("regenerate").hidden = false; addTurn("note", "Saved. The report was already made: press “Update report” to include this."); break;
    case "status":
      if (message.status === "processing") {
        setStatus("processing", "busy");
        stopCapture();
        $("regenerate").hidden = true;
        showProgress();
      } else if (message.status === "ready") {
        setStatus("report ready", "live");
        finishProgress(true);
        $("report-card").hidden = false;
        $("report-summary").textContent = message.summary || "";
        $("report-open").href = withToken(message.report);
        $("report-frame").dataset.src = withToken(message.report);
        if ($("report-frame").hidden === false) $("report-frame").src = $("report-frame").dataset.src;
        if (message.summary) { addTurn("result", message.summary); if (voiceMode === "cascaded") speak(message.summary); }
        addReportLink(message.report, "The claim report is ready.");
        fillResult();
      } else if (message.status === "failed") {
        setStatus("failed", "bad");
        finishProgress(false);
        addTurn("bad", `Processing failed: ${message.error || "unknown error"}. The frames are kept; see History.`);
      }
      break;
    case "error": addTurn("bad", `⚠ ${message.message}`); break;
  }
}

function startRecognition() {
  // Voice (browser speech): the browser turns speech into text and sends it as a typed turn.
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) { addTurn("note", "This browser has no speech recognition: type to the agent instead."); return; }
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
  resetSheet();
  $("transcript").innerHTML = "";
  try {
    await openSource();
    voiceMode = $("voice").value;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(withToken(`${scheme}://${location.host}/ws/session?voice=${voiceMode}`));
    socket.onmessage = (e) => handle(JSON.parse(e.data));
    socket.onclose = () => {
      if ($("status").textContent === "recording") setStatus("disconnected", "bad");
      stopCapture(); socket = null;
    };
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    if (voiceMode === "gemini") {
      speaker = new Speaker();
      mic = new Microphone((data) => socket?.readyState === 1 && socket.send(JSON.stringify({ type: "audio", data })));
      await mic.start();
    } else if (voiceMode === "cascaded") {
      startRecognition();
    }                                                  // chat: no microphone, no speech
    frameTimer = setInterval(() => {
      const data = grabFrame();
      if (data && socket?.readyState === 1) socket.send(JSON.stringify({ type: "frame", data }));
    }, 1000 / (config.frame_fps || 2));
    $("end").disabled = false;
  } catch (error) {
    addTurn("bad", `Could not start: ${error.message || error}`);
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
$("regenerate").addEventListener("click", () => { $("regenerate").hidden = true; socket?.send(JSON.stringify({ type: "regenerate" })); });
$("report-toggle").addEventListener("click", () => {
  const frame = $("report-frame");
  frame.hidden = !frame.hidden;
  if (!frame.hidden) frame.src = frame.dataset.src;
  $("report-toggle").textContent = frame.hidden ? "Show it here" : "Hide it";
});
const sendText = () => {
  const text = $("text").value.trim();
  if (text && socket) {
    socket.send(JSON.stringify({ type: "text", text }));
    if (voiceMode === "gemini") addTurn("user", text);     // the other modes echo the user's turn from the server
    $("text").value = "";
  }
};
$("send").addEventListener("click", sendText);
$("text").addEventListener("keydown", (e) => { if (e.key === "Enter") sendText(); });
$("voice").addEventListener("change", showModeHint);

function showModeHint() {
  const hints = {
    chat: "You type, the agent types back. Uses the cascaded model, so it saves Gemini Live quota.",
    gemini: "Speak to the agent; it speaks back. Use headphones.",
    cascaded: "The browser listens and speaks for the agent (Chrome works best).",
  };
  $("mode-hint").textContent = hints[$("voice").value] || "";
}

// ---- History: every session, with its report and conversation ----
const fmtTime = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

async function loadHistory() {
  const body = $("history-rows");
  try {
    const rows = await api("/api/sweeps");
    body.innerHTML = "";
    if (!rows.length) body.innerHTML = '<tr><td colspan="6" class="muted">No sessions yet.</td></tr>';
    rows.forEach((r) => {
      const tr = el("tr");
      const how = [r.label, r.voice && r.origin === "live" ? `(${r.voice})` : "", r.profile ? `· ${r.profile}` : ""].filter(Boolean).join(" ");
      const result = r.books != null ? `${r.books} books, ${r.identified} titled` : "–";
      const actions = el("div", "actions");
      if (r.report) { const a = el("a", "button", "Report"); a.href = withToken(`/sweeps/${r.id}/report.html`); a.target = "_blank"; a.rel = "noopener"; actions.append(a); }
      if (r.has_log) { const b = el("button", "secondary", "Conversation"); b.onclick = () => showConversation(r); actions.append(b); }
      const del = el("button", "danger", "Delete");
      del.disabled = ["recording", "processing"].includes(r.status);
      del.onclick = async () => {
        if (!confirm(`Delete ${r.id}? Its frames, report and conversation are removed for good.`)) return;
        try { await api(`/api/sweeps/${r.id}`, { method: "DELETE" }); loadHistory(); } catch (e) { alert(e.message); }
      };
      actions.append(del);
      const badge = el("span", `badge ${r.status}`, r.status);
      [fmtTime(r.created_at), how, badge, result, r.id].forEach((cell) => { const td = el("td"); td.append(cell instanceof Node ? cell : document.createTextNode(cell)); tr.append(td); });
      const td = el("td"); td.append(actions); tr.append(td);
      body.append(tr);
    });
  } catch (e) { body.innerHTML = `<tr><td colspan="6" class="muted">Could not load: ${e.message}</td></tr>`; }
}

async function showConversation(row) {
  const view = $("history-view"), log = $("history-log");
  view.hidden = false; log.innerHTML = "";
  $("history-title").textContent = `Conversation of ${row.id}`;
  const events = await api(`/api/sweeps/${row.id}/log`);
  events.forEach((e) => {
    if (e.type === "transcript" && e.final) addTurn(e.speaker === "user" ? "user" : "agent", e.text, log);
    else if (e.type === "warning") addTurn("warn", `⚠ ${e.message}`, log);
    else if (e.type === "tool") addTurn("note", `${e.name.replaceAll("_", " ")}${e.args?.text ? `: ${e.args.text}` : ""}`, log);
    else if (e.type === "status" && e.summary) addTurn("result", e.summary, log);
    else if (e.type === "error") addTurn("bad", e.message, log);
  });
  if (!log.children.length) addTurn("note", "No conversation was recorded for this session.", log);
  view.scrollIntoView({ behavior: "smooth" });
}
$("refresh").addEventListener("click", loadHistory);

// ---- Upload ----
$("upload").addEventListener("click", async () => {
  const files = $("upload-files").files;
  if (!files.length) return;
  const form = new FormData();
  [...files].forEach((f) => form.append("files", f));
  $("upload-status").textContent = "Uploading…";
  let body;
  try { body = await api("/api/sweeps/upload", { method: "POST", body: form }); }
  catch (e) { $("upload-status").textContent = `Upload failed: ${e.message}`; return; }
  const poll = async () => {
    const s = await api(`/api/sweeps/${body.sweep_id}/status`);
    if (s.status === "processing") {
      const name = (STAGES.find(([k]) => k === s.stage) || [])[1];
      $("upload-status").textContent = `Processing ${body.sweep_id}${name ? `: ${name} (step ${STAGES.findIndex(([k]) => k === s.stage) + 1} of ${STAGES.length}, ${s.state === "done" ? "done" : "running"})` : "…"}`;
      setTimeout(poll, 3000); return;
    }
    $("upload-status").innerHTML = s.report ? `Ready: <a target="_blank" href="${withToken(s.report)}">open the claim report</a> (also in History)`
      : `Finished with status ${s.status}${s.error ? `: ${s.error}` : ""}`;
  };
  poll();
});

// ---- Settings ----
async function loadConfig() {
  config = await api("/api/config");
  $("voice").value = config.voice === "gemini" && !config.has_gemini_key ? "chat" : config.voice;
  if (!["chat", "gemini", "cascaded"].includes($("voice").value)) $("voice").value = "chat";
  $("country").value = config.country;
  $("profile").innerHTML = "";
  config.profiles.forEach((p) => $("profile").add(new Option(p, p, false, p === config.profile)));
  $("config").textContent = `Gemini key ${config.has_gemini_key ? "present" : "missing"} · models: ${config.profile} · ` +
    (config.cache_only ? "model calls OFF" : "model calls allowed");
  $("calls-off").hidden = !config.cache_only;
  $("calls-off").textContent = "Model calls are off (cache-only), so the agent cannot answer. Stop the server and start it with:  uv run claim --allow-calls serve";
  showModeHint();
}
$("save").addEventListener("click", async () => {
  const currency = { IN: "INR", US: "USD" }[$("country").value];
  await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ country: $("country").value, currency, profile: $("profile").value, voice: $("voice").value }) });
  await loadConfig();
});

listSources();
loadConfig();
