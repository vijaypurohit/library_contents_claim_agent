# Requirements coverage

Where each requirement in [DESIGN §4](DESIGN.md#4-requirements) is met, how it is checked, and what the test captures could and could not show. Status:

- **Done**: built, tested, and shown on the test data.
- **Built**: built and unit-tested, but the test data cannot show it (no scale reference, no room footage).
- **Partly**: part of it is built; the rest is noted.

Measured results: [`eval/`](../eval/) and the [failure log](FAILURE_LOG.md). The live session was checked end to end with the scripted session of [Testing §10.1](TESTING.md#101-scripted-live-session-no-person-needed); its results are in [Live session results](#live-session-results) below.

## FR-1 Live sweep agent

| Requirement | Status | Where | Checked by |
|---|---|---|---|
| 1.1 Two-way voice, camera on, in a browser | Done | `api/app.py`, `web/`, `live/gemini_realtime.py`, `live/cascaded.py` | `tests/unit/test_live.py`; scripted session (cascaded); the Gemini Live bridge against the SDK's own message types |
| 1.2 Camera choice; a recorded video instead | Done | `web/app.js` (source list, Replay a video), Upload tab, `claim ingest` | TESTING §10–11 |
| 1.3 Greets, confirms country and currency, explains the sweep | Done | opening note (`live/session.py: opening_note`), `prompts/live_system.md` | `test_live.py`; scripted session: the agent's first line names India and rupees |
| 1.4 Directs the capture: blur, too fast, glare | Done | `live/session.py: QualityMonitor` (thresholds calibrated on the `slow` and `rush` sweeps) | `test_live.py`, `test_simulate.py`; scripted session: the first fault produced all three warnings on its first frame; later faults within 6 s were debounced |
| 1.4 Blocked view, spines too small, missed shelf or wall | Partly | the voice model is told to watch for these in the frames it sees (1 per second); there is no code check | not measured: the test footage has no occlusion or missed area |
| 1.5 Re-capture of a shelf within the same pass | Done | tool `request_recapture`; keyframes warn about views that are only blurry | `test_live.py`, `test_keyframes.py` |
| 1.6 Never asks for barcodes, ISBNs or books pulled out | Done | the prompt never asks; nothing in the pipeline needs it | review of `prompts/live_system.md` |
| 1.7 Says what it logs; asks what video cannot answer (original or print) | Partly | prompt: log observations aloud, ask about framed art once | not shown: the test space has no art |
| 1.8 Interrupt mid-sentence | Done | Gemini Live: activity interrupts the model; cascaded: the page stops speaking when it hears the user | not scriptable; TESTING §10 step 6 by hand |
| 1.9 Corrections by voice, stored as facts and applied | Done | tool `record_user_fact` → `facts.json` → price and packet stages; a fact that matches no line goes to review | `test_packet_stage.py`, `test_price.py`; scripted session |
| 1.10 Inventory fills in during the sweep | Partly | provisional spine count and item categories of the latest frame every 20 s | scripted session; not per shelf |
| 1.11 Summary read back; packet available | Done | `live/session.py: summary_for_voice` (from the packet's totals only) | `test_live.py`; scripted session |
| 1.12 Replay a recording through the session and the pipeline | Done | Replay a video (web), `claim replay`, `claim simulate` | TESTING §10, §10.1 |

## FR-2 Books

| Requirement | Status | Where | Checked by |
|---|---|---|---|
| 2.1 Every spine, upright, flat or stacked | Done with Gemini Flash; not with Haiku | `stages/detect.py`, `prompts/detect.md` | count 10/10 on all four recorded sweeps (Gemini Flash); 47 and 40 for 10 in the live runs on Claude Haiku |
| 2.2 Each book counted once across frames | Done | `stages/inventory.py` (homographies onto one reference frame) | count 10/10; `test_inventory.py` |
| 2.3 Unit, shelf, position | Done | `stages/inventory.py` | `eval/results_*.md` per position |
| 2.4 Annotated shelf image per shelf | Done | `evidence/shelf_*.jpg`, linked from each line | report |
| 2.5 Height, thickness, shelf run, with confidence and method | Built | `stages/measure.py`, `scale/` | synthetic tests; the captures have no scale, so values are empty with review reasons |
| 2.6 Read the spine, resolve to a work, edition only on evidence | Done | `stages/identify.py`, `text/match.py`, Open Library | titles 78–89% on steady sweeps, 0% confidently wrong |
| 2.7 Unreadable spine → unidentified, no guessed title | Done | identify statuses; identical readings distrusted | `rush`: 4 blank instead of guessed |

## FR-3 Book valuation

| Requirement | Status | Where | Checked by |
|---|---|---|---|
| 3.1 Local country and currency | Done | `config/locales/*.yaml`, `stages/price.py` | `slow` packet in INR |
| 3.2 Replacement cost and used value | Done | `price.py: book_values` (used only from a used listing, capped) | `test_price.py` |
| 3.3 Source, URL, date, condition, snapshot | Done | quotes in the packet; `evidence/prices/` | packet validator |
| 3.4 Converted when no local price | Done | `pricing/fx.py`, `converted: true` | `test_price.py`; US reprice |
| 3.5 No price → say so, exclude from totals | Done | `no_quotes` status, `excluded_from_totals` | 4 of 4 unlisted books left unpriced |
| 3.6 Rare, signed, first edition, above threshold → appraisal | Done | `APPRAISAL_FACTS`, per-country threshold | `test_price.py`, `test_packet_stage.py` |
| 3.7 Another country without re-capture | Done | `claim reprice --country US` | `runs/photos/claim_packet_US.json` |

## FR-4 Non-book contents

| Requirement | Status | Where | Checked by |
|---|---|---|---|
| 4.1 Detect and classify non-book items | Done (small sample) | `stages/detect.py`, `stages/inventory.py` | 1 of 1 item (the almirah) on every recorded sweep |
| 4.2 Category, material, size, brand, frame | Built | `stages/inventory.py`, `stages/measure.py` | sizes empty without scale |
| 4.3 Local replacement cost or sourced range | Done | `price.py: price_item` (Flipkart, local search words) | `test_price.py` |
| 4.4 Art needs appraisal unless the user says print | Done | `price.py: print_items` | `test_price.py`; no art in the test space |

## FR-5 Room

| Requirement | Status | Where | Checked by |
|---|---|---|---|
| 5.1–5.4 Room size, floor, walls, shelved wall area, shape | Built | `stages/room.py` | `test_room.py` on synthetic walls; the captures have no room footage, so the packet's room is empty with review reasons |

## FR-6 Claim packet

| Requirement | Status | Where | Checked by |
|---|---|---|---|
| 6.1 `claim_packet.json` and HTML report per sweep | Done | `stages/packet.py`, `report/` | every sweep |
| 6.2 Confidence and `frame_ref` per line | Done | `domain/packet.py` | validator: every frame and snapshot exists |
| 6.3 Empty when unknown | Done | schema defaults `null`; review entries | `test_packet.py` |
| 6.4 Totals in code | Done | `ClaimPacket.build` | validator rejects edited totals |
| 6.5 Review queue with reasons | Done | gathered from every stage, plus unmatched user facts | every packet |
| 6.6 Trace a figure to its frame and price page | Done | `claim export` copies every referenced file | TESTING §12 |

## FR-7 Settings and FR-8 Evaluation

| Requirement | Status | Where |
|---|---|---|
| 7.1–7.4 Country, providers per stage, camera, appraisal threshold | Done | `config/settings.yaml`, `config/locales/`, web Settings tab |
| 8.1 Ground truth as a file | Done | `ground_truth/almirah/truth.yaml` (count, titles, prices; no spine sizes or room: not measurable here) |
| 8.2 Evaluation tool | Done | `claim eval` → `eval/results_*.md` |
| 8.3 Pipeline on local models only | Built | `local` profile (Ollama); not run on this machine (no Ollama installed) |

## Non-functional

| Requirement | Status | Evidence |
|---|---|---|
| Sweep under 3 minutes | Done | the scripted sweep is 46 s of camera; the agent does not slow it down |
| Time to packet under 5 minutes with hosted models | Not shown | recorded sweeps: 1–5 minutes, dominated by free-tier rate limits; the scripted session ran every model through the local `claude` CLI and took about 12 minutes (see below) |
| Capture problem → warning under 3 s | Done | warnings are computed on each frame as it arrives; in the scripted session the first fault was reported on its first frame |
| No model-written prices or totals; everything traceable | Done | schema, packet validator, `claim export` |
| Stage isolation | Done | each stage runs alone (`claim stage`) and has unit tests |
| Provider portability | Done | profiles in `config/settings.yaml`; detection needs a model that can locate objects (see the failure log) |
| A failure never loses the sweep | Done | frames are written before any model call; every stage can rerun; a session that ends early keeps its frames and log |
| Setup under 15 minutes | Done | README, `.env.example`, TESTING §12 |
| Cost and latency per stage | Done | `metrics.json` per sweep; failure log |

## Live session results

Scripted session on Claude Haiku (`claim simulate`, cascaded voice), 46 s of reference feed with glare, blur and a fast pan added; and the same script through the web page in Chrome. Detail and causes: [failure log, errors 5 and 6](FAILURE_LOG.md).

| What was checked | Result |
|---|---|
| Agent speaks first, names the country and currency, explains the sweep | Yes, in both runs |
| Capture warnings | Blur, glare and motion warnings reached the browser during the matching footage, on the first frame of the fault; later faults within 6 s were debounced |
| Provisional count during the sweep | Yes (12 spines in view) |
| Spoken correction recorded as a fact | Yes; the target "bottom shelf" matches no book, so it went to the review queue |
| Agent ends the sweep when the user says they are done | 1 of 2 runs; otherwise the End button / fallback |
| Packet produced, valid, summary read back | Yes, both runs |
| Time to packet | About 12 minutes on Haiku through the local CLI: above the 5-minute target |
| Book count | **Missed: 47 and 40 for 10** |
| Titles | **Missed**: 9 and 3 lines with a title; none confidently wrong |

Not tested: Gemini Live audio, interruption by voice, a person's own camera and microphone. The pass/fail checklist for those is [Testing §10](TESTING.md#10-live-agent-mac-camera).
