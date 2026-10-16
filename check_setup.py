#!/usr/bin/env python3
"""
Smoke test: confirms your key works by calling each Sarvam API once with a tiny
input. Total cost is well under ₹0.10. The results are cached, so re-running is free.

    python check_setup.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from heritage.sarvam_client import Sarvam  # noqa: E402  (also loads .env)

key = os.environ.get("SARVAM_API_KEY", "")
if not key or os.environ.get("SARVAM_MOCK") == "1":
    sys.exit("✗ No SARVAM_API_KEY found (or SARVAM_MOCK=1 is set). "
             "Put SARVAM_API_KEY=sk_... in .env first.")
print(f"• Key loaded: {key[:6]}…{key[-4:]}")

sb = Sarvam(mock=False)
ok = True


def step(name, fn):
    global ok
    try:
        out = fn()
        print(f"✔ {name}: {out}")
    except Exception as e:
        ok = False
        msg = str(e)
        hint = ""
        if type(e).__name__ in ("ProxyError", "ConnectError", "ConnectTimeout"):
            hint = "  → network problem: can this machine reach api.sarvam.ai?"
        elif "403" in msg:
            hint = "  → key rejected: check it on dashboard.sarvam.ai"
        elif "429" in msg:
            hint = "  → rate limited: wait a minute and retry"
        elif "402" in msg or "credit" in msg.lower():
            hint = "  → out of credits"
        print(f"✗ {name}: {type(e).__name__}: {msg[:200]}{hint}")


step("Translate (Mayura)", lambda: sb.translate("Hello.", "en-IN", "hi-IN"))
step("Chat (Sarvam-105B)", lambda: sb.chat_json(
    "Reply in JSON.", "Say hi in Hindi.",
    {"type": "object", "additionalProperties": False, "required": ["hi"],
     "properties": {"hi": {"type": "string"}}}, "smoke", max_tokens=512))
step("TTS (Bulbul v3)", lambda: f"{len(sb.tts('नमस्ते।', 'hi-IN'))} bytes of audio")

print(f"\nSpent so far: ₹{sb.ledger.spent:.4f}  (log: .cache/ledger.json)")
print("All good — run:  python run.py --sample --langs '' --no-audio" if ok
      else "Fix the ✗ above before running the pipeline.")
sys.exit(0 if ok else 1)
