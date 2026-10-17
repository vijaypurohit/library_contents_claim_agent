"""Record a simulated demo: the real web client, in Chrome, running one scripted live sweep.

What is real and what is simulated:
    real       the web page, the server, the cascaded voice agent and its tool calls, the capture
               warnings, the provisional count, every pipeline stage, the report and the packet
    simulated  the camera (Chrome's fake camera plays the reference feed from config/live_script.yaml,
               built from recordings of the test space; faults marked synthetic there), the user's
               speech (the script's lines are handed to the page's speech-recognition handler and
               spoken by a macOS voice), and the agent's voice (what the page asks the browser to
               speak is rendered with a macOS voice at the same moment)
A banner across the top of the page says so for the whole recording. It is one continuous take:
nothing is cut, sped up or stitched.

Run (uses model calls; the dev profile answers through the local `claude` command):
    uv run --with playwright playwright install ffmpeg          # once: the page recorder
    uv run --with playwright --with imageio-ffmpeg python scripts/record_demo.py
Needs Google Chrome and macOS `say`. Writes runs/<sweep>/demo/demo.mp4 (plus page.webm, audio.wav
and timeline.json).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claimagent.core.settings import Settings  # noqa: E402
from claimagent.live.simulate import AGENT_WAIT_S, Script, build_feed, shaken, start_server  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
VIEWPORT = {"width": 1280, "height": 800}
LEAD_IN_S = 14          # the camera rests on the first view while the agent greets the user
TAIL_S = 30             # the last view is held this long, so Chrome's fake camera never loops back
AGENT_VOICE, USER_VOICE = "Samantha", "Aman"
RATE = 22050            # audio sample rate of the soundtrack
BANNER = ("SIMULATED SWEEP · camera: recorded footage of the test space (glare, blur and fast-pan faults added) · "
          "user: scripted lines, spoken by a computer voice · agent, warnings, count and pipeline: live "
          "(Claude Haiku via the dev profile) · one continuous take")

# Runs in the page before its own scripts: the banner, a speech-recognition stand-in the recorder
# can "speak" into, and a log of everything the page asks the browser to say.
INIT_SCRIPT = """
window.__spoken = [];
speechSynthesis.speak = (u) => window.__spoken.push({ t: Date.now(), text: u.text });
speechSynthesis.cancel = () => window.__spoken.push({ t: Date.now(), cancel: true });
class ScriptedRecognition { constructor() { window.__recognition = this; } start() {} stop() {} }
window.SpeechRecognition = ScriptedRecognition;
window.webkitSpeechRecognition = ScriptedRecognition;
window.__hear = (text) => window.__recognition?.onresult?.({ results: [[{ transcript: text }]] });
addEventListener("DOMContentLoaded", () => {
  const b = document.createElement("div");
  b.textContent = BANNER_TEXT;
  b.style.cssText = "position:fixed;bottom:0;left:0;right:0;z-index:9;padding:6px 12px;font:12px system-ui;" +
                    "background:#7a1f1f;color:#fff;text-align:center";
  document.body.appendChild(b);
});
""".replace("BANNER_TEXT", json.dumps(BANNER))


def write_y4m(path: Path, frames: list[np.ndarray], fps: int) -> None:
    """Frames as a raw YUV4MPEG2 file, the format Chrome's fake camera reads."""
    h, w = frames[0].shape[:2]
    with path.open("wb") as out:
        out.write(f"YUV4MPEG2 W{w} H{h} F{fps}:1 Ip A1:1 C420jpeg\n".encode())
        for frame in frames:
            out.write(b"FRAME\n")
            out.write(cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).tobytes())


def camera_file(script: Script, fps: float, folder: Path) -> tuple[Path, float]:
    """The reference feed with a lead-in and a long tail, as a Y4M file. Returns (path, feed seconds)."""
    feed = [cv2.imdecode(np.frombuffer(f.jpeg, np.uint8), cv2.IMREAD_COLOR) for f in build_feed(script.camera, fps)]
    lead = [shaken(feed[0], k) for k in range(int(LEAD_IN_S * fps))]
    tail = [shaken(feed[-1], k) for k in range(int(TAIL_S * fps))]
    path = folder / "camera.y4m"
    write_y4m(path, lead + feed + tail, int(fps))
    return path, len(feed) / fps


def speech(text: str, voice: str, folder: Path, n: int) -> np.ndarray:
    """One line rendered by macOS `say`, as 16-bit samples at RATE."""
    path = folder / f"line_{n}.wav"
    subprocess.run(["say", "-v", voice, "-o", str(path), "--file-format=WAVE", f"--data-format=LEI16@{RATE}", text],
                   check=True)
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16)


def soundtrack(timeline: list[dict], start: float, length_s: float, folder: Path) -> Path:
    """Mix the agent's and the user's lines at the moments they happened.

    The agent's lines queue one after another, as the browser's speech queue does, and a user line
    cuts the agent off (the page cancels speech when it hears the user)."""
    track = np.zeros(int((length_s + 30) * RATE), np.int32)
    agent_free = 0.0                     # when the agent's previous line finishes
    agent_cut: float | None = None       # a cancel stops queued agent speech
    for n, event in enumerate(sorted(timeline, key=lambda e: e["t"])):
        at = event["t"] - start
        if event.get("cancel"):
            agent_free, agent_cut = at, at
            continue
        voice = USER_VOICE if event["who"] == "user" else AGENT_VOICE
        clip = speech(event["text"], voice, folder, n).astype(np.int32)
        if event["who"] == "agent":
            at = max(at, agent_free)
            agent_free = at + len(clip) / RATE
        begin = int(at * RATE)
        if event["who"] == "agent" and agent_cut is not None and at < agent_cut:
            continue
        end = min(len(track), begin + len(clip))
        track[begin:end] += clip[:end - begin]
    path = folder / "audio.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.clip(track, -32768, 32767).astype(np.int16).tobytes())
    return path


def main() -> None:
    import imageio_ffmpeg
    from playwright.sync_api import sync_playwright

    settings = Settings.load()
    settings.gateway.cache_only = False      # a live sweep needs fresh answers
    settings.models.profile = "dev"
    settings.live.voice = "cascaded"
    script = Script.load(ROOT / "config" / "live_script.yaml")
    fps = settings.live.frame_fps
    work = Path(tempfile.mkdtemp(prefix="claim-demo-"))
    camera, feed_s = camera_file(script, fps, work)
    print(f"camera: {feed_s:.1f} s of reference feed after a {LEAD_IN_S} s lead-in")

    ws_url, server = start_server(settings)
    http_url = ws_url.replace("ws://", "http://")
    timeline: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True, args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-video-capture={camera}"])
        context = browser.new_context(viewport=VIEWPORT, record_video_dir=str(work), record_video_size=VIEWPORT,
                                      permissions=["camera", "microphone"])
        context.add_init_script(INIT_SCRIPT)
        page = context.new_page()
        start = time.time()                  # the page recording starts about now
        page.goto(http_url + "/")
        page.wait_for_function("document.getElementById('config').textContent.length > 0")
        page.wait_for_timeout(1500)
        page.click("#start")
        started = time.time()

        def agent_turns() -> int:
            return page.locator(".turn.agent").count()

        def wait_until(condition, limit: float) -> bool:
            deadline = time.time() + limit
            while not condition():
                if time.time() > deadline:
                    return False
                page.wait_for_timeout(250)
            return True

        def camera_t() -> float:
            return time.time() - started - LEAD_IN_S

        answered = 0
        for line in script.user:
            if line.after_agent:
                wait_until(lambda: agent_turns() > answered, AGENT_WAIT_S)
            if line.camera_t is not None:
                wait_until(lambda: camera_t() >= line.camera_t, 3600)
            if line.camera_done:
                wait_until(lambda: camera_t() >= feed_s, 3600)
            answered = agent_turns()
            timeline.append({"t": time.time(), "who": "user", "text": line.say})
            page.evaluate("text => window.__hear(text)", line.say)
            print(f"[{time.time() - start:6.1f}s] user: {line.say}")
            if line.end and not wait_until(lambda: "processing" in page.inner_text("#status")
                                           or "ready" in page.inner_text("#status"), script.end_grace_s):
                print("the agent did not end the sweep; pressing End")
                page.click("#end")

        print("waiting for the claim packet…")
        page.wait_for_selector("#result a", timeout=script.packet_timeout_s * 1000)
        print(f"[{time.time() - start:6.1f}s] packet ready: {page.inner_text('#result')}")
        page.wait_for_timeout(12000)          # the summary is read out
        report = page.get_attribute("#result a", "href")
        sweep_id = report.split("/")[2]
        spoken = page.evaluate("window.__spoken")   # what the page said, read before leaving it
        page.goto(http_url + report)
        for _ in range(24):                   # scroll through the report
            page.mouse.wheel(0, 500)
            page.wait_for_timeout(900)
        page.goto(f"{http_url}/api/sweeps/{sweep_id}/packet")
        for _ in range(8):
            page.mouse.wheel(0, 700)
            page.wait_for_timeout(700)
        length = time.time() - start
        video = page.video.path()
        context.close()
        browser.close()
    server.should_exit = True

    out = settings.runs_dir / sweep_id / "demo"
    out.mkdir(parents=True, exist_ok=True)
    timeline += [{"t": e["t"] / 1000, "who": "agent", "text": e.get("text", ""), **({"cancel": True} if e.get("cancel") else {})}
                 for e in spoken]
    (out / "timeline.json").write_text(json.dumps(sorted(timeline, key=lambda e: e["t"]), indent=2, ensure_ascii=False))
    audio = soundtrack(timeline, start, length, work)
    Path(video).replace(out / "page.webm")
    audio.replace(out / "audio.wav")
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(out / "page.webm"),
                    "-i", str(out / "audio.wav"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(out / "demo.mp4")], check=True)
    print(f"demo: {out / 'demo.mp4'} ({length:.0f} s)")


if __name__ == "__main__":
    main()
