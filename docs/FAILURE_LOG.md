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
| 5 Identify (Claude Haiku via the CLI) | 2 (5 spines each) | about $0.10–0.24 estimated by the CLI (subscription, not billed) | 100–270 s; a hosted Flash model would take a few seconds |
| 7 Price (no model) | 30–90 page fetches | free | 10–30 s first time, under 1 s cached |
| Other stages | none | free | under 2 s each |

Time to packet is dominated by model latency and rate limits, not by the pipeline's own work.

## With two more weeks
- Price from MRP plus sale median, with edition matching (error 1).
- Read each spine from two or three views and vote, instead of one best view; `slow` has three views of most books.
- Depth rung and an on-screen prompt for a scale sheet (error 3).
- A review screen to confirm a box, title or price and regenerate the packet.
- A headless-browser fetcher for the shops that refuse plain requests (Amazon, ThriftBooks).
