# Library Contents Claim Agent: Design Doc

## 1. Problem

A policyholder documenting a home library for a contents claim has to list every book and item, find what each is worth, and measure the room. Done by hand this takes hours, and the adjuster then has to trust the numbers.

This product turns that into one continuous walk through the room with a camera. The policyholder talks to a live agent while the camera is on. The agent directs the capture, and the system produces a claim packet:
- every book counted, located, identified from its spine, measured and priced in local currency
- every non-book item identified and priced
- the room's dimensions, floor area and wall area

Every number is traceable to a saved frame or a price URL, and carries a confidence value. Totals are computed in code. Anything the system is not sure about goes to a review queue instead of being guessed.

The interaction pattern builds on the open-source [Insurance Claim Live Agent Team](../awesome-llm-apps/voice_ai_agents/insurance_claim_live_agent_team) example. That app describes what it sees. This product measures, counts and prices it.

## 2. Goals and non-goals

**Goals**
1. Produce counts, dimensions, prices and areas that hold up against hand measurement.
2. Never output a fabricated value. Every estimate states its method and confidence.
3. Make every book and item locatable, so a person can verify it in the frame where it was seen.
4. Keep every processing stage separate and testable: each has its own inputs and output, and can run on its own.
5. Work with any model provider (Gemini, OpenAI, Anthropic, Groq or local open models), chosen per stage through configuration.
6. Work in rooms the system has never seen, with no hardcoded room sizes, catalogues or prices, and price in whichever country is set in settings (v1 is checked with India and the US).

**Out of scope for v1**
User accounts, cloud deployment, a policy database, and training models from scratch.

## 3. Operating constraints

| What varies | How the design handles it |
|---|---|
| **Where it runs:** the server runs on the user's computer | Open it in a desktop browser on `localhost` with the computer's camera, or on a phone browser through a `cloudflared` quick tunnel (HTTPS, no account, access token in the link). Recorded videos can also be processed offline. |
| **Model access:** API keys for Gemini, OpenAI, Anthropic or Groq, or none | Each model stage takes its own provider and key. Without a key, that stage uses a local open model, downloaded on first use (section 5.3). |
| **Machine running local models:** limited memory (for example 16 GB) | Local models load one at a time; post-sweep stages run in sequence. |
| **Country and currency** | Set in the app's settings. |

## 4. Requirements

### 4.1 Functional requirements

**FR-1 Live sweep agent**
- FR-1.1 Real-time two-way voice with the camera on, in a desktop browser.
- FR-1.2 The user picks the camera from the devices the browser exposes (laptop webcam, USB camera, or a phone's rear camera when opened through the tunnel). A recorded video can be submitted instead of a live sweep.
- FR-1.12 **Replay:** a recorded video or a set of images can be played through the live session as if it were the camera, and through the pipeline, saving every output for comparison between runs.
- FR-1.3 The agent greets the user, confirms the country and currency from settings, and explains the sweep in one or two sentences.
- FR-1.4 The agent actively directs the capture. While the user is still at that spot, it says when something is wrong:
  - the image is blurry, or the user is moving too fast
  - glare is hiding spines
  - something is blocking the view
  - spines are too small to read, so step closer
  - a shelf, wall or floor area was missed
- FR-1.5 The agent can ask for a re-capture of a specific shelf or corner within the same pass.
- FR-1.6 The agent never asks the user to pull books out, scan barcodes, type ISBNs or photograph items one at a time.
- FR-1.7 The agent says what it is logging, and asks short questions it cannot answer from video, for example "is that portrait an original or a print?".
- FR-1.8 The user can interrupt the agent mid-sentence.
- FR-1.9 The user can correct the agent by voice, for example "that is a first edition" or "skip that shelf, those are not mine". Corrections are stored as structured facts and applied to the inventory.
- FR-1.10 The inventory fills in on screen during the sweep as books and items are recognised (books per shelf, items with their category), marked provisional until confirmed.
- FR-1.11 When the sweep ends, the agent reads back a summary and the claim packet becomes available.

**FR-2 Books: count, locate, measure, identify**
- FR-2.1 Detect every book spine in the frames, including books lying flat or stacked, with a bounding box.
- FR-2.2 De-duplicate books seen in several frames, so each physical book is counted once.
- FR-2.3 Give each book a position a person can find: shelving unit, shelf row, and order from the left (for example "Unit A, shelf 2, book 5").
- FR-2.4 Produce an annotated image per shelf with numbered boxes, linked from each book's line, so a person can verify the count and each identification.
- FR-2.5 Measure each spine's height and thickness in centimetres, and report total shelf run in metres. Every measurement carries a confidence value and the scale method used.
- FR-2.6 Read the spine (title, author and publisher where visible, including non-Latin scripts such as Devanagari) and resolve it to a specific work, and to an edition only when the evidence supports it.
- FR-2.7 Log an unreadable spine as an unidentified book with its position and dimensions. Never guess a title.

**FR-3 Book valuation**
- FR-3.1 Price each identified book in the country and currency set in settings.
- FR-3.2 Give two figures per book: replacement cost (new or equivalent today) and used market value.
- FR-3.3 Prices come from live web pages. Every price carries its source, URL, retrieval date, the condition assumed, and a saved snapshot of the page.
- FR-3.4 When no local price exists, convert from another market and label the price as converted.
- FR-3.5 When no price exists at all, say so and exclude the book from totals.
- FR-3.6 Rare, signed or antiquarian books, user-declared first editions, and books above a value threshold set per country go to human appraisal and are not priced automatically.
- FR-3.7 Changing the country in settings reprices an existing inventory without re-capturing.

**FR-4 Non-book contents**
- FR-4.1 Detect and classify everything that is not a book: shelving, furniture, appliances, lamps, portraits and framed art, rugs, electronics and decor.
- FR-4.2 Record category, material, approximate dimensions with confidence, brand or model when legible, and the frame where the item was seen.
- FR-4.3 Price each item at local replacement cost with a web source, or give a sourced price range when brand and model are unknown.
- FR-4.4 Art and portraits default to "needs appraisal" unless the user states the piece is a print. The agent asks.

**FR-5 Room surface area**
- FR-5.1 Report room length, width and ceiling height, each with confidence and method.
- FR-5.2 Report floor area and total wall area in m² and ft², plus the wall area covered by shelving.
- FR-5.3 State how the shape was handled when the room is not a rectangle.
- FR-5.4 All room measurements come from the same sweep, with no separate measuring pass.

**FR-6 Claim packet**
- FR-6.1 Each sweep produces one `claim_packet.json` (schema in Appendix A) and one human-readable HTML report.
- FR-6.2 Every line carries a confidence value and a `frame_ref` to a saved frame or video timestamp.
- FR-6.3 An empty field means the system does not know. A filled field always has evidence behind it.
- FR-6.4 Totals are computed in code from the lines, never written by a language model.
- FR-6.5 A review queue lists every line the system is not confident in, with the reason.
- FR-6.6 An adjuster can trace any figure back to the frame it was seen in and the price source it came from.

**FR-7 Settings**
- FR-7.1 Country and currency.
- FR-7.2 Model provider and API key for each model stage (section 5.3), or a local model.
- FR-7.3 Camera source.
- FR-7.4 Appraisal value threshold.

**FR-8 Accuracy evaluation**
- FR-8.1 Ground truth for a test space (count, titles, spine sizes, prices, item list, room measurements) is stored as a file.
- FR-8.2 An evaluation tool compares a claim packet with ground truth and reports accuracy for every measure.
- FR-8.3 The processing pipeline can run on recorded video with local models only.

### 4.2 Non-functional requirements

| Requirement | Target |
|---|---|
| **Sweep duration:** one room in a single continuous pass | under 3 min |
| **Time to packet:** sweep end → summary and packet ready | under 5 min with hosted models |
| **Live responsiveness:** capture problem → agent mentions it | under 3 s |
| **Integrity:** no model-written prices or totals; every number traceable to a frame or price page | Enforced by schema and a packet validator |
| **Stage isolation:** every stage reads files, writes one output file, and runs on its own from the command line | Each stage has unit tests with saved inputs and mocked models |
| **Provider portability:** switching provider needs config only, no code change | Gemini, OpenAI, Anthropic, Groq, Ollama |
| **Resilience:** a model or website failure never loses the sweep | Frames saved first; any stage can rerun |
| **Setup:** a new developer runs it from the README with API keys | under 15 min; `.env.example`; no secrets in git; local models optional |
| **Cost visibility:** cost and latency per stage for every sweep | Reported with each packet |

## 5. Architecture

Diagrams are SVG files in [diagrams/](diagrams/). Open one directly to zoom. The `.mmd` files next to them are the editable sources.

### 5.1 System overview

![System overview](diagrams/system.svg)

- **Recorder + quality checks:** saves every frame and runs blur, glare, motion and coverage checks.
- **Live agent:** talks with the user and turns quality warnings into spoken directions.
- **Processing pipeline:** builds the claim packet after the sweep (section 5.2).
- **Model gateway:** routes every model call to the provider set in settings (section 5.3).

### 5.2 Processing: what runs when

**During the sweep** (keeps the agent and screen live):
1. **Record:** save every frame at full resolution with a timestamp.
2. **Check quality:** measure blur, glare, motion and coverage on each frame, and pass warnings to the live agent.
3. **Provisional inventory:** every few seconds, run the detect stage on the sharpest recent frame and push provisional entries to the screen (books per shelf, items with their category).

**After the sweep ends** (builds the packet):

![Processing pipeline](diagrams/pipeline.svg)

**Stage contract.** Every stage follows the same rules, so each can be built, tested and rerun on its own:
- It reads only saved files: frames, and earlier stages' outputs.
- It writes one output file (named in the diagram), which includes its own latency, model calls and cost.
- It runs from the command line on its own, for example rerunning only pricing after a fix.
- It has unit tests that use saved inputs and mocked model responses.

| Stage | What it does |
|---|---|
| 1 Keyframes | Keeps the sharpest frames that together cover every shelf and wall, so later stages process tens of frames, not thousands. |
| 2 Scale | Finds metric scale for each shelf unit and wall using the scale ladder (section 6.1). |
| 3 Detect | Draws a box around every spine (upright, flat, stacked), every non-book item, and wall corners, doors and windows. |
| 4 Inventory | Maps boxes from all frames onto each shelf's plane, merges the same book seen twice, and numbers books by unit, shelf row and position from the left. Produces the annotated shelf images. |
| 5 Identify | Crops each spine, transcribes the visible text exactly, and matches it against book catalogues. Code computes the identification confidence. Keeps each matched book's catalogue height. |
| 6 Measure | Converts each box to centimetres using the best scale source for its shelf (section 6.1), including the catalogue heights of identified books on the same shelf. |
| 7 Price | Books: searches a short list of shops for the selected country (section 5.4). Items: web search for product pages. In both cases it fetches the pages, extracts prices from page data, and saves snapshots. |
| 8 Room | Measures walls and ceiling height from the detected wall regions and the scale planes, then computes floor and wall areas. |
| 9 Packet | Applies the user's voice corrections, flags low-confidence lines, computes totals, validates the packet and writes the report. |

### 5.3 Models: stages and providers

Four stages use a model. Each one has its own provider and API key in settings, so providers can be mixed. A stage with no key uses its local open model, which is downloaded only when first selected.

| Model stage | What the model does | Default (hosted, with API key) | Local option (optional download) |
|---|---|---|---|
| **Live conversation** | Holds the voice conversation, sees low-resolution frames, directs the capture, records corrections | Gemini Live, realtime | Qwen VL via Ollama, cascaded |
| **Detect** (stage 3) | Draws boxes around spines and items | Gemini Flash (native bounding boxes) | OWLv2 |
| **Read spines** (stage 5) | Transcribes spine crops exactly | Gemini Flash | Qwen VL via Ollama, with Surya OCR as a cross-check (handles Devanagari) |
| **Classify items** (stage 3) | Category, material, brand or model | Gemini Flash | Qwen VL via Ollama |

Any hosted provider works in any stage: Gemini, OpenAI, Anthropic or Groq. Scale rung 3 (section 6.1) uses Depth Anything V2 Metric. It is local only and optional; without it, that rung is skipped.

**Detection fallback.** Boxes from some providers are less precise than others. Code checks every detect result: box shapes must be plausible for spines, boxes must stay inside the shelf area, and they must cover the shelf's run. If a result fails these checks, the stage retries with the next detector in its list (another provider, or OWLv2 if installed). Frames that still fail go to the review queue.

The live conversation stage uses one of two **voice modes**:
- **Realtime:** the provider streams audio in and out in one session (Gemini Live, OpenAI Realtime). Lowest latency.
- **Cascaded:** speech-to-text (browser speech recognition or local Whisper) → any chat model with the latest frame → text-to-speech (browser speech synthesis or local Piper). This works with every provider, including Anthropic and local models.

Quality warnings from the recorder don't need a model. They are short fixed phrases, spoken immediately in both modes.

**Can the live model do background work?** Realtime models are built for streaming conversation, not batch jobs. Background stages use the same provider's standard model with the same API key: one Gemini key covers Gemini Live and Gemini Flash.

**Profiles** are presets that fill every stage at once. Each stage can still be changed on its own.

| Profile | Live conversation | Detect, read, classify |
|---|---|---|
| `gemini` (default) | Gemini Live, realtime | Gemini Flash |
| `openai` | OpenAI Realtime | GPT vision model |
| `anthropic` | Claude, cascaded | Claude |
| `groq` | Groq vision model, cascaded (Whisper on Groq for speech-to-text) | Groq vision model |
| `local` | Qwen VL via Ollama, cascaded | OWLv2 + Qwen VL + Surya (one model loaded at a time) |
| `dev` (development only) | Cascaded only, several seconds per turn: for trying the flow | Claude Haiku through the local `claude -p` command, on the developer's own login |

**Cache-only mode.** Every answer is cached. With cache-only mode on (the development default), no model is ever called: stages replay cached answers, and a missing answer fails clearly. This keeps development and tests free, and doubles as a replay mode for demos.

**Fallback chains.** Each stage has an ordered provider list. For live conversation, the default list is: the configured provider → Groq in cascaded mode → spoken quality warnings only. The last step needs no model, so a sweep continues even when every provider hits its rate limit. Post-sweep stages can also be rerun later from saved frames.

All hosted and Ollama model calls go through **LiteLLM**, which exposes Gemini, OpenAI, Anthropic, Groq and Ollama behind one API. OWLv2, Surya and Depth Anything run in-process. The `claude-cli` provider is a small adapter around the command line.

### 5.4 Price sources

Every source is free and needs no API key. Shops are listed per country in config. Adding a country means adding its list, and adding a shop that uses one of the existing reading strategies is a config change only.

| | India (INR) | US (USD) |
|---|---|---|
| **Books: new price** | Oxford Bookstore, Flipkart | AbeBooks |
| **Books: used price** | AbeBooks (USD, converted) | AbeBooks |
| **Non-book items** | Flipkart search, using local words for categories (shelving → "almirah") | – |

- Pages are fetched with plain HTTP. Prices are read only from structured page data: schema.org JSON-LD `Offer` records (search pages with full records, or product pages linked from a search page) and Shopify product JSON.
- Shops that refuse automated requests are listed per country with the reason, skipped, and reported with each run. Checked when the shop lists were set up: Amazon.in and Amazon answer 503, ThriftBooks 406, Bookswagon refuses connections, Sapna Online renders prices only with JavaScript, Barnes & Noble's search is not reachable without a browser, and DuckDuckGo answers with a bot check. A headless-browser fetcher can be added behind the same fetch function for such shops.
- A result counts only when its title and author match the identified book, and any class or part number on the spine appears in the listing. Without an author on one side, only an almost exact, specific title counts, and the book goes to review.
- Prices in the country's currency are preferred; foreign prices are used only when none exist, and are marked as converted.
- Used value comes only from used listings and is capped at the replacement cost. A book with no used listing, or no new price to cap against, is left out of the used total.
- The appraisal threshold is set per country in its config file (for example ₹5,000 in India, $75 in the US).
- Each book stops after the first two shops that return a match; used-price sources are still searched. Results are cached so reruns don't fetch again.
- Currency conversion uses Frankfurter (ECB rates).

## 6. Key design decisions

| Decision | Why |
|---|---|
| **The live agent directs the capture; it never writes inventory lines or prices** | Realtime models are fast but prone to inventing details. Inventory comes from pipeline stages that check their evidence. |
| **Every book is a box in a saved frame, with a position on a shelf** | Counts become verifiable: a person can open the annotated shelf image and check each numbered book. |
| **Models read; code decides** | Models transcribe text and draw boxes. Code de-duplicates, matches catalogues, scores confidence, extracts prices and computes totals. Wrong answers become measurable, and guessed titles and prices stay out. |
| **Prices only from fetched web pages, with a snapshot** | Model memory cannot be checked. A saved page gives a source, a date and evidence. |
| **Separate stages with file outputs** | Each stage can be tested, measured and rerun on its own. A failure in one stage never forces a new sweep. |
| **Provider per stage, local as fallback** | Anyone can run it with whichever API keys they have. Cascaded voice covers providers without realtime audio, and local models cover missing keys and offline testing. |

### 6.1 Scale ladder

For each measured plane (a shelf unit or a wall), the system uses the highest rung available, cross-checks when two are available, and records which one it used:

| Rung | Source | Available when | Confidence |
|---|---|---|---|
| 1 | **Reference card:** a printed marker or A4 sheet in the same plane | The user places one | High for that plane |
| 2 | **Catalogue book heights:** the printed height of confidently identified books on the same shelf | Books on the shelf are identified and the catalogue lists their size | Medium to high, from the spread across books |
| 3 | **Known object sizes:** door height, A-series paper, sockets | A recognised object is in view | Medium |
| 4 | **Monocular depth model** with camera focal length (from photo metadata, or a camera field-of-view setting) | Always | Low; always sent to review |

## 7. Technology choices

| Area | Choice | Why |
|---|---|---|
| Server | Python 3.12, FastAPI, `uv` | Strong computer vision and model ecosystem; async WebSockets for live audio; fast, reproducible installs |
| Frontend | Plain JavaScript modules served by FastAPI | One page (session, upload, settings) with no build step, so setup stays short and one origin serves both the API and the client through a tunnel |
| Model gateway | LiteLLM | One API for Gemini, OpenAI, Anthropic, Groq and Ollama, so the provider is a setting |
| Local models (optional) | Ollama (Qwen VL), OWLv2, Surya OCR, Depth Anything V2 | Used when a stage has no API key, and for offline, repeatable testing on recorded captures |
| Vision and geometry | OpenCV | Blur and glare checks, marker detection, homography for de-duplication and measurement |
| Web pricing | httpx + schema.org JSON-LD / Shopify JSON | Free, no API keys. Reads structured price data from shop pages and saves snapshots; shops that need a browser are listed as blocked |
| Book catalogues | Open Library, Google Books | Free, broad coverage |
| Schemas | Pydantic v2 | Enforces the packet contract; model outputs have no price fields |

## 8. Reference app: kept, changed, dropped

| | What |
|---|---|
| **Kept** | The interaction pattern (talk, camera, a claim file that builds live); the WebSocket bridge to a realtime voice model; background tool calls that don't block the conversation |
| **Changed** | The agent directs the capture instead of describing damage; the notebook becomes an itemised, located and priced inventory; background agents become separate detection, identification, pricing and measurement stages; one provider becomes any provider |
| **Dropped** | The ADK graph, policy lookup, incident sketches, the avatar |

## 9. Assumptions
1. Books on shelves are mostly spine-out. Books shelved cover-out or in closed cabinets are not counted.
2. Rooms are treated as rectangles unless the sweep shows more than four walls. Wall area is gross; doors and windows are reported, not subtracted.
3. Used value is reported only when a real used listing is found, and never above the replacement cost. No depreciation formula.
4. An item's depth is left empty, because it cannot be seen from the front.

## 10. Future scope (next two weeks)
- **Review screen:** confirm or fix a book's box, title or price in the annotated shelf image, then reprice and regenerate the packet.
- **Condition grading** per book (spine wear, sun fade, water damage), feeding the used value.
- **Checker agent:** a second, independent model that audits the packet and reports disagreements.
- **Floor plan sketch** with shelving and items placed, from the room measurements.
- **WebXR AR measurement** on Android Chrome (phone capture through the tunnel) as a new top rung of the scale ladder.

## Appendix A. Claim packet schema

Fields may be added. None of these may be removed. Fields this design adds:
- `books[].unit`, `books[].bbox` and `books[].annotated_ref`, for position and verification
- `books[].dimension_confidence` and `books[].scale_method`
- `items[].bbox`
- `price_snapshot` paths on every price

```json
{
  "sweep": { "id": "", "captured_at": "", "device": "", "duration_s": 0, "country": "", "currency": "" },
  "room": {
    "length_m": 0, "width_m": 0, "height_m": 0,
    "floor_area_m2": 0, "wall_area_m2": 0, "shelved_wall_area_m2": 0,
    "scale_method": "", "confidence": 0
  },
  "books": [
    {
      "id": "", "shelf": "", "position": 0, "frame_ref": "",
      "status": "identified | unidentified | needs_appraisal",
      "title": "", "author": "", "edition": "", "isbn": "",
      "spine_height_cm": 0, "spine_thickness_cm": 0,
      "id_confidence": 0,
      "replacement_cost": { "amount": 0, "source": "", "url": "", "retrieved_at": "", "converted": false },
      "used_value": { "amount": 0, "source": "", "url": "", "retrieved_at": "", "condition_assumed": "" }
    }
  ],
  "items": [
    {
      "id": "", "category": "", "description": "", "brand_model": "", "frame_ref": "",
      "dimensions_cm": { "w": 0, "h": 0, "d": 0 },
      "status": "priced | range | needs_appraisal",
      "replacement_cost": { "low": 0, "high": 0, "source": "", "url": "", "retrieved_at": "" },
      "confidence": 0
    }
  ],
  "totals": {
    "book_count": 0, "books_identified": 0, "books_unidentified": 0, "shelf_run_m": 0,
    "books_replacement_cost": 0, "books_used_value": 0,
    "items_replacement_cost_low": 0, "items_replacement_cost_high": 0,
    "excluded_from_totals": 0
  },
  "review_queue": [ { "ref_id": "", "reason": "" } ]
}
```
