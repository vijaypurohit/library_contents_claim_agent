# Library Contents Claim Agent

A live voice-and-vision agent that inventories a home library from one camera sweep. It counts, identifies, measures and prices every book from its spine, prices non-book items, measures the room, and writes a claim packet in which every number traces to a frame or a price source. Totals are computed in code, and anything uncertain goes to a review queue instead of being guessed.

Docs: [Architecture](docs/ARCHITECTURE.md) · [Design](docs/DESIGN.md) · [Testing](docs/TESTING.md) · [Failure log](docs/FAILURE_LOG.md) · [Requirements](docs/requirements.md) · [Evaluation results](eval/)

## 1. Set up (5 minutes)

Needs [uv](https://docs.astral.sh/uv/) (it installs Python 3.12). Chrome or Safari for the live page; `brew install cloudflared` only if you want to use a phone.

```bash
uv sync
cp .env.example .env      # then put a GEMINI_API_KEY in it (one key covers Gemini Live and Gemini Flash)
uv run pytest             # about 200 tests, no network and no keys needed
```

### The three options you will see in commands

| Option | Plain meaning |
|---|---|
| `--allow-calls` | "Yes, you may call the AI models." Without it the program refuses to call any model, so nothing is billed by accident. Put it **before** the command name: `uv run claim --allow-calls replay …`. |
| `--profile NAME` | *Which* AI models answer. Leave it out for the default (`gemini`). Use `--profile dev` if you have no API key (see the table). |
| `--id NAME` | A **name for this run**. Its results go in `runs/NAME/`. Optional: if you leave it out, a time-stamped name is made. Using the same name again replaces that run. |

| Profile | Needs | Use it for |
|---|---|---|
| `gemini` (default) | `GEMINI_API_KEY` in `.env` | normal use, live voice included |
| `openai`, `anthropic`, `groq` | that provider's key | the same stages on another provider |
| `local` | Ollama running | no network |
| `dev` | the `claude` command-line tool, logged in | trying everything with no API key (Claude Haiku: slow, about 10 minutes per sweep, and chat or browser-speech only) |

## 2. Run it on a recorded video or photos (offline path)

Any phone video (`.mp4`, `.mov`) or photos (`.jpg`, `.heic`) of shelves works; give them in capture order. Film slowly, left to right, top shelf first, with the spines readable.

```bash
uv run claim --allow-calls replay path/to/shelves.mp4 --id mine     # all nine stages; prints each stage as it runs
open runs/mine/report.html                                          # the claim report (Linux: xdg-open)
```

(No API key? Add `--profile dev`. No `--id`? A time-stamped name is used and printed at the end.) You get, in `runs/mine/`: `claim_packet.json`, `report.html`, `metrics.json` (time and cost per stage), annotated shelf images and price-page snapshots under `evidence/`. Add `--profile dev` if you have no API key.

Useful follow-ups on the same sweep (no new model calls):

```bash
uv run claim reprice --sweep mine --country US     # same books priced for another country (IN and US are provided)
uv run claim export --sweep mine                   # packet + every frame and price page it cites -> release/mine/
uv run claim run --sweep mine --from price         # rerun from one stage onward
uv run claim sessions                              # list everything in runs/: how each was made and what it produced
uv run claim clean mine                            # delete a run (asks first); `claim clean --unfinished` removes runs with no packet
```

For real dimensions, put a printed scale card in view: `uv run claim marker --out marker.pdf`, print it at 100%, and tape it to a shelf. Without a scale, sizes stay empty and the packet says why.

## 3. Run the live agent

```bash
uv run claim --allow-calls serve          # then open http://127.0.0.1:8000/
```

On the page: pick a camera, pick **Talk by** (below), press **Start sweep**, film the shelves, and press **End sweep** (or just say or type "I'm done"). The agent greets you, guides the sweep shelf by shelf, warns about blur, glare and panning too fast, and records what you say that changes the claim ("skip the top shelf", "that one is signed").

- **Talk by:** *Chat* (you type, the agent types back: no microphone, saves Gemini Live quota), *Voice: Gemini Live* (speak, it speaks back, you can interrupt; headphones please) or *Voice: browser speech* (the browser listens and speaks for any model).
- **While you film:** the **claim sheet** (a sheet of paper under the chat) fills in live: books in view, what the agent logged, what you told it, shelves to film again, capture problems.
- **After End sweep:** a progress card shows the nine steps with a running timer and what you can do meanwhile (keep chatting, add corrections, or close the page). A correction made after the report exists gets an **Update report** button. When it is done, the report opens from the chat, from the card, or inline.
- **History tab:** every session, how it was made (live, simulated, upload, replay), its result, its report and its conversation. Sessions can be deleted there. A session with no camera frames is not kept.
- **No camera?** Choose **Replay a video…** as the camera and pick a recording: it plays into the session as if it were the camera. The **Upload** tab processes a recorded video without the live agent.
- **Phone:** `uv run claim --allow-calls serve --tunnel` prints an `https://….trycloudflare.com/?token=…` link; open it on the phone.

**What the terminal shows.** The server prints what is happening, so you can tell what works:

```
14:02:11 INFO  live      session live-20261017-140211 opened (voice chat, models: dev, model calls allowed)
14:02:19 INFO  live      agent: Hi! We're pricing this claim in India, in rupees…
14:02:31 INFO  live      capture warning (glare): There is glare on the spines…
14:02:40 INFO  live      tool record_user_fact {"kind": "first_edition", …} -> {"ok": true, …}
14:03:05 INFO  pipeline  stage 3/9 detect started (live-20261017-140211)
14:05:10 INFO  pipeline  stage 3/9 detect done in 125.0 s (4 model calls)
```

Add `--log-level debug` for more (frame batches), or `warning` for problems only: `uv run claim --allow-calls --log-level debug serve`. A yellow banner on the page warns when model calls are off. Each session also keeps its own log, `runs/<id>/live_log.jsonl`.

## 4. Run the tests (optional)

You do not need this to use the app. These commands check that the code itself works; run them after changing something.

```bash
uv run pytest                      # unit tests: no network, no keys
node --test tests/web/             # the browser's audio encoder
```

These two need the recordings of the test space, which are kept locally and not published (`tests-local-data/`), so skip them on a fresh clone:

```bash
uv run pytest -o addopts="" tests/integration          # replays a cached sweep through every stage
uv run claim --allow-calls --profile dev simulate      # a scripted live session with a reference camera feed, end to end
```

`claim simulate` starts the real server, streams a camera feed built from the recordings (glare, blur and a fast pan added), plays a scripted user, and writes the packet plus `runs/<id>/simulation/transcript.md`. Edit `config/live_script.yaml` to point it at your own videos. To compare any packet with your own hand-made ground truth (same format as `ground_truth/almirah/truth.yaml`): `uv run claim eval --sweep mine --truth my_truth.yaml`. Details: [Testing](docs/TESTING.md).

## Configuration

- **`.env`**: API keys only. A stage whose provider has no key moves on to the next provider in its list.
- **`config/settings.yaml`**: everything else: `locale` (country and currency; shops, item search words and the appraisal threshold are per country in `config/locales/<country>.yaml`), `models` (profiles and per-stage overrides), `live` (voice, frame rate, capture-warning thresholds), `gateway.cache_only`.
- **`config/targets.yaml`**: accuracy targets the evaluation reports against.

## Results on the test captures

Count 10 of 10 on all four sweeps; titles correct 78–89% on the steady sweeps (33% on the rushed one); no confidently wrong titles anywhere; every price sourced, dated and snapshotted, with 4 regional books that have no listing anywhere left unpriced. Prices agree with the hand check on only 1–2 of 6 books, and dimensions and room are empty because the captures have no scale: see [the failure log](docs/FAILURE_LOG.md) for root causes and a measured fix.

## Models, libraries and tools

- **Models:** Gemini Live (live voice), Gemini Flash (detect, spines, items; fallbacks Flash-Lite and a newer Flash), Claude Haiku through the `claude` CLI (development profile, used to read the test spines). Any of OpenAI, Anthropic, Groq or Ollama through LiteLLM by configuration.
- **Libraries:** FastAPI, uvicorn, google-genai, LiteLLM, OpenCV (ArUco, ORB, homographies, quality checks), Pillow + pillow-heif, Pydantic, RapidFuzz, httpx, Jinja2, Typer.
- **Data sources:** Open Library and Google Books (catalogues), Oxford Bookstore, Flipkart and AbeBooks (prices), Frankfurter (ECB currency rates).
- **AI coding tools:** Claude Code and ChatGPT were used to help write and review the code and documents.

## Reference app: kept, changed, dropped

Built on the pattern of the open-source [Insurance Claim Live Agent Team](https://github.com/Shubhamsaboo/awesome-llm-apps/tree/main/voice_ai_agents/insurance_claim_live_agent_team) example.

| | What |
|---|---|
| **Kept** | The Gemini Live bridge over a JSON WebSocket: 16 kHz PCM16 up, 24 kHz down, input and output transcription, the user interrupting the agent, camera frames as video input, tool calls answered with tool responses; local-only access by default |
| **Changed** | The agent directs a capture instead of taking a statement; its tools record observations, re-capture requests and claim facts, and end the sweep; every frame is recorded into a sweep the pipeline reads; capture-quality warnings feed the conversation; a cascaded voice mode works with any provider; phone access through a token-protected tunnel |
| **Dropped** | The ADK agent graph, policy lookup, incident sketches and the avatar |

## Assumptions

1. Books are shelved spine-out; books shelved cover-out or in closed cabinets are not counted.
2. Rooms are rectangles unless the sweep shows more than four walls. Wall area is gross; doors and windows are listed, not subtracted.
3. Used value is reported only from a real used listing, and never above the replacement cost.
4. An item's depth is left empty: it can't be seen from the front.
5. Replacement cost is today's price of the current edition, not the edition on the shelf.

## Test data and limitations

The system is built to the full specification. The test data it was checked on is smaller than the target scenario, because a larger physical space wasn't available:

| Target scenario | Test data actually used | Effect on reported results |
|---|---|---|
| At least 60 books across at least 2 shelving units | About 10 books on 1 almirah, in 3 photos and 3 short videos (`tests-local-data/`, kept locally and not published) | Counting and identification are tested at small scale |
| At least 8 non-book items in one enclosed room | The almirah only; no room footage | Item detection and pricing are tested in code and on the almirah; item recall is measured on 1 item |
| One continuous sweep of the whole room | Videos of 2–6 seconds of the shelves | Room dimensions and areas come out empty, with review reasons |
| Metric scale from a reference object in view | No scale card or A4 sheet in any capture; no catalogue heights for these editions | Dimensions come out empty, with review reasons; the scale rungs are tested on synthetic images |
| 20 hand-measured spines and a tape-measured room | Not available | Dimension and room accuracy are reported as "not measured" |
| Live sweep on a phone | Laptop camera on `localhost`; phone access through a `cloudflared` tunnel is supported | Live agent tested mainly at a desk |

Other limitations: shops that refuse plain page requests (Amazon, ThriftBooks, Bookswagon, Sapna Online) are skipped and listed; the depth scale rung and local detectors are not included; Google Books needs its own key once the shared anonymous quota is used up.

**Ground truth that was collected:** the true book count, hand-read titles and authors, and hand-checked prices from the same kind of online shops the system uses (`ground_truth/almirah/truth.yaml`). Accuracy for count, identification and pricing is measured against these in `eval/`.
