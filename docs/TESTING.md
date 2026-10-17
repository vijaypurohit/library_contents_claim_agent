# Library Contents Claim Agent: Testing

Run these steps in order from the repository root. Each step lists the command, then what to check. Every command runs on the recorded captures in `tests-local-data/`; nothing needs re-capturing.

Sweep ids used throughout:

| Id | Source | Purpose |
|---|---|---|
| `photos` | `still_photo_above.heic`, `still_photo_below.heic`, `still_photo_both.heic` | Best case: sharp stills |
| `slow` | `video_slow.mp4` | Normal sweep |
| `rush` | `video_rush.mp4` | Fast, blurry sweep |
| `far` | `video_far_focus.mp4` | Distant sweep |

## 1. Setup

```bash
uv sync
cp .env.example .env
```

Open `.env` and add at least `GEMINI_API_KEY`. Keys for other providers are optional.

```bash
uv run claim --help
```

Check: the help text lists `ingest`, `stage`, `run`, `reprice`, `eval`, `export`, `replay`, `serve` and `marker`.

**Optional local model** (only for stages set to the `local` profile or an `ollama/...` provider):

```bash
brew install ollama && ollama serve &
ollama pull qwen2.5vl:7b
```

## 2. Unit tests

These need no network or API keys.

```bash
uv run pytest tests/unit -q
node --test tests/web/          # the browser's audio encoder
```

Run one area at a time while working on it:

```bash
uv run pytest tests/unit/core -q
uv run pytest tests/unit/vision -q
uv run pytest tests/unit/stages/test_keyframes.py -q
uv run pytest tests/unit/stages/test_scale.py -q
uv run pytest tests/unit/stages/test_detect.py -q
uv run pytest tests/unit/stages/test_inventory.py -q
uv run pytest tests/unit/stages/test_identify.py -q
uv run pytest tests/unit/stages/test_price.py -q
uv run pytest tests/unit/stages/test_room.py -q
uv run pytest tests/unit/stages/test_packet_stage.py -q
uv run pytest tests/unit/test_text.py tests/unit/test_eval.py tests/unit/test_live.py -q
uv run pytest tests/unit/test_simulate.py -q     # scripted session against the real server (a stand-in voice)
```

Check: all pass.

## 3. Ingest the captures

```bash
uv run claim ingest tests-local-data/still_photo_above.heic tests-local-data/still_photo_below.heic tests-local-data/still_photo_both.heic --id photos
uv run claim ingest tests-local-data/video_slow.mp4 --id slow
uv run claim ingest tests-local-data/video_rush.mp4 --id rush
uv run claim ingest tests-local-data/video_far_focus.mp4 --id far
ls runs/
ls runs/slow/frames | head
```

Check:
- `runs/` has four sweep folders.
- `slow` has about 22 frames (5.6 s × 4 fps), `rush` about 7, `far` about 14, and `photos` 3.

## 4. Run each stage on `slow`

Run the stages one at a time. After each, read the output and open the debug image.

**Model calls are off by default** (`gateway.cache_only: true` in `config/settings.yaml`): stages replay cached answers and never call a provider. In cache-only mode an answer any provider gave to the identical request is replayed, so the profile does not matter for replays. The first time a stage needs a new answer, add `--allow-calls`, for example `uv run claim --allow-calls stage detect --sweep slow`. Without an API key, use the `dev` profile (Claude Haiku through the local `claude` command) sparingly: `uv run claim --allow-calls --profile dev stage identify --sweep slow`.

Cached answers are keyed by the exact frame bytes. Re-ingesting a video gives the same bytes on the same machine, so reruns stay cached; a different video decoder may differ slightly and then needs new calls.

### 4.1 Keyframes
```bash
uv run claim stage keyframes --sweep slow
open runs/slow/debug/keyframes.jpg
```
Check: both shelves appear in at least one sharp keyframe, with no blurry frames kept.

### 4.2 Scale
```bash
uv run claim stage scale --sweep slow
jq '.data.frames[] | {frame, method, cm_per_px, confidence}' runs/slow/stages/2_scale.json
```
Check: `method` is `none` for every frame and `card_frames` is 0, because the captures have no card. No scale is invented, and the stage warns that sizes will come from the later scale rungs. With a printed card (`claim marker`) in view, card frames show `method: card` and `debug/scale_<frame>.jpg` outlines the card; frames that only overlap a card frame show `propagated_from` and a lower confidence.

### 4.3 Detect
```bash
uv run claim stage detect --sweep slow
jq '[.data[] | select(.kind=="spine")] | length' runs/slow/stages/3_detect.json
open runs/slow/debug/det_*.jpg
```
Check:
- Every book has a box, including the flat stack (English, Fundamental Physics).
- No box sits on the newspaper.
- `warnings` lists any frame that failed validation.

### 4.4 Inventory
```bash
uv run claim stage inventory --sweep slow
jq '.data.books[] | {book_id, unit, shelf, position, frame_ref}' runs/slow/stages/4_inventory.json
open runs/slow/evidence/shelf_*.jpg
```
Check:
- Each book appears once, even though it was seen in several frames: `slow` gives 10 books, the same as `still_photo_both` (`--sweep photos`).
- The numbering follows reading order: top shelf first, left to right; the flat stack on the top shelf is numbered top book first (A1-5, A1-6).
- `jq '.data.unconfirmed' runs/slow/stages/4_inventory.json` lists boxes seen in only one frame that were not counted. On `slow` these are two side-on slivers in the first keyframe, of books already counted.
- `unlinked_frames` is empty; a keyframe listed there could not be matched to the others and its books were left out of the count.

### 4.5 Identify
```bash
uv run claim stage identify --sweep slow
jq '.data.books[] | {book_id, status, title, author, match_score, id_confidence}' runs/slow/stages/5_identify.json
open runs/slow/debug/spine_A1-4.jpg      # what the reader was shown: the spine in each orientation
```
Check:
- Readable spines have titles (for example "Word Power Made Easy", Norman Lewis, `identified` at 100).
- Books no catalogue knows keep the spine text as their title, as `spine_only`, with a review note.
- The Hindi spine keeps its Devanagari text.
- Unreadable spines are `unidentified` with an empty title.
- Google Books may be reported unavailable (quota); Open Library is still used.

### 4.6 Measure
```bash
uv run claim stage measure --sweep slow
jq '.data.books[] | {book_id, height_cm, thickness_cm, scale_method, confidence}' runs/slow/stages/6_measure.json
```
Check:
- Every filled dimension has a `scale_method` (`card`, `catalogue_height` or `known_object`).
- On these captures every shelf is `none`: there is no card, no known-size object, and the catalogue records carry no heights. All dimensions are empty, never guessed, and the stage warns about it.

### 4.7 Price
```bash
uv run claim stage price --sweep slow
jq '.data.books[] | {book_id, status, replacement_cost, used_value, quotes: [.quotes[] | {shop, condition, price, currency, url, snapshot_ref}]}' runs/slow/stages/7_price.json
jq '.data.blocked_shops, .warnings' runs/slow/stages/7_price.json
ls runs/slow/evidence/prices | head
```
Check:
- Every price has quotes, each with a URL and a snapshot file.
- Shops that refuse requests are listed in `blocked_shops`.
- Open 3 snapshots: the extracted price matches the page.
- Books with no matching listing are `no_quotes` and stay out of the totals.

### 4.8 Room
```bash
uv run claim stage room --sweep slow
jq '.data' runs/slow/stages/8_room.json
```
Check: wall values are empty with review reasons, since the capture shows only the almirah. Shelved wall area is empty too (no scale), with a review reason.

### 4.9 Packet
```bash
uv run claim stage packet --sweep slow
open runs/slow/report.html
jq '.totals, (.review_queue | length)' runs/slow/claim_packet.json
```
Check:
- The validator reports no errors.
- Click 5 books in the report through to their frame and price page.
- Totals equal the sum of the lines shown.

### 4.10 Rerun a single stage
```bash
uv run claim run --sweep slow --from price
```
Check: only price and packet rerun, and the earlier stage files are unchanged.

## 5. Full pipeline on every sweep

```bash
for s in photos slow rush far; do uv run claim run --sweep $s; done
for s in photos slow rush far; do echo "$s: $(jq '.totals.book_count' runs/$s/claim_packet.json) books"; done
for s in photos slow rush far; do echo "$s: $(jq '.total_latency_s' runs/$s/metrics.json) s, valid $(jq '.valid' runs/$s/metrics.json)"; done
```

Check:
- `photos` and `slow` give the same book count.
- `rush` and `far` give the same count or a lower one, with more review entries. They never give more books.

## 6. Second country

```bash
uv run claim reprice --sweep slow --country US
jq '.books[] | {id, title, usd: .replacement_cost.amount, converted: .replacement_cost.converted}' runs/slow/claim_packet_US.json
uv run claim reprice --sweep slow --country IN      # back to India (the main packet follows the last reprice)
```

Check: the same books carry USD prices from US sources (AbeBooks only, so few books are priced and single listings are not cross-checked). Converted prices are labelled.

## 7. Evaluation against ground truth

Fill in `ground_truth/almirah/truth.yaml` **before tuning anything**. All of this can be done from the laptop:
- **Books:** read the photos. Record the true count, plus each book's unit, shelf, position, title as you read it, and whether it's legible.
- **Prices:** for up to 15 legible books, search the same shops the system uses (section 5.4 of the design). Record the new price, the URL and the date.
- **Items:** list every non-book item visible in the captures, with its category.
- **Measurements:** spine sizes and room dimensions need a tape measure. Leave them empty if you can't measure; the evaluation reports them as "not measured".

```bash
uv run claim eval --sweep photos --truth ground_truth/almirah/truth.yaml
uv run claim eval --sweep slow --truth ground_truth/almirah/truth.yaml
uv run claim eval --sweep rush --truth ground_truth/almirah/truth.yaml
cat eval/results_slow.md
```

Check:
- Count, title, price and item accuracy are reported for **every** sweep, including `rush` and `far`.
- Measures without truth data show "not measured".

After fixing a problem, rerun the evaluation and keep both result files. The before/after pair goes in the failure log.

## 8. Switching providers

Change a single stage's provider in `config/settings.yaml`, then rerun from that stage:

```bash
# example: read spines with a local model
uv run claim run --sweep slow --from identify
```

Also try the `anthropic`, `openai` or `groq` profile if you have a key. Check: the pipeline completes with no code change, and the `model_calls` field in each stage file shows the provider used.

Fallback check: set an invalid key for the first detect provider and rerun:

```bash
uv run claim run --sweep slow --from detect
jq '.warnings' runs/slow/stages/3_detect.json
```

Check: the warning shows the fallback to the next provider.

## 9. Integration tests

These run the full pipeline on the captures, replaying the cached model responses from earlier runs:

```bash
uv run pytest -o addopts="" tests/integration -q     # integration tests are left out of a plain `pytest` run
```

It copies the `slow` frames and runs every stage on them; it is skipped without `runs/slow/` and the cache.

## 10. Live agent (Mac camera)

Live sessions call models, so start the server with calls allowed:

```bash
uv run claim --allow-calls serve
```

Open http://127.0.0.1:8000/. In **Settings**, check the country (India) and that the Gemini key is present. In **Session**, pick the camera and the voice (Gemini Live). Point the camera at any books near you, or at the capture photos on another screen. Use headphones.

| # | Do | Expect |
|---|---|---|
| 1 | Press Start sweep | The agent greets you and explains the sweep (top shelf first, slowly, left to right) |
| 2 | Hold the camera on the books | Within about 20 s a provisional count appears under "Books in view" |
| 3 | Pan fast or shake the camera | A "moving too fast" or "blurry" warning on screen, and the agent tells you to slow down, within 3 s |
| 4 | Point at a bright lamp or window reflection | A glare warning |
| 5 | Say "skip the bottom shelf, those are not mine" | The agent confirms; a `record user fact` line appears, and `runs/<id>/facts.json` holds a `skip_shelf` fact |
| 6 | Talk while it is speaking | It stops and listens |
| 7 | Say "I'm done" | It calls end sweep, says processing has started, then reads back the summary; a report link appears |

Then check the live sweep's packet:

```bash
ls -t runs | head -1
jq '.data.valid, .data.errors, .data.facts_applied' runs/$(ls -t runs | head -1)/stages/9_packet.json
```

**Replay instead of the camera:** choose **Replay a video…** as the source and pick `tests-local-data/video_slow.mp4`; the recording plays into the session as if it were the camera.

**Cascaded voice:** choose Cascaded and repeat steps 1, 3, 5 and 7 in Chrome. Speech is recognised and spoken by the browser; replies take a few seconds.

**Phone:** `uv run claim --allow-calls serve --tunnel` prints a `https://….trycloudflare.com/?token=…` link. Open it on the phone; without the token the server answers 401.

**Chat mode:** choose *Chat (type, no voice)* under **Talk by** to run the whole checklist by typing (steps 1, 3, 5 and 7), with no microphone and no Gemini Live quota. It uses the profile's `converse` models.

**Terminal log:** the server prints what the session is doing (agent and user turns, capture warnings, tool calls, each pipeline stage with its time). `--log-level debug` adds more; a banner on the page says when model calls are off.

**History tab:** lists every session with how it was made, its result, its report and its conversation; sessions can be deleted there, or with `claim sessions` and `claim clean`.

**Session log:** every live sweep keeps `runs/<id>/live_log.jsonl`: what the user said, what the agent said, each warning, tool call and provisional count, and the notes the agent was sent, with times. Raw audio and frames are not in it (the frames are in `frames/`).

### 10.1 Scripted live session (no person needed)

`claim simulate` runs the same checklist without a microphone or camera. It starts the real server, connects over a WebSocket like the browser does, streams a reference camera feed at 4 fps in real time, and sends scripted user lines as recognised speech. The agent, the warnings, the tools, the provisional count and every pipeline stage run for real.

```bash
uv run claim --allow-calls --profile dev simulate                 # cascaded voice on Claude Haiku (local claude CLI)
uv run claim --allow-calls simulate --voice gemini                # Gemini Live: the user's lines go in as typed turns
uv run claim --allow-calls simulate --script my_script.yaml       # another feed or other lines
```

The feed and the lines are in `config/live_script.yaml`. The feed is built from the recordings in `tests-local-data/`: the far video, the slow video, each still photo seen through a portrait window that pans along the shelf, and the rushed video. Faults are added on top and marked `synthetic`: a glare patch, a lost-focus blur, and the rushed pan played twice as fast. The lines confirm the country, state a first edition, and say "I'm done".

Outputs, next to the sweep's usual files in `runs/<id>/`:

| File | What |
|---|---|
| `simulation/reference.mp4` | the camera feed as sent |
| `simulation/events.jsonl` | every message the server sent |
| `simulation/transcript.md` | the feed's segments, the timeline (who said what, warnings, tools, counts, status) and the result |
| `simulation/simulation.json` | warnings and tools with times, whether the agent itself ended the sweep, time to packet, totals |

Check, in `transcript.md`:

| # | Expect |
|---|---|
| 1 | The agent speaks first, names the country and currency and explains the sweep |
| 2 | Blur, glare and motion warnings appear during the matching segments |
| 3 | A provisional count appears during the sweep |
| 4 | "the Hindi book … is a first edition" produces a `record_user_fact` call; in the packet the fact is either applied to a book or listed in the review queue as "matches no shelf, book or item" |
| 5 | After "I'm done" the agent calls `end_sweep` itself (`agent_ended_sweep: true`) |
| 6 | The status ends `ready` with the summary, and `claim_packet.json` is valid |

Interruption (talking over the agent) cannot be scripted this way: the cascaded browser client stops speaking when it hears the user, and Gemini Live handles it in the model.

## 11. Upload a recorded video

In the web app, open **Upload** and choose `tests-local-data/video_slow.mp4`. Check: the packet matches the one from step 5 for `slow`.

## 12. Release checks

**No secrets in git**
```bash
git ls-files | xargs grep -n -E "(AIza|sk-|sk-ant-|gsk_)[A-Za-z0-9_-]{10,}" || echo "no keys found"
git check-ignore .env runs/ && echo ".env and runs/ are ignored"
```

**Fresh setup in under 15 minutes**
```bash
cd /tmp && rm -rf claim-check && git clone <repo-url> claim-check && cd claim-check
time (uv sync && cp .env.example .env && uv run pytest tests/unit -q)
```
Check: the time is under 15 minutes, and the README alone was enough.

**Export the demonstrated sweep**
```bash
uv run claim export --sweep <demo_sweep_id> --out release/<demo_sweep_id>
open release/<demo_sweep_id>/report.html
```
Check: every frame and price link in the exported report opens from inside `release/`.

**Demo recording**
- Use macOS screen recording (Cmd+Shift+5) with the microphone on.
- Record one take, 6 minutes or less, with no edits: start the session, sweep, show the inventory filling in, end the sweep, and wait for the packet.
- Then export that sweep with the command above.

**Simulated demo recording** (no person needed): drives the real web client in Chrome with the scripted session of §10.1.

```bash
uv run --with playwright playwright install ffmpeg      # once: Playwright's page recorder
uv run --with playwright --with imageio-ffmpeg python scripts/record_demo.py dev-sonnet   # or dev (Haiku), or any profile
```

Chrome's fake camera plays the reference feed; the script's lines go to the page's speech-recognition handler; what the page asks the browser to say, and the user's lines, are voiced with macOS `say` at the moments they happened. A banner across the page states what is simulated for the whole take. Options: `--chat` (typed conversation, no voice and no soundtrack), `--script config/live_script_clean.yaml` (one steady recording, no added faults), `--provisional N` (live count every N seconds, 0 = off), `--fresh-cache` (empty answer cache, so timings are real). Example, the Gemini chat demo: `record_demo.py gemini --chat --script config/live_script_clean.yaml --provisional 45`.

Output: `runs/<id>/demo/demo.mp4`, one continuous take of the page ending on the finished claim report. Needs Google Chrome and macOS. `claim export --sweep <id> --out release/<name>` copies the packet, report, frames, price snapshots and the demo video.
