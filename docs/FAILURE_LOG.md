# Failure log

Measured on the four sweeps of the almirah test space (`photos`, `slow`, `rush`, `far`: about 10 books, 2 shelves) against `ground_truth/almirah/truth.yaml`. Full results per sweep and per book: [`eval/results_*.md`](../eval/).

| Measure | photos | slow | rush | far | Target |
|---|---|---|---|---|---|
| Book count | 10/10 | 10/10 | 10/10 | 10/10 | within 5% |
| Titles correct (legible spines) | 89% | 78% | 33% | 89% | at least 70% |
| Titles confidently wrong | 0% | 0% | 0% | 0% | at most 3% |
| Book prices within 25% of the hand check | 1 of 6 | 2 of 6 | 0 of 6 | 2 of 6 | all |
| Books with no listing anywhere left unpriced | 4 of 4 | 4 of 4 | 4 of 4 | 4 of 4 | all |
| Items found with the right category | 1 of 1 | 1 of 1 | 1 of 1 | 1 of 1 | at least 80% |
| Spine dimensions, floor and wall area | not measured (no scale in the captures, no hand measurements) | | | | |

## The three worst errors

**1. Book prices disagree with the hand check (1–2 of 6 within 25%).**
*What happens:* R.S. Aggarwal Class 10 is ₹260 in the packet (median of Flipkart listings at ₹255 and ₹266) against ₹621 in the hand check (another Flipkart seller, 2025 edition). Pradeep's Physics XII is ₹1,865 (₹1,112 and ₹2,618) against ₹834.
*Root cause:* Indian school books are sold by many sellers at very different discounts off the same printed price, and editions change yearly. A median of whichever listings the search returns and a single hand-picked listing measure different things. The printed price (MRP) is far more stable (₹2,240 for Pradeep's two volumes in the hand check), but the system reads only the sale price from the structured data.
*What to change:* read the MRP where the shop shows it and price from it, keep the sale median as a second figure, and prefer listings whose edition year or publisher matches the spine.

**2. The rushed sweep reads only a third of the titles (`rush`: 33%, 4 blank, 3 wrong).**
*What happens:* the reader returned no text for 4 spines and invented "SANJIV SINHA / INORGANIC CHEMISTRY" for three different books.
*Root cause:* the capture. `rush` pans at a frame-to-frame change of about 0.12 against 0.08 for `slow`, and its sharpness median is half of `slow`'s (185 against 371). A blurred spine gives a weak reader nothing to read, and it then copies text between images.
*What changed:* identical text for two books is now distrusted (those 3 became unidentified instead of wrongly titled, so no wrong title reaches the packet). The live agent's pan-speed warning is calibrated on exactly this pair: it fires above 0.11 at 4 fps, which `rush` exceeds and `slow` and `far` never reach, so the user is told to slow down and re-film during the same pass.

**3. No dimensions and no room measurements.**
*What happens:* every book's height and thickness, the shelf run, the almirah's size and the room are empty.
*Root cause:* no rung of the scale ladder applies. There is no printed card in the captures, the catalogue records found carry no printed heights (Open Library has no dimensions for these Indian editions, and Google Books' anonymous quota was used up and does not accept the project key), detect found no known-size object, and the depth rung is not installed.
*What to change:* have the live agent ask for an A4 sheet or the printed card on each shelf when no scale has been seen (both rungs exist and are tested), add the depth model as the last rung, and use a Google Books key for catalogue heights.

## The live path, run end to end on Claude Haiku (scripted session)

A scripted session (`claim simulate`, [Testing §10.1](TESTING.md#101-scripted-live-session-no-person-needed)) streamed a 46 s reference feed built from the almirah recordings through the real server, with the voice agent and every stage on Claude Haiku (the `dev` profile, through the local `claude` command; no paid key). The same script was also run through the web page in Chrome (`scripts/record_demo.py`). Both sweeps were then compared with the ground truth (`eval/results_live-*.md`).

| | Scripted session | Same script, in Chrome |
|---|---|---|
| Packet produced and valid | yes | yes |
| Book count (true 10) | **47** | **40** |
| Lines with a title (of the count) | 9 | 3 |
| Titles confidently wrong | 0 | 0 |
| Agent ended the sweep itself | yes | no: the recorder pressed End after 60 s |
| Time from end of sweep to packet | 748 s | about 740 s |

**5. The live path over-counts badly with a weak detector (47 and 40 books for 10).**
*What happens:* the packet counts four to five times the true number, and says so without a warning. The spoken summary repeats it ("I counted 47 books"). The packet is valid and every line is sourced, but the lines are mostly duplicates and unreadable.
*Root cause (likely, not proven):* the detector. Claude Haiku returns 7–14 spine boxes per keyframe on a scene with 10 books, and its boxes are loose (tall boxes reaching into empty space above the books). The inventory stage merges the same book across views by mapping boxes onto one reference frame, so loose boxes do not land on each other: 15 of the 47 lines were seen in 2 views and 10 in 3, where each book should be one line. The recorded sweeps were detected by Gemini Flash and counted 10 of 10, so the merge itself works with precise boxes. A first run on a feed that jumped between wide shots and close-up stills counted 45; making the feed one continuous recording did not fix it, which points at the model and not the feed.
*What to check first:* rerun detection on a copy of the live sweep with the `gemini` profile (12 calls) and compare the counts.
*What to change:* a detector that locates objects well is a requirement, not a choice (the design says so; this measures it). Add a count check that compares the final count with the most spines seen in any single view and sends a doubtful count to the review queue; it needs care, because a real room has many views.

**5b. The same script on stronger models.** The detector decides the count; spine reading is parallel since this run.

| Model (feed) | Count (true 10) | Titles | Time, end of sweep to packet |
|---|---|---|---|
| Claude Haiku (stitched feed with faults) | 40–47 | 3–9 lines | 740–748 s |
| Claude Sonnet 5.5, local `claude` command (same feed) | 19 | 10 lines | 76 s |
| Claude Opus 5.5, local `claude` command (same feed) | 15 | 11 lines | 117 s |
| Claude Opus 5.5 (one steady recording) | 12 | 10 lines, 5 of 9 correct | 67 s, 12 s and 229 s in three runs of one script (the 12 s run replayed cached answers; the 229 s run waited out shop timeouts: price 104 s) |
| **Gemini Flash, typed chat (one steady recording)** | **10** | **8 of 9 correct, none confidently wrong** | 477 s: detect 352 s waiting out API timeouts, identify 80 s, price 40 s |

Sonnet and Opus found the capture problems themselves: they refused to close the sweep because the footage showed mostly the green door, and called `request_recapture`. Parallel spine reading (four batches at once; it was one after another) took identify from 459 s to 26–80 s. So a detector that locates spines precisely is a requirement, and Gemini Flash meets it; Claude models through the CLI are good enough to show the flow, not to count. Prices in the Gemini demo: Mathematics XI ₹407 (hand check ₹396) and Word Power ₹172 (₹182) agree; Fundamental Physics ₹1,232 (₹834) and Secondary School Mathematics ₹261 (₹621) do not, because the shops list different editions at very different prices (error 1).

**6. Smaller findings from the same runs**
- *Reading from live frames:* live frames used to be capped at 1280 px, too small to cut readable spine crops from. They are now 1920 px (`live.evidence_side`). Haiku still read only 3 to 9 titles; the readings it did give were not wrong.
- *Warnings were debounced away:* glare, blur and a fast pan arrived within 6 s of each other, so only the first (all three kinds on the first glare frame) was reported; the later blur and rushed-pan stretches were silent. Each kind is debounced on its own, but one bad stretch of footage trips several kinds on its first frame, and the user hears a single warning for it.
- *Polling:* with typed chat on Gemini the agent called `sweep_status` about 40 times in a row while the pipeline ran, one model call each. Nothing told it processing had finished, so it kept asking. Fixed: the same tool call twice in a row, with nobody speaking in between, is dropped, and the prompt says not to poll (`live/cascaded.py`, unit-tested). The recorded Gemini demo still shows the repeats.
- *Blur warning on one soft frame:* a clean recording raised "frames are blurry" for a single soft frame (a playback glitch). Blur now needs two soft frames in a row (0.5 s at 4 fps; `live.blur_frames`); the faulted feed still triggers blur, glare and motion.
- *Replayed answers look like speed:* the recorder shares one answer cache across runs, so an identical second run replays model answers and finishes in seconds. `record_demo.py --fresh-cache` starts empty. Shop lookups also time out at times (all three shops did in one run, leaving no book priced), which a real session would show too.
- *The agent does not always end the sweep:* Haiku called `end_sweep` in one run of two. The End button (and in the simulator, a fallback press) covers it.
- *A correction that names no line:* "the Hindi book on the bottom shelf is a first edition" was recorded with the target "bottom shelf". It matches no book id, so the packet put it in the review queue ("matches no shelf, book or item") instead of applying it. Before this run it would have been dropped without a word.
- *Not tested:* Gemini Live audio (no quota), interruption by voice, a person's real camera and microphone.

**7. A fresh offline run on Claude Haiku (`claim replay` on `video_slow.mp4`, no API key).**
The whole pipeline ran in about 6 minutes (detect 163 s, identify 182 s, price 16 s, the rest under 3 s) and the packet validated, but accuracy was poor: count 13 for 10, 1 of 9 titles correct, and 2 of 10 books with a confidently wrong title (20%, against a limit of 3%). Gemini Flash read this same recording at 78% with none confidently wrong (`eval/results_slow.md`), so the reader model is the difference, and a key-less Haiku run is a check that the flow works, not a measure of accuracy.
*Two gaps in the safeguards showed up:* (a) the check that distrusts identical spine text missed a copy with the words in a different order ("MATHEMATICS / R.D. SHARMA" for two different books); (b) Haiku invented the spine "MARCEL PROUST" for a school textbook, the catalogue held a book of that name, so it passed as identified with full confidence, and its printed height (30 cm) then set the scale for its whole shelf: heights of 31 cm and a shelf run of 1.1 m for a cupboard about a third of that, flagged only by a 0.48 confidence.
*Fixed:* (a) word order is now ignored (`repeated_readings`, unit-tested). Rerun on the cached answers: confidently wrong titles 2 → 1, but one correct title was lost with the copy it shared text with (correct 1 → 0).
*Not fixed:* (b). A hallucinated reading that happens to match a catalogue entry cannot be caught from the text alone; what would help is requiring two identified books whose catalogue heights agree before a shelf takes its scale from the catalogue (today one book is enough, at 0.6 trust), and checking the reading from a second view.

## One fix, measured: class numbers must agree

*Error:* the first identify run took "MATHEMATICS / CLASS XI" by R.D. Sharma for the catalogue's "Mathematics class 8" at a score of 91, so a wrong title went out as confidently identified. The same weakness let the price stage quote other classes' books.

*Fix:* class and part numbers anywhere on the spine (Roman or after "Class", "Part") must appear in the catalogue or shop title (`text/match.py: level_numbers, numbers_agree`).

*Measured on the same cached readings and shop pages, with the check switched off and on (identify → packet → eval rerun on copies of each sweep):*

| `slow` sweep | Without the check | With the check |
|---|---|---|
| Quotes for a different class than the spine | 5 (R.D. Sharma XI priced from "Mathematics Class 6" and "Class 9"; Pradeep's XII from two Class 11 listings; one Class X listing for a spine read as "Class III") | 0 |
| Books priced | 4 | 2 |
| Prices within 25% of the hand check | 3 of 6 | 2 of 6 |

The check makes the price stage give fewer answers, but no wrong ones. Of the three prices that were "within 25%" without it, two came from other classes' books that happened to land near the right price: R.D. Sharma at ₹479.50 from the Class 6 and Class 9 listings (hand check ₹396), and Pradeep's XII at ₹917.50 from Class 11 listings (hand check ₹834). Titles were unchanged on all four sweeps in this rerun, because later matching rules (a title alone never reaches review; a distinguishing extra word such as "ICSE" keeps a match in review) now also block the original "class 8" case.

## Cost and latency per sweep

First runs (the only ones that call models; reruns replay the cache at no cost):

| Stage | Calls per sweep | Cost per sweep | Latency |
|---|---|---|---|
| 3 Detect (Gemini Flash, free tier) | 2–4 keyframes, plus retries | $0.01–0.03 | 35 s (`rush`) to 155 s (`far`: 13 attempts through 503 and quota errors) |
| 5 Identify (Claude Haiku via the CLI) | 2 (5 spines each; 8–10 in the live runs, which had 40+ lines) | about $0.10–0.24 estimated by the CLI (subscription, not billed) | 100–270 s; a hosted Flash model would take a few seconds |
| 7 Price (no model) | 30–90 page fetches | free | 10–30 s first time, under 1 s cached |
| Other stages | none | free | under 2 s each |

Time to packet is dominated by model latency and rate limits, not by the pipeline's own work. In the live runs on Haiku through the local CLI it was about 12 minutes (detect 137–209 s, identify 459–503 s, price 28 s, the rest under 15 s), which is above the 5-minute target; a hosted model answers these calls in seconds.

## With two more weeks
- Price from MRP plus sale median, with edition matching (error 1).
- Read each spine from two or three views and vote, instead of one best view; `slow` has three views of most books.
- Depth rung and an on-screen prompt for a scale sheet (error 3).
- A review screen to confirm a box, title or price and regenerate the packet.
- A headless-browser fetcher for the shops that refuse plain requests (Amazon, ThriftBooks).
