#!/usr/bin/env python3
"""
Heritage Storytelling Agent — turn manuscripts + oral histories into an
interactive, narrated, multilingual story.

Offline test (₹0, no key needed):
    SARVAM_MOCK=1 python run.py --sample

Real run on the bundled samples (~₹15 with --langs '' ; ~₹40 with English too):
    export SARVAM_API_KEY=...
    python make_samples.py --synthesize      # one-time, makes the oral-history audio (~₹2)
    python run.py --sample

Your own sources:
    python run.py --doc scans/page1.jpg:hi-IN --doc letter.pdf:en-IN \
                  --audio interview.wav:hi-IN --story-lang hi-IN --langs en-IN \
                  --out out/my_story
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from heritage import pipeline as P
from heritage.sarvam_client import Sarvam, BudgetExceeded, estimate_cost, BUDGET_INR

SAMPLES = Path(__file__).parent / "samples"


def parse_src(s: str) -> tuple[str, str]:
    path, _, lang = s.rpartition(":")
    if not path:                       # no ":lang" given
        return s, "hi-IN"
    return path, lang


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", action="append", default=[], help="manuscript/record PATH:LANG (pdf/png/jpg, ≤10 pages)")
    ap.add_argument("--audio", action="append", default=[], help="oral recording PATH:LANG (wav/mp3)")
    ap.add_argument("--sample", action="store_true", help="use the bundled Kesarpur stepwell samples")
    ap.add_argument("--story-lang", default="hi-IN", help="language the story is written in")
    ap.add_argument("--langs", default="en-IN", help="extra languages, comma-separated ('' for none)")
    ap.add_argument("--speakers", type=int, default=2, help="speakers in oral recordings (1 = no diarization)")
    ap.add_argument("--max-nodes", type=int, default=8)
    ap.add_argument("--tts-model", default="bulbul:v3", help="bulbul:v3 (default) or bulbul:v4-flash")
    ap.add_argument("--voice", action="append", default=[],
                    help="LANG=SPEAKER, e.g. hi-IN=anand (v3) or hi-IN=anand_hi_documentary (v4-flash)")
    ap.add_argument("--no-audio", action="store_true", help="skip TTS (cheapest way to iterate on the story)")
    ap.add_argument("--yes", action="store_true", help="don't ask before spending on TTS")
    ap.add_argument("--out", default="out/story")
    a = ap.parse_args()

    if a.sample:
        a.doc = [f"{SAMPLES/'bahi_kesarpur_1854_hi.png'}:hi-IN",
                 f"{SAMPLES/'collector_memo_1855_en.png'}:en-IN"]
        a.audio = [f"{SAMPLES/'oral_history_daadi_hi.wav'}:hi-IN"]
        a.out = a.out if a.out != "out/story" else "out/kesarpur"
    if not a.doc and not a.audio:
        ap.error("give at least one --doc or --audio (or --sample)")

    sb = Sarvam()
    mode = "MOCK (offline, ₹0)" if sb.mock else "LIVE"
    print(f"▶ Sarvam mode: {mode} | spent so far ₹{sb.ledger.spent:.2f} / limit ₹{BUDGET_INR:.0f}")
    out = Path(a.out)
    targets = [l.strip() for l in a.langs.split(",") if l.strip()]
    voices = dict(v.split("=", 1) for v in a.voice)

    try:
        print("1/5 Ingest: OCR + speech-to-text …")
        sources = P.ingest(sb, [parse_src(d) for d in a.doc], [parse_src(x) for x in a.audio],
                           num_speakers=a.speakers)
        for s in sources:
            print(f"    {s['id']} {s['kind']:6s} {len(s['segments']):3d} segments  {s['title']}")

        print("2/5 Extract facts & disagreements (Sarvam-105B) …")
        facts = P.extract_facts(sb, sources)
        print(f"    {len(facts['events'])} events, {len(facts['disagreements'])} disagreements")

        print("3/5 Build branching story (Sarvam-105B) …")
        story = P.build_story(sb, sources, facts, a.story_lang, a.max_nodes)
        print(f"    '{story['title']}' — {len(story['nodes'])} nodes, "
              f"{sum(n['ending'] for n in story['nodes'])} endings")
        for w in story["_warnings"]:
            print(f"    ⚠ {w}")

        tr_chars = P.translation_chars(story, len([t for t in targets if t != a.story_lang]))
        print(f"4/5 Translate → {targets or 'none'}  (~{tr_chars} chars, ~₹{estimate_cost(chars_translate=tr_chars):.2f})")
        texts = P.localize(sb, story, a.story_lang, targets)

        audio = None
        if not a.no_audio:
            chars = P.narration_chars(texts)
            est = estimate_cost(chars_tts=chars)
            print(f"5/5 Narrate with {a.tts_model}: {chars} chars ≈ ₹{est:.2f} "
                  f"(uncached parts only are billed)")
            if not sb.mock and not a.yes:
                if input("    Proceed? [y/N] ").strip().lower() != "y":
                    print("    Skipped narration. Re-run with --yes when the story looks right.")
                    a.no_audio = True
            if not a.no_audio:
                audio = P.narrate(sb, texts, out, a.tts_model, voices)
        else:
            print("5/5 Narration skipped (--no-audio)")

        player = P.export(out, sources, facts, story, texts, audio, a.story_lang)
    except BudgetExceeded as e:
        print(f"\n✋ Budget guard: {e}")
        sys.exit(2)

    label = "Estimated cost if this were a live run" if sb.mock else "Spent so far"
    print(f"\n✔ Done. {label}: ₹{sb.ledger.spent:.2f}.")
    print(f"  Open {player} in a browser (or: python -m http.server -d {out})")


if __name__ == "__main__":
    main()
