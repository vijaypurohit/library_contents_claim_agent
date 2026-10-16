# Library Contents Claim Agent

A live voice-and-vision agent that inventories a home library from one camera sweep. It counts, identifies, measures and prices every book from its spine, prices non-book items, measures the room, and produces a claim packet in which every number is traced to a frame or a price source. Totals are computed in code, and anything uncertain goes to a review queue instead of being guessed.

Docs: [Architecture](docs/ARCHITECTURE.md) · [Design](docs/DESIGN.md) · [Implementation plan](docs/IMPLEMENTATION_PLAN.md) · [Testing](docs/TESTING.md) · [Failure log](docs/FAILURE_LOG.md) · [Evaluation results](eval/)

## Setup

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12). For phone access, also `brew install cloudflared`.

```bash
uv sync
cp .env.example .env          # add GEMINI_API_KEY (one key covers Gemini Live and Gemini Flash)
uv run pytest                 # 180+ unit tests: no network, no keys
```

## Configuration

- **`.env`**: API keys only. A stage whose provider has no key moves on to the next provider in its list.
- **`config/settings.yaml`**: everything else.
  - `locale`: country and currency. Shops, item search words and the appraisal threshold are per country in `config/locales/<country>.yaml` (India and the US are provided).
  - `models.profile`: providers for every model stage at once (`gemini`, `openai`, `anthropic`, `groq`, `local`, `dev`); `models.overrides` changes one stage. `--profile <name>` on the command line overrides it for one run.
  - `gateway.cache_only: true` (the default): no model is ever called; stages replay cached answers. Add `--allow-calls` to a command to call models.
  - `live`: voice mode, frame rate and the capture-warning thresholds.
- **`config/targets.yaml`**: accuracy targets the evaluation reports against.

## Running the pipeline on a recording

```bash
uv run claim ingest tests-local-data/video_slow.mp4 --id slow     # video or photos -> runs/slow/
uv run claim --allow-calls run --sweep slow                        # all nine stages
uv run claim stage price --sweep slow                              # rerun one stage
uv run claim run --sweep slow --from price                         # rerun from a stage onward
open runs/slow/report.html                                         # the claim report
uv run claim reprice --sweep slow --country US                     # same inventory, US shops and USD
uv run claim eval --sweep slow                                     # vs ground truth -> eval/results_slow.md
uv run claim export --sweep slow                                   # packet + every referenced file -> release/slow/
uv run claim replay tests-local-data/video_slow.mp4 --id slow      # ingest + all stages, outputs kept under replays/
```

Outputs per sweep (`runs/<id>/`): `claim_packet.json`, `report.html`, `metrics.json` (latency, model calls and cost per stage), `stages/*.json`, annotated shelf images and price-page snapshots under `evidence/`.

## Live agent

```bash
uv run claim --allow-calls serve              # http://127.0.0.1:8000/
uv run claim --allow-calls serve --tunnel     # also prints a phone link: https://<name>.trycloudflare.com/?token=...
```

Open the page, choose a camera (or **Replay a video** to play a recording into the session), choose the voice, and press **Start sweep**. The agent guides the sweep shelf by shelf, warns about blur, glare and panning too fast, and records what you say that changes the claim ("skip the top shelf", "that one is signed"). Say you are done (or press **End sweep**); the pipeline runs and the agent reads back a summary built from the finished packet, with a link to the report. Use headphones so the agent does not hear itself.

- **Gemini Live** (default): audio in and out, and you can interrupt the agent.
- **Cascaded**: the browser's speech recognition and speech synthesis around any vision model (the profile's `converse` chain). Works with every provider; slower.
- **Upload** tab: process a recorded video or photos without the live agent.

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
