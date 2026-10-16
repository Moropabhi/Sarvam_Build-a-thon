"""
Heritage Storytelling pipeline

  sources (manuscript scans, oral recordings)
     │  Sarvam Vision (doc_ai.digitise)  /  Saaras v4 batch STT (+diarization)
     ▼
  segments  ── every paragraph / speaker turn gets a citable id: D1.p3, A1.t2
     │  Sarvam-105B  (strict JSON schema)
     ▼
  facts     ── people, places, events, and DISAGREEMENTS between sources
     │  Sarvam-105B  (strict JSON schema) + validator + 1 repair round
     ▼
  story graph ── branching nodes; each node cites segment ids and is tagged
     │           record / oral / mixed / dramatized
     │  Sarvam Translate (Mayura)
     ▼
  localized story ── one copy per target language
     │  Bulbul TTS
     ▼
  narration audio ── out/audio/<lang>/<node>.wav  →  player.html
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .sarvam_client import Sarvam, estimate_cost

LANG_NAMES = {
    "hi-IN": "Hindi", "en-IN": "English", "bn-IN": "Bengali", "ta-IN": "Tamil",
    "te-IN": "Telugu", "mr-IN": "Marathi", "gu-IN": "Gujarati", "kn-IN": "Kannada",
    "ml-IN": "Malayalam", "pa-IN": "Punjabi", "od-IN": "Odia",
}

# Default Bulbul v3 narrator per language (v3 speakers work for every v3 language).
DEFAULT_VOICE = {"hi-IN": "anand", "en-IN": "anand"}

NARRATION_CAP = 650   # chars per node: keeps TTS cost predictable


# =============================================================== 1. INGEST
def _paragraphs(text: str) -> list[str]:
    text = re.sub(r"[#*_>`|]+", " ", text)             # strip markdown symbols
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n|\n", text)]
    return [p for p in paras if len(p) > 2]


def ingest(sb: Sarvam, docs: list[tuple[str, str]], audios: list[tuple[str, str]],
           num_speakers: int = 2) -> list[dict]:
    sources = []
    for i, (path, lang) in enumerate(docs, 1):
        md = sb.digitise(path, language=lang)
        sources.append({
            "id": f"D{i}", "kind": "record", "file": str(path), "language": lang,
            "title": Path(path).stem.replace("_", " "),
            "segments": [{"id": f"D{i}.p{j}", "text": t}
                         for j, t in enumerate(_paragraphs(md), 1)],
        })
    for i, (path, lang) in enumerate(audios, 1):
        stt = sb.transcribe(path, language=lang, diarize=num_speakers > 1,
                            num_speakers=num_speakers)
        entries = (stt.get("diarized_transcript") or {}).get("entries") or []
        if not entries:                                     # no diarization
            ts = stt.get("timestamps") or {}
            entries = [{"transcript": c, "start_time_seconds": s, "end_time_seconds": e,
                        "speaker_id": "0"} for c, s, e in zip(
                ts.get("chunks", []), ts.get("start_time_seconds", []),
                ts.get("end_time_seconds", []))] or [
                {"transcript": stt.get("transcript", ""), "start_time_seconds": 0,
                 "end_time_seconds": 0, "speaker_id": "0"}]
        sources.append({
            "id": f"A{i}", "kind": "oral", "file": str(path), "language": lang,
            "title": Path(path).stem.replace("_", " "),
            "segments": [{"id": f"A{i}.t{j}", "text": e["transcript"].strip(),
                          "speaker": f"Speaker {e.get('speaker_id', '0')}",
                          "start": round(float(e["start_time_seconds"]), 2),
                          "end": round(float(e["end_time_seconds"]), 2)}
                         for j, e in enumerate(entries, 1) if e["transcript"].strip()],
        })
    return sources


def sources_as_prompt(sources: list[dict]) -> str:
    out = []
    for s in sources:
        kind = "WRITTEN RECORD" if s["kind"] == "record" else "ORAL TESTIMONY"
        out.append(f"### Source {s['id']} — {kind} ({LANG_NAMES.get(s['language'], s['language'])}): {s['title']}")
        for seg in s["segments"]:
            who = f" [{seg['speaker']}]" if "speaker" in seg else ""
            out.append(f"[{seg['id']}]{who} {seg['text']}")
        out.append("")
    return "\n".join(out)


# =============================================================== 2. FACTS
FACTS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["people", "places", "events", "disagreements"],
    "properties": {
        "people": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "role", "refs"],
            "properties": {"name": {"type": "string"}, "role": {"type": "string"},
                           "refs": {"type": "array", "items": {"type": "string"}}}}},
        "places": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "refs"],
            "properties": {"name": {"type": "string"},
                           "refs": {"type": "array", "items": {"type": "string"}}}}},
        "events": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["when", "what", "basis", "refs"],
            "properties": {"when": {"type": "string"}, "what": {"type": "string"},
                           "basis": {"type": "string", "enum": ["record", "oral"]},
                           "refs": {"type": "array", "items": {"type": "string"}}}}},
        "disagreements": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["topic", "versions"],
            "properties": {"topic": {"type": "string"}, "versions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["claim", "basis", "refs"],
                "properties": {"claim": {"type": "string"},
                               "basis": {"type": "string", "enum": ["record", "oral"]},
                               "refs": {"type": "array", "items": {"type": "string"}}}}}}}},
    },
}

FACTS_SYSTEM = """You are a careful archivist. Extract ONLY what the sources state.
Every item must cite the bracketed segment ids it comes from (e.g. "D1.p3", "A1.t2").
Never add names, dates, numbers or places that are not in the sources.
Pay special attention to DISAGREEMENTS: points where written records and oral
testimony (or two records) give different accounts of the same thing. List each
version separately with its own refs. Write in English."""


def extract_facts(sb: Sarvam, sources: list[dict]) -> dict:
    return sb.chat_json(FACTS_SYSTEM, sources_as_prompt(sources), FACTS_SCHEMA,
                        "heritage_facts", mock_fn=lambda: _mock_facts(sources))


# =============================================================== 3. STORY
STORY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "logline", "start", "nodes"],
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "start": {"type": "string"},
        "nodes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "title", "narration", "basis", "refs", "choices", "ending"],
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string"},
                "narration": {"type": "string"},
                "basis": {"type": "string", "enum": ["record", "oral", "mixed", "dramatized"]},
                "refs": {"type": "array", "items": {"type": "string"}},
                "choices": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["label", "to"],
                    "properties": {"label": {"type": "string"}, "to": {"type": "string"}}}},
                "ending": {"type": "boolean"},
            }}},
    },
}


def story_system(lang: str, max_nodes: int) -> str:
    return f"""You turn historical sources into a short INTERACTIVE audio story.

Rules:
- Write every title, narration and choice label in {LANG_NAMES.get(lang, lang)}, in native script.
- {max(5, max_nodes - 3)} to {max_nodes} nodes. Node ids: n1, n2, ... ; "start" is the first node id.
- Each narration is spoken aloud: vivid, second-person or storyteller voice, at most {NARRATION_CAP} characters.
- Build the BRANCHES around the disagreements between sources: the listener chooses whose
  account to follow (the written record, the oral testimony, ...) and hears that version.
  Branches should rejoin or end in an ending node that lets the listener weigh the versions.
- 2 or 3 ending nodes (ending=true, choices=[]). Every other node has 1-3 choices whose "to"
  is an existing node id. Every node must be reachable from start.
- refs: segment ids (e.g. "D1.p3", "A1.t2") supporting the node. Only ids that exist.
- basis: "record" if only from written records, "oral" if only from oral testimony,
  "mixed" if both, "dramatized" only for connective scenes with no factual claims (refs may be empty).
- Never invent names, dates, amounts or places absent from the sources. Sensory scene-setting
  is fine; new facts are not. Do not take sides: present each version as that source's account."""


def build_story(sb: Sarvam, sources: list[dict], facts: dict, lang: str,
                max_nodes: int = 8) -> dict:
    user = (sources_as_prompt(sources) + "\n### Extracted facts (JSON)\n"
            + json.dumps(facts, ensure_ascii=False))
    seg_ids = {seg["id"] for s in sources for seg in s["segments"]}
    story = sb.chat_json(story_system(lang, max_nodes), user, STORY_SCHEMA, "heritage_story",
                         mock_fn=lambda: _mock_story(sources, facts, lang))
    errors = validate_story(story, seg_ids)
    if errors:   # one repair round: show the model its own output + the problems
        fix = (user + "\n### Your previous story (JSON)\n" + json.dumps(story, ensure_ascii=False)
               + "\n### Problems to fix\n- " + "\n- ".join(errors)
               + "\nReturn the corrected full story.")
        story = sb.chat_json(story_system(lang, max_nodes), fix, STORY_SCHEMA,
                             "heritage_story_fix", mock_fn=lambda: story)
        errors = validate_story(story, seg_ids)
    story = sanitize_story(story, seg_ids)
    story["_warnings"] = errors
    return story


def validate_story(story: dict, seg_ids: set[str]) -> list[str]:
    errs = []
    nodes = {n["id"]: n for n in story.get("nodes", [])}
    if story.get("start") not in nodes:
        errs.append(f"start '{story.get('start')}' is not a node id")
    for n in nodes.values():
        for c in n["choices"]:
            if c["to"] not in nodes:
                errs.append(f"{n['id']}: choice '{c['label']}' points to missing node '{c['to']}'")
        if n["ending"] and n["choices"]:
            errs.append(f"{n['id']}: ending node must have no choices")
        if not n["ending"] and not n["choices"]:
            errs.append(f"{n['id']}: dead end (not an ending but no choices)")
        bad = [r for r in n["refs"] if r not in seg_ids]
        if bad:
            errs.append(f"{n['id']}: unknown refs {bad}")
        if n["basis"] != "dramatized" and not n["refs"]:
            errs.append(f"{n['id']}: basis '{n['basis']}' needs refs")
        if len(n["narration"]) > NARRATION_CAP * 1.3:
            errs.append(f"{n['id']}: narration too long ({len(n['narration'])} chars)")
    # reachability
    seen, stack = set(), [story.get("start")]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in nodes:
            continue
        seen.add(cur)
        stack += [c["to"] for c in nodes[cur]["choices"]]
    unreach = set(nodes) - seen
    if unreach:
        errs.append(f"unreachable nodes: {sorted(unreach)}")
    if not any(n["ending"] for n in nodes.values()):
        errs.append("no ending node")
    return errs


def sanitize_story(story: dict, seg_ids: set[str]) -> dict:
    """Last-resort fixes so the player never breaks: drop bad refs/choices, trim text."""
    ids = {n["id"] for n in story["nodes"]}
    for n in story["nodes"]:
        n["refs"] = [r for r in n["refs"] if r in seg_ids]
        n["choices"] = [c for c in n["choices"] if c["to"] in ids]
        if not n["choices"]:
            n["ending"] = True
        if len(n["narration"]) > NARRATION_CAP * 1.3:
            n["narration"] = n["narration"][: int(NARRATION_CAP * 1.3)].rsplit(" ", 1)[0] + "…"
    if story["start"] not in ids:
        story["start"] = story["nodes"][0]["id"]
    return story


# =============================================================== 4. TRANSLATE
def localize(sb: Sarvam, story: dict, src: str, targets: list[str]) -> dict:
    """Returns {lang: {"title","logline","nodes":{id:{title,narration,choices:[labels]}}}}"""
    out = {src: _text_view(story)}
    for tgt in targets:
        if tgt == src:
            continue
        tr = lambda t: sb.translate(t, src, tgt)
        out[tgt] = {
            "title": tr(story["title"]), "logline": tr(story["logline"]),
            "nodes": {n["id"]: {"title": tr(n["title"]), "narration": tr(n["narration"]),
                                "choices": [tr(c["label"]) for c in n["choices"]]}
                      for n in story["nodes"]},
        }
    return out


def _text_view(story: dict) -> dict:
    return {"title": story["title"], "logline": story["logline"],
            "nodes": {n["id"]: {"title": n["title"], "narration": n["narration"],
                                "choices": [c["label"] for c in n["choices"]]}
                      for n in story["nodes"]}}


def translation_chars(story: dict, n_targets: int) -> int:
    t = _text_view(story)
    chars = len(t["title"]) + len(t["logline"]) + sum(
        len(v["title"]) + len(v["narration"]) + sum(map(len, v["choices"]))
        for v in t["nodes"].values())
    return chars * n_targets


# =============================================================== 5. NARRATE
def narration_chars(texts: dict) -> int:
    return sum(len(v["narration"]) for lang in texts.values() for v in lang["nodes"].values())


def narrate(sb: Sarvam, texts: dict, out_dir: Path, model: str, voices: dict) -> dict:
    """Writes out/audio/<lang>/<node>.wav ; returns {lang: {node: relpath}}"""
    paths = {}
    for lang, view in texts.items():
        voice = voices.get(lang) or DEFAULT_VOICE.get(lang, "anand")
        d = out_dir / "audio" / lang
        d.mkdir(parents=True, exist_ok=True)
        paths[lang] = {}
        for nid, v in view["nodes"].items():
            wav = sb.tts(v["narration"], language=lang, speaker=voice, model=model)
            p = d / f"{nid}.wav"
            p.write_bytes(wav)
            paths[lang][nid] = f"audio/{lang}/{nid}.wav"
    return paths


# =============================================================== 6. EXPORT
def export(out_dir: Path, sources: list[dict], facts: dict, story: dict, texts: dict,
           audio: dict | None, story_lang: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sources").mkdir(exist_ok=True)
    pub_sources = []
    for s in sources:
        src = Path(s["file"])
        dst = out_dir / "sources" / src.name
        if src.exists() and src.resolve() != dst.resolve():
            shutil.copy(src, dst)
        pub_sources.append({**s, "file": f"sources/{src.name}"})
    bundle = {
        "story_language": story_lang,
        "languages": list(texts.keys()),
        "start": story["start"],
        "graph": {n["id"]: {"basis": n["basis"], "refs": n["refs"], "ending": n["ending"],
                            "to": [c["to"] for c in n["choices"]]} for n in story["nodes"]},
        "text": texts,
        "audio": audio or {},
        "sources": pub_sources,
        "facts": facts,
        "warnings": story.get("_warnings", []),
    }
    (out_dir / "story.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=1))
    tpl = (Path(__file__).parent.parent / "player" / "player.html").read_text()
    html = tpl.replace("/*__STORY__*/null", json.dumps(bundle, ensure_ascii=False))
    (out_dir / "player.html").write_text(html)
    return out_dir / "player.html"


# =============================================================== mocks (offline dev)
def _mock_facts(sources):
    return {
        "people": [], "places": [],
        "events": [{"when": "", "what": seg["text"][:120], "refs": [seg["id"]],
                    "basis": s["kind"]} for s in sources for seg in s["segments"][:2]],
        "disagreements": [{"topic": "Who built it?", "versions": [
            {"claim": s["segments"][0]["text"][:100], "refs": [s["segments"][0]["id"]],
             "basis": s["kind"]} for s in sources if s["segments"]]}],
    }


def _mock_story(sources, facts, lang):
    """Deterministic graph: intro -> one branch per source -> shared ending(s)."""
    nodes = [{"id": "n1", "title": "[mock] The question", "basis": "dramatized", "refs": [],
              "narration": "[mock] Long ago, in a dry year, a village built something that "
                           "still stands. But who built it? Each source remembers it differently.",
              "choices": [], "ending": False}]
    for i, s in enumerate(sources, 2):
        segs = s["segments"][:3]
        nodes[0]["choices"].append({"label": f"[mock] Follow {s['title']}", "to": f"n{i}"})
        nodes.append({"id": f"n{i}", "title": f"[mock] {s['title']}",
                      "basis": s["kind"], "refs": [x["id"] for x in segs],
                      "narration": " ".join(x["text"] for x in segs)[:NARRATION_CAP],
                      "choices": [{"label": "[mock] Weigh the versions", "to": "end1"},
                                  {"label": "[mock] Hear another account", "to": "n1"}],
                      "ending": False})
    nodes.append({"id": "end1", "title": "[mock] Many hands", "basis": "mixed",
                  "refs": [s["segments"][0]["id"] for s in sources if s["segments"]],
                  "narration": "[mock] The records name the powerful; memory names the "
                               "people who carried water. Both are part of the story.",
                  "choices": [], "ending": True})
    return {"title": "[mock] Heritage story", "logline": "[mock] Offline test story",
            "start": "n1", "nodes": nodes}
