# Library Contents Claim Agent

A live voice-and-vision agent that inventories a home library from one camera sweep. It counts, identifies, measures and prices every book from its spine, prices non-book items, measures the room, and produces a claim packet in which every number is traced to a frame or a price source.

Design: [docs/DESIGN.md](docs/DESIGN.md) · Implementation: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) · Testing: [docs/TESTING.md](docs/TESTING.md)

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 automatically).

```bash
uv sync
cp .env.example .env          # add at least one provider key, e.g. GEMINI_API_KEY
uv run pytest                 # unit tests: no network, no keys needed
uv run claim ingest tests-local-data/video_slow.mp4 --id slow
uv run claim --help
```

Providers are chosen per model stage in `config/settings.yaml` (`models.profile`, or `models.overrides` for one stage).

## Test data and limitations

The system is built to the full specification. The test data it was checked on is smaller than the target scenario, because a larger physical space wasn't available:

| Target scenario | Test data actually used | Effect on reported results |
|---|---|---|
| At least 60 books across at least 2 shelving units | About 11 books on 1 almirah, in 3 photos and 3 short videos (`tests-local-data/`, kept locally and not published) | Counting and identification are tested at small scale |
| At least 8 non-book items in one enclosed room | Almirah only; no room footage | Item detection and pricing are tested in code and on whatever appears in the captures; item recall isn't measured |
| One continuous sweep of the whole room | Videos of 2–6 seconds of the shelves | Room dimensions and areas come out empty, with review reasons |
| Metric scale from a reference object in view | No scale card in any capture | Dimensions come from known-size objects or the depth model, at medium or low confidence |
| 20 hand-measured spines and a tape-measured room | Not available | Dimension and room accuracy are reported as "not measured" |
| Live sweep on a phone | Laptop camera on `localhost`; phone access through a `cloudflared` tunnel is supported | Live agent tested mainly at a desk |

**Ground truth that was collected:** the true book count and hand-read titles from the photos, and hand-checked prices from the same online shops the system uses. Accuracy for count, identification and pricing is measured against these. See `eval/`.
