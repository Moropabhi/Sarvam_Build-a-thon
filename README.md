# Heritage Storyteller — Sarvam AI

Turns **historical records** (manuscript scans, letters) and **oral histories**
(recorded interviews) into a **branching, narrated, multilingual audio story**,
where every scene cites the exact line of the source it came from.

Built for idea #26, *Heritage Storytelling Agent*, from the Sarvam idea library.

## Pipeline

```
scans/PDFs ──► Sarvam Vision (doc_ai.digitise) ─┐
                                               ├─► segments with ids (D1.p3, A1.t2)
audio      ──► Saaras v4 batch STT + diarization┘
                     │
                     ▼  Sarvam-105B, strict JSON schema
              facts + DISAGREEMENTS between sources
                     │
                     ▼  Sarvam-105B, strict JSON schema → validator → 1 repair round
              story graph: nodes, choices, refs, basis (record / oral / mixed / dramatized)
                     │
                     ▼  Sarvam Translate
              same story in each target language
                     │
                     ▼  Bulbul TTS
              audio/<lang>/<node>.wav  ──►  player.html
```

The central idea is that the branches come from **disagreements** between the
sources. The listener chooses whose account to follow, for example the ledger,
the colonial memo, or grandma. Each scene is tagged with how it is known.

## Execution steps (Fedora / any Linux)

```bash
# 0. Unzip and enter the project
unzip heritage-storyteller.zip && cd heritage-storyteller

# 1. Python environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. API key. Put it in .env, which is git-ignored and loaded automatically.
cp .env.example .env
nvim .env                     # set SARVAM_API_KEY=sk_...

# 3. Offline dry run. Costs ₹0; proves the code works before touching credits.
SARVAM_MOCK=1 python run.py --sample
xdg-open out/kesarpur/player.html

# 4. Check the key. Costs under ₹0.10 and calls translate, chat and TTS once each.
python check_setup.py

# 5. Real audio for the oral-history sample. One-time, about ₹2.
python make_samples.py --synthesize

# 6. Real story, without narration first (under ₹2). Open the player and read it.
rm -rf out/kesarpur
python run.py --sample --langs "" --no-audio
xdg-open out/kesarpur/player.html

# 7. Happy with it? Narrate it. This reuses the cached story, so you only pay for TTS.
python run.py --sample --langs ""        # Hindi narration, about ₹15 total so far
python run.py --sample                   # optional: adds English, about ₹40 total
```

How much you have spent is logged in `.cache/ledger.json`.
**Don't delete `.cache/`** between runs, because it is what makes re-runs free.

To run on your own sources:

```bash
python run.py --doc scans/page1.jpg:hi-IN --doc letter.pdf:en-IN \
              --audio interview.wav:hi-IN --speakers 2 \
              --story-lang hi-IN --langs en-IN,mr-IN --out out/my_story
```

Useful flags:

- `--tts-model bulbul:v4-flash --voice hi-IN=anand_hi_documentary` uses v4-flash narration voices.
- `--max-nodes 6` makes a shorter story, which means cheaper narration.

## Staying inside ₹100

| Guard | What it does |
|---|---|
| Disk cache (`.cache/`) | Every call is keyed by its inputs, so re-runs are free. |
| Budget guard | Each call is costed before it is sent. The run stops at `SARVAM_BUDGET_INR`, default **₹90**. |
| Mock mode | `SARVAM_MOCK=1` runs the whole pipeline for ₹0. `.cache/ledger_mock.json` then shows what a live run would cost. |
| `--no-audio` | Iterate on the story without paying for TTS, which is about 70% of the cost. |
| Confirm prompt | Shows a TTS estimate and asks before spending (skip with `--yes`). |

Real spend is logged in `.cache/ledger.json`.

Prices used (Oct 2026), from <https://docs.sarvam.ai/api/getting-started/pricing>:

| Service | Price |
|---|---|
| Document Digitization | ₹0.5 / page |
| Speech-to-text | ₹30 / hour (₹45 with diarization) |
| Sarvam-105B | ₹15 / ₹60 per 1M tokens (in / out) |
| Translate | ₹20 / 10K characters |
| Bulbul v3 | ₹30 / 10K characters |

## Bundled samples (fictional)

The samples are three invented sources about one stepwell in the invented
village of Kesarpur. They are **not real history**. They were written to
disagree on purpose:

| File | Kind | Says the stepwell was paid for by… |
|---|---|---|
| `bahi_kesarpur_1854_hi.png` | village ledger, Hindi | the Thakur (₹800) and the panchayat (₹400) |
| `collector_memo_1855_en.png` | colonial memo, English | grain merchant Seth Gopaldas (Rs. 900) |
| `oral_history_daadi_hi.wav` | interview, Hindi, 2 speakers | the village women, led by the widow Rukmini Bai |

Each sample has a ground-truth sidecar: a `.txt` file for the scans and a `.json` file for the audio.
You can diff Sarvam's OCR and STT output against these to report accuracy in your presentation.

For real material, look at public-domain scans on archive.org (old district
gazetteers, travel accounts), the National Archives of India portal
*Abhilekh Patal*, or record an elder telling a local legend.
Check the licence before you use anything.

## Files

```
run.py                   CLI that runs the whole pipeline
make_samples.py          regenerates the test sources (and voices them with --synthesize)
heritage/sarvam_client.py  SDK wrapper: cache, budget, mock, WAV utilities
heritage/pipeline.py     ingest → facts → story → translate → narrate → export
player/player.html       interactive player (the story JSON is embedded at export)
samples/                 test sources and ground truth
```

## Notes and gotchas

- Doc AI accepts at most 10 pages per job. Split larger PDFs first.
- REST STT is limited to 30 s, so this project always uses the batch STT API, which handles up to 2 h.
- Bulbul REST accepts at most 2,500 characters per call. The wrapper splits text on `।` and `.` boundaries automatically.
- Sarvam-105B thinks by default, and reasoning tokens count against `max_tokens`. The wrapper uses `reasoning_effort="low"` and `max_tokens=4096`, which is the Starter plan cap.
- The player works straight from `file://`. If your browser blocks audio, serve the folder with `python -m http.server -d out/kesarpur`.
