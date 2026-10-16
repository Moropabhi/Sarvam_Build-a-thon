"""
Thin wrapper around the Sarvam AI SDK with three things a ₹100 budget needs:

1. Disk cache  - every call is keyed by a hash of (operation, params, input bytes).
                 Re-running the pipeline never pays twice for the same thing.
2. Budget guard - every billable call is costed *before* it is sent and refused
                 if it would push spend over the limit (default ₹90 of ₹100).
3. Mock mode   - SARVAM_MOCK=1 (or no API key) returns deterministic fake output
                 so you can develop the whole app offline for ₹0.

Prices are from https://docs.sarvam.ai/api/getting-started/pricing (Oct 2026).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import time
import wave
import zipfile
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- pricing (INR)
PRICE = {
    "doc_digitise_per_page": 0.5,
    "stt_per_hour": 30.0,
    "stt_diarized_per_hour": 45.0,
    "translate_per_10k_chars": 20.0,
    "tts_per_10k_chars": 30.0,          # bulbul:v3 (v4-flash: check pricing page)
    "llm_in_per_1m": 15.0,              # sarvam-105b
    "llm_out_per_1m": 60.0,
}

def _load_dotenv() -> None:
    """Read KEY=VALUE lines from .env (project root) without overriding real env vars."""
    for p in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break


_load_dotenv()
CACHE_DIR = Path(os.environ.get("HERITAGE_CACHE", ".cache"))
BUDGET_INR = float(os.environ.get("SARVAM_BUDGET_INR", "90"))


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Ledger:
    path: Path

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"spent_inr": 0.0, "calls": []}

    @property
    def spent(self) -> float:
        return self._load()["spent_inr"]

    def check(self, cost: float, what: str) -> None:
        if self.spent + cost > BUDGET_INR:
            raise BudgetExceeded(
                f"{what} would cost ~₹{cost:.2f}; spent ₹{self.spent:.2f} of "
                f"₹{BUDGET_INR:.0f} limit. Raise SARVAM_BUDGET_INR to override."
            )

    def add(self, cost: float, what: str) -> None:
        data = self._load()
        data["spent_inr"] = round(data["spent_inr"] + cost, 4)
        data["calls"].append({"t": time.strftime("%F %T"), "what": what, "inr": round(cost, 4)})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _key(op: str, params: dict, blob: bytes = b"") -> str:
    h = hashlib.sha256()
    h.update(op.encode())
    h.update(json.dumps(params, sort_keys=True, ensure_ascii=False).encode())
    h.update(blob)
    return h.hexdigest()[:24]


def _wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / float(w.getframerate())


def silent_wav(seconds: float, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def concat_wavs(parts: list[bytes], gap_s: float = 0.0) -> bytes:
    """Join WAV byte-strings that share format, with optional silence between."""
    if not parts:
        return silent_wav(0.1)
    with wave.open(io.BytesIO(parts[0])) as w0:
        params = w0.getparams()
    gap = b"\x00" * int(gap_s * params.framerate) * params.sampwidth * params.nchannels
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        for i, p in enumerate(parts):
            with wave.open(io.BytesIO(p)) as r:
                if (r.getframerate(), r.getsampwidth(), r.getnchannels()) != (
                    params.framerate, params.sampwidth, params.nchannels):
                    raise ValueError("WAV parts have different formats")
                w.writeframes(r.readframes(r.getnframes()))
            if gap and i < len(parts) - 1:
                w.writeframes(gap)
    return out.getvalue()


class Sarvam:
    def __init__(self, api_key: str | None = None, mock: bool | None = None):
        api_key = api_key or os.environ.get("SARVAM_API_KEY")
        if mock is None:
            mock = os.environ.get("SARVAM_MOCK") == "1" or not api_key
        self.mock = mock
        self.ledger = Ledger(CACHE_DIR / ("ledger_mock.json" if mock else "ledger.json"))
        self.client = None
        if not mock:
            from sarvamai import SarvamAI
            self.client = SarvamAI(api_subscription_key=api_key)

    # ------------------------------------------------------------ cache helpers
    def _cache_get(self, key: str, suffix: str = ".json"):
        p = CACHE_DIR / "calls" / f"{key}{suffix}"
        if not p.exists():
            return None
        return p.read_bytes() if suffix != ".json" else json.loads(p.read_text())

    def _cache_put(self, key: str, value, suffix: str = ".json"):
        p = CACHE_DIR / "calls" / f"{key}{suffix}"
        p.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".json":
            p.write_text(json.dumps(value, ensure_ascii=False, indent=1))
        else:
            p.write_bytes(value)

    # ============================================================ Document AI
    def digitise(self, path: str | Path, language: str = "hi-IN", pages: int = 1) -> str:
        """OCR a PDF/PNG/JPG (≤10 pages) with Sarvam Vision. Returns Markdown."""
        path = Path(path)
        blob = path.read_bytes()
        key = _key("digitise", {"language": language}, blob)
        if (hit := self._cache_get(key)) is not None:
            return hit["markdown"]

        cost = PRICE["doc_digitise_per_page"] * pages
        self.ledger.check(cost, f"OCR {path.name}")

        if self.mock:
            side = path.with_suffix(".txt")
            md = side.read_text() if side.exists() else f"[mock OCR of {path.name}]"
        else:
            mime = {".pdf": "application/pdf", ".png": "image/png",
                    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".zip": "application/zip"}[path.suffix.lower()]
            with open(path, "rb") as f:
                try:   # newer SDKs accept output_format; older ones default to HTML
                    job = self.client.doc_ai.digitise(
                        file=[(path.name, f, mime)], language=language, output_format="md")
                except TypeError:
                    f.seek(0)
                    job = self.client.doc_ai.digitise(
                        file=[(path.name, f, mime)], language=language)
            terminal = {"completed", "partially_completed", "failed", "rejected"}
            while True:
                st = self.client.doc_ai.get_status(job_id=job.job_id)
                if st.status.lower() in terminal:
                    break
                time.sleep(3)
            if st.status.lower() not in ("completed", "partially_completed"):
                raise RuntimeError(f"OCR job {job.job_id} ended as {st.status}")
            pages = st.usage.pages_processed or pages
            cost = PRICE["doc_digitise_per_page"] * pages
            import httpx
            dl = self.client.doc_ai.get_download_url(job_id=job.job_id)
            z = zipfile.ZipFile(io.BytesIO(httpx.get(dl.url, timeout=120).content))
            names = sorted(z.namelist())
            md_files = [n for n in names if n.endswith(".md")]
            if md_files:
                md = "\n\n".join(z.read(n).decode("utf-8") for n in md_files)
            else:
                html_files = [n for n in names if n.endswith(".html")]
                md = "\n\n".join(html_to_text(z.read(n).decode("utf-8")) for n in html_files)

        self.ledger.add(cost, f"OCR {path.name}")
        self._cache_put(key, {"markdown": md})
        return md

    # ============================================================ Speech-to-text
    def transcribe(self, path: str | Path, language: str = "hi-IN",
                   diarize: bool = True, num_speakers: int = 2) -> dict:
        """Batch STT (handles up to 2 h). Returns Sarvam's JSON:
        {transcript, timestamps{chunks,start_time_seconds,end_time_seconds},
         diarized_transcript{entries[{transcript,start_time_seconds,end_time_seconds,speaker_id}]}}"""
        path = Path(path)
        blob = path.read_bytes()
        params = {"language": language, "diarize": diarize, "n": num_speakers}
        key = _key("stt", params, blob)
        if (hit := self._cache_get(key)) is not None:
            return hit

        try:
            secs = _wav_seconds(blob)
        except Exception:
            secs = 600  # unknown format: assume 10 min for the budget check
        rate = PRICE["stt_diarized_per_hour" if diarize else "stt_per_hour"]
        cost = rate * secs / 3600
        self.ledger.check(cost, f"STT {path.name}")

        if self.mock:
            side = path.with_suffix(".json")
            out = json.loads(side.read_text()) if side.exists() else {
                "transcript": f"[mock transcript of {path.name}]",
                "diarized_transcript": {"entries": [{
                    "transcript": f"[mock transcript of {path.name}]",
                    "start_time_seconds": 0.0, "end_time_seconds": secs,
                    "speaker_id": "0"}]}}
        else:
            job = self.client.speech_to_text_job.create_job(
                model="saaras:v4", mode="transcribe", language_code=language,
                with_diarization=diarize, num_speakers=num_speakers if diarize else None)
            job.upload_files(file_paths=[str(path)])
            job.start()
            job.wait_until_complete()
            res = job.get_file_results()
            if not res["successful"]:
                raise RuntimeError(f"STT failed: {res['failed']}")
            outdir = CACHE_DIR / "stt_out" / key
            job.download_outputs(output_dir=str(outdir))
            out = json.loads(next(outdir.glob("*.json")).read_text())

        self.ledger.add(cost, f"STT {path.name} ({secs:.0f}s)")
        self._cache_put(key, out)
        return out

    # ============================================================ Chat (LLM)
    def chat_json(self, system: str, user: str, schema: dict, name: str,
                  mock_fn=None, max_tokens: int = 4096) -> dict:
        """Sarvam-105B with strict JSON-schema output. Returns parsed dict."""
        params = {"system": system, "user": user, "schema": schema}
        key = _key("chat", params)
        if (hit := self._cache_get(key)) is not None:
            return hit

        est_in = (len(system) + len(user)) / 3
        cost = est_in * PRICE["llm_in_per_1m"] / 1e6 + max_tokens * PRICE["llm_out_per_1m"] / 1e6
        self.ledger.check(cost, f"LLM {name}")

        if self.mock:
            out = mock_fn() if mock_fn else {}
            real_cost = 0.0
        else:
            resp = self.client.chat.completions(
                model="sarvam-105b",
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                response_format={"type": "json_schema", "json_schema": {
                    "name": name, "strict": True, "schema": schema}},
                reasoning_effort="low",
                temperature=0.4,
                max_tokens=max_tokens,
            )
            choice = resp.choices[0]
            if not choice.message.content:
                raise RuntimeError(f"LLM returned no content (finish_reason={choice.finish_reason}). "
                                   "Raise max_tokens or set reasoning_effort=None.")
            out = json.loads(choice.message.content)
            u = resp.usage
            real_cost = (u.prompt_tokens * PRICE["llm_in_per_1m"]
                         + u.completion_tokens * PRICE["llm_out_per_1m"]) / 1e6

        self.ledger.add(real_cost, f"LLM {name}")
        self._cache_put(key, out)
        return out

    # ============================================================ Translate
    def translate(self, text: str, source: str, target: str) -> str:
        if source == target or not text.strip():
            return text
        parts = []
        for chunk in split_text(text, 900):
            key = _key("translate", {"s": source, "t": target, "x": chunk})
            if (hit := self._cache_get(key)) is not None:
                parts.append(hit["text"])
                continue
            cost = len(chunk) * PRICE["translate_per_10k_chars"] / 1e4
            self.ledger.check(cost, "translate")
            if self.mock:
                out = f"[{target}] {chunk}"
            else:
                r = self.client.text.translate(
                    input=chunk, source_language_code=source, target_language_code=target)
                out = r.translated_text
            self.ledger.add(cost, f"translate {source}->{target} {len(chunk)}ch")
            self._cache_put(key, {"text": out})
            parts.append(out)
        return " ".join(parts)

    # ============================================================ Text-to-speech
    def tts(self, text: str, language: str, speaker: str = "anand",
            model: str = "bulbul:v3", pace: float = 1.0) -> bytes:
        """Returns WAV bytes (22.05 kHz). Splits text >2500 chars automatically."""
        parts = []
        for chunk in split_text(text, 2400):
            params = {"l": language, "s": speaker, "m": model, "p": pace, "x": chunk}
            key = _key("tts", params)
            if (hit := self._cache_get(key, ".wav")) is not None:
                parts.append(hit)
                continue
            cost = len(chunk) * PRICE["tts_per_10k_chars"] / 1e4
            self.ledger.check(cost, f"TTS {len(chunk)}ch")
            if self.mock:
                wav = silent_wav(max(1.0, len(chunk) / 14))  # ~14 chars/sec speech
            else:
                r = self.client.text_to_speech.convert(
                    text=chunk, language_code=language, model=model,
                    speaker=speaker, pace=pace, speech_sample_rate=22050)
                wav = base64.b64decode("".join(r.audios))
            self.ledger.add(cost, f"TTS {model}/{speaker} {len(chunk)}ch")
            self._cache_put(key, wav, ".wav")
            parts.append(wav)
        return concat_wavs(parts, gap_s=0.25)


def html_to_text(html: str) -> str:
    """Crude HTML -> text: block tags become paragraph breaks."""
    import re
    import html as h
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)</?(p|div|br|h[1-6]|li|tr|section|article)[^>]*>", "\n\n", html)
    text = h.unescape(re.sub(r"<[^>]+>", "", html))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def split_text(text: str, limit: int) -> list[str]:
    """Split on sentence boundaries (handles Devanagari danda '।') to ≤limit chars."""
    import re
    sents = re.split(r"(?<=[.!?।॥])\s+", text.strip())
    chunks, cur = [], ""
    for s in sents:
        while len(s) > limit:            # pathological long sentence
            chunks.append(s[:limit]); s = s[limit:]
        if len(cur) + len(s) + 1 > limit and cur:
            chunks.append(cur); cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return chunks or [""]


def estimate_cost(chars_tts: int = 0, chars_translate: int = 0, ocr_pages: int = 0,
                  audio_seconds: float = 0, diarize: bool = True) -> float:
    return (chars_tts * PRICE["tts_per_10k_chars"] / 1e4
            + chars_translate * PRICE["translate_per_10k_chars"] / 1e4
            + ocr_pages * PRICE["doc_digitise_per_page"]
            + audio_seconds / 3600 * PRICE["stt_diarized_per_hour" if diarize else "stt_per_hour"])
