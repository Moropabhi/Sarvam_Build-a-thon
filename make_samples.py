#!/usr/bin/env python3
"""
Builds the bundled test set: three FICTIONAL sources about one stepwell (baoli)
in the fictional village of Kesarpur. They deliberately disagree, so the story
engine has real branches to build:

  bahi_kesarpur_1854_hi.png   village ledger (Hindi)  → the Thakur paid for it
  collector_memo_1855_en.png  colonial memo (English) → a grain merchant paid for it
  oral_history_daadi_hi.wav   grandmother's interview (Hindi, 2 speakers)
                              → the village women, led by a widow, built it

Each file has a ground-truth sidecar (.txt for scans, .json in Sarvam STT format
for audio). Mock mode reads those sidecars; in live mode you can diff Sarvam's
output against them to measure OCR/STT accuracy.

    python make_samples.py               # images + silent placeholder audio (₹0)
    python make_samples.py --synthesize  # real 2-voice audio via Bulbul v3 (~₹2)
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
S = HERE / "samples"

BAHI_HI = """श्री गणेशाय नमः
संवत १९११, वैशाख सुदी पंचमी।
ग्राम केसरपुर में ठाकुर रणजीत सिंह जी के हुकुम से नई बावड़ी का काम पूरा हुआ।
खर्च रुपये १२०० लगे — रुपये ८०० ठाकुर साहब के खजाने से और रुपये ४०० गाँव की पंचायत से।
इस साल अकाल पड़ा था, इसलिए मजदूरों को मजदूरी में अनाज बाँटा गया।
बावड़ी में ग्यारह सीढ़ियाँ हैं और गहराई सात पुरुष है।
लिखा — मुनीम हरिदत्त, केसरपुर।"""

MEMO_EN = """Office of the Collector
14th March 1855
Memorandum
A new stepwell at Kesarpur was inspected this day.
The headman informs me that the work was chiefly paid for
by the grain merchant Seth Gopaldas, who advanced Rs. 900
on the security of the coming harvest.
The Thakur's own contribution appears to have been small.
The water is good and sufficient for the village and for travellers.
J. Whitcombe, Asst. Collector"""

# (speaker_id, v3 voice, text)
ORAL = [
    ("0", "aditya", "दादी, केसरपुर की बावड़ी किसने बनवाई थी?"),
    ("1", "roopa", "बेटा, कागज़ों में तो ठाकुर का नाम लिखा है। पर मेरी दादी बताती थीं कि जिस साल "
                   "अकाल पड़ा, गाँव की औरतें रोज़ चार कोस दूर से पानी लाती थीं।"),
    ("1", "roopa", "तब रुक्मिणी बाई नाम की एक विधवा ने अपने गहने बेच दिए और कहा, पहली कुदाल मैं चलाऊँगी।"),
    ("0", "aditya", "फिर क्या हुआ?"),
    ("1", "roopa", "फिर पूरे गाँव की औरतें साथ आ गईं। सेठ ने उधार दिया, और ठाकुर ने बाद में पत्थर "
                   "लगवाकर अपना नाम खुदवा दिया।"),
    ("1", "roopa", "आज भी बावड़ी की ग्यारहवीं सीढ़ी पर एक छोटा सा कंगन खुदा है। लोग कहते हैं वो "
                   "रुक्मिणी बाई की निशानी है।"),
]


def _font(paths: list[str], size: int):
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def aged_page(lines: list[str], font, size=(1400, 1000), seed=0, line_h=None,
              ink=(52, 33, 18), margin=(110, 110), title_idx=()) -> Image.Image:
    rnd = random.Random(seed)
    W, H = size
    page = Image.new("RGB", size, (232, 214, 172))
    # paper blotches
    blot = Image.new("L", size, 0)
    d = ImageDraw.Draw(blot)
    for _ in range(70):
        x, y, r = rnd.randint(0, W), rnd.randint(0, H), rnd.randint(20, 160)
        d.ellipse([x - r, y - r, x + r, y + r], fill=rnd.randint(10, 45))
    blot = blot.filter(ImageFilter.GaussianBlur(40))
    page.paste((170, 135, 85), mask=blot)
    # edge darkening
    edge = Image.new("L", size, 0)
    ImageDraw.Draw(edge).rectangle([0, 0, W, H], outline=120, width=50)
    page.paste((120, 90, 50), mask=edge.filter(ImageFilter.GaussianBlur(45)))
    # text with slight per-line jitter
    txt = Image.new("RGBA", size, (0, 0, 0, 0))
    td = ImageDraw.Draw(txt)
    line_h = line_h or int(font.size * 1.9)
    y = margin[1]
    for i, line in enumerate(lines):
        x = margin[0] + rnd.randint(-6, 6) + (40 if i in title_idx else 0)
        td.text((x, y + rnd.randint(-3, 3)), line, font=font,
                fill=ink + (rnd.randint(205, 245),))
        y += line_h
    txt = txt.rotate(rnd.uniform(-1.2, 1.2), resample=Image.BICUBIC, center=(W / 2, H / 2))
    txt = txt.filter(ImageFilter.GaussianBlur(0.6))
    page.paste(txt, (0, 0), txt)
    # speckle
    sp = ImageDraw.Draw(page)
    for _ in range(1600):
        x, y = rnd.randint(0, W - 1), rnd.randint(0, H - 1)
        c = rnd.randint(90, 160)
        sp.point((x, y), fill=(c, c - 20, c - 50))
    return page.filter(ImageFilter.GaussianBlur(0.4))


def make_images():
    S.mkdir(exist_ok=True)
    hi_font = _font([str(S / "fonts" / "NotoSerifDevanagari.ttf")], 40)
    hi_lines = []
    for ln in BAHI_HI.splitlines():                   # wrap long Hindi lines
        words, cur = ln.split(), ""
        for w in words:
            if hi_font.getlength(cur + " " + w) > 1180 and cur:
                hi_lines.append(cur); cur = w
            else:
                cur = f"{cur} {w}".strip()
        hi_lines.append(cur)
    img = aged_page(hi_lines, hi_font, size=(1400, 150 + 76 * len(hi_lines)), seed=7, line_h=76)
    img.save(S / "bahi_kesarpur_1854_hi.png")
    (S / "bahi_kesarpur_1854_hi.txt").write_text(BAHI_HI + "\n")

    en_font = _font(["/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
                     "DejaVuSerif-Italic.ttf"], 36)
    en_lines = MEMO_EN.splitlines()
    img = aged_page(en_lines, en_font, size=(1400, 150 + 66 * len(en_lines)), seed=11,
                    line_h=66, ink=(30, 30, 60))
    img.save(S / "collector_memo_1855_en.png")
    (S / "collector_memo_1855_en.txt").write_text(MEMO_EN + "\n")
    print("✔ wrote manuscript images + ground-truth .txt")


def make_audio(synthesize: bool):
    import sys
    sys.path.insert(0, str(HERE))
    from heritage.sarvam_client import Sarvam, concat_wavs, silent_wav, _wav_seconds

    sb = Sarvam(mock=not synthesize)
    parts, entries, t = [], [], 0.0
    gap = 0.6
    for spk, voice, text in ORAL:
        wav = sb.tts(text, language="hi-IN", speaker=voice, model="bulbul:v3", pace=0.95) \
            if synthesize else silent_wav(len(text) / 14)
        dur = _wav_seconds(wav)
        entries.append({"transcript": text, "start_time_seconds": round(t, 2),
                        "end_time_seconds": round(t + dur, 2), "speaker_id": spk})
        parts.append(wav)
        t += dur + gap
    (S / "oral_history_daadi_hi.wav").write_bytes(concat_wavs(parts, gap_s=gap))
    (S / "oral_history_daadi_hi.json").write_text(json.dumps({
        "transcript": " ".join(e["transcript"] for e in entries),
        "language_code": "hi-IN",
        "diarized_transcript": {"entries": entries},
        "_note": "Ground truth written by make_samples.py (same shape as Sarvam batch STT output).",
    }, ensure_ascii=False, indent=1))
    kind = "Bulbul v3 two-voice audio" if synthesize else "SILENT placeholder audio (mock only)"
    print(f"✔ wrote oral_history_daadi_hi.wav — {kind}, {t:.1f}s")
    if synthesize:
        print(f"  spent so far ₹{sb.ledger.spent:.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthesize", action="store_true",
                    help="use Bulbul (needs SARVAM_API_KEY) to voice the oral history")
    a = ap.parse_args()
    make_images()
    make_audio(a.synthesize)
