"""The answer loop: classify, look up, recall, draft, guardrails.

Automation before AI. The LLM is only called for questions that need judgment
(free_text, numeric_by_year). Exact fields and document checks are deterministic.
The guardrails run after the model and have the final say.
"""

import logging
import re
from datetime import datetime

from . import db
from . import llm
from . import memory as mem
from .ingest import load_cache

log = logging.getLogger("prequal.agent")

CONFIDENCE_AUTO = 0.7
EVIDENCE_FOR_MODEL = 8

# Terms that, if asserted positively in a free-text draft, must be backed by a document on file.
CLAIM_GUARDS = [
    (re.compile(r"\biso\s*9001\b", re.I), "iso_9001", "ISO 9001"),
    (re.compile(r"\b(ehs|hse)\s+policy\b", re.I), "ehs_policy", "EHS policy"),
    (re.compile(r"\b(udyam|msme)\b", re.I), "udyam_certificate", "Udyam/MSME registration"),
    (re.compile(r"\bquality\s+manual\b", re.I), "quality_manual", "quality manual"),
]
NEGATION = re.compile(r"\b(not|no|never|without|neither|nor|don't|do not|does not|are not|is not)\b", re.I)


def _company_name(cache: dict) -> str:
    return (cache.get("company") or {}).get("name") or "the company"


# ---------------------------------------------------------------------------
# Deterministic answers
# ---------------------------------------------------------------------------

def answer_field(q: dict, cache: dict) -> dict | None:
    fields = cache.get("fields") or {}
    values = fields.get("values") or {}
    key = q.get("field")
    if not key or not values:
        return None
    if key not in values:
        return None
    value = values.get(key)
    if value is None:
        canned = (fields.get("null_answers") or {}).get(key)
        if canned:
            return dict(draft_text=canned, status="auto", confidence=1.0, source="lookup",
                        evidence={"items": [], "timeline": []}, agent_note=f"exact field '{key}' is not applicable; canned answer")
        return dict(draft_text=None, status="gap", confidence=None, source="lookup",
                    evidence={"items": [], "timeline": []}, agent_note=f"field '{key}' has no value on file")
    return dict(draft_text=str(value), status="auto", confidence=1.0, source="lookup",
                evidence={"items": [], "timeline": []}, agent_note=f"exact match on field '{key}', no model call")


def answer_document(q: dict, cache: dict, m) -> dict | None:
    docs = cache.get("documents") or {}
    key = q.get("document_key")
    if not key or not docs:
        return None
    doc = docs.get(key)
    if not doc:
        return None
    # Pull the document memory as evidence so the UI shows where the answer came from.
    items = []
    try:
        # Each document memory carries its key as a tag, so this recall is exact.
        items = m.recall(f"{doc['name']} document", tags=[key], budget="low", max_tokens=600)[:3]
    except Exception as exc:
        log.warning("recall for document evidence failed: %s", exc)
    is_yes_no = q.get("type") == "yes_no"
    if doc["status"] == "on_file":
        extra = []
        if doc.get("issued"):
            extra.append(f"issued {doc['issued']}")
        if doc.get("valid_until"):
            extra.append(f"valid until {doc['valid_until']}")
        detail = f" ({', '.join(extra)})" if extra else ""
        text = (f"Yes. {doc['name']} is attached{detail}." if is_yes_no
                else f"Attached: {doc['name']}{detail}. File: {doc.get('file')}.")
        return dict(draft_text=text, status="auto", confidence=1.0, source="document",
                    evidence={"items": items, "timeline": []}, agent_note=f"document '{key}' is on file")
    text = (f"No. We do not hold a {doc['name']} at present." if is_yes_no
            else f"Not available. We do not hold a {doc['name']}.")
    return dict(draft_text=text, status="gap", confidence=1.0, source="document",
                evidence={"items": items, "timeline": []},
                agent_note=f"'{doc['name']}' is not on file. The agent will not claim it. Decide whether to answer No, or obtain the document.")


# ---------------------------------------------------------------------------
# Memory-backed answers
# ---------------------------------------------------------------------------

def _timeline(items: list[dict]) -> list[dict]:
    dated = [e for e in items if e.get("occurred_start")]
    dated.sort(key=lambda e: e["occurred_start"], reverse=True)
    out = []
    for e in dated:
        try:
            when = datetime.fromisoformat(e["occurred_start"].replace("Z", "+00:00"))
            label = when.strftime("%b %Y")
        except Exception:
            label = e["occurred_start"][:10]
        out.append({"when": e["occurred_start"][:10], "label": label, "text": e["text"], "id": e["id"], "type": e.get("type")})
    return out


def _recall_for(q: dict, m, company: str) -> list[dict]:
    """One bank = one company, so the question itself is the query; no company-name prefix (it only adds BM25 noise)."""
    kwargs = dict(budget="mid", max_tokens=1500)
    query = q["text"]
    if q.get("type") == "numeric_by_year":
        # World facts only: observations may consolidate the years into one belief and hide the timeline.
        # Financial facts carry the 'financial' tag at ingest, so scope to them first; fall back if that is thin.
        hits = m.recall(query, types=["world"], tags=["financial"], **kwargs)
        if len(hits) >= 2:
            return hits
        return m.recall(query, types=["world"], tags=None, **kwargs)
    return m.recall(query, types=["world", "experience", "observation"], **kwargs)


def apply_guardrails(q: dict, draft: llm.DraftResult, evidence: list[dict], cache: dict) -> dict:
    ev_ids = {e["id"] for e in evidence}
    if draft.tool == "flag_gap":
        return dict(status="gap", confidence=None, agent_note=f"model flagged a gap: {draft.reason}")
    if draft.tool == "none":
        return dict(status="needs_review", confidence=None,
                    agent_note=f"model did not return a valid tool call ({draft.reason}). Raw: {(draft.raw or '')[:160]}")
    notes = []
    status = "auto"
    conf = draft.confidence if draft.confidence is not None else 0.0
    cited = [i for i in draft.evidence_ids if i in ev_ids]
    if not cited:
        status = "needs_review"
        notes.append("draft cites no recalled evidence")
    if conf < CONFIDENCE_AUTO:
        status = "needs_review"
        notes.append(f"confidence {conf:.2f} below {CONFIDENCE_AUTO}")
    # Positive claims about certifications the company does not hold.
    docs = cache.get("documents") or {}
    text = draft.text or ""
    for pattern, key, label in CLAIM_GUARDS:
        if pattern.search(text) and not NEGATION.search(text):
            doc = docs.get(key)
            if doc and doc["status"] != "on_file":
                status = "needs_review"
                notes.append(f"draft mentions {label} positively but it is not on file")
    if status == "auto":
        notes.append(f"confidence {conf:.2f}, {len(cited)} evidence items cited")
    return dict(status=status, confidence=conf, agent_note="; ".join(notes), cited=cited)


def answer_with_memory(q: dict, cache: dict, m) -> dict:
    try:
        evidence = _recall_for(q, m, _company_name(cache))
    except Exception as exc:
        return dict(draft_text=None, status="needs_review", confidence=None, source="memory",
                    evidence={"items": [], "timeline": []}, agent_note=f"memory unavailable during recall: {type(exc).__name__}")
    # Recall can return 30+ items. The timeline uses every dated fact; the model sees only the top few,
    # which keeps prompts small (Groq free-tier token limits) and the draft focused.
    timeline = _timeline(evidence) if q.get("type") == "numeric_by_year" else []
    evidence = evidence[:EVIDENCE_FOR_MODEL]
    if not evidence:
        return dict(draft_text=None, status="gap", confidence=None, source="memory",
                    evidence={"items": [], "timeline": []}, agent_note="nothing in memory answers this question")
    draft = llm.draft(q, evidence)
    verdict = apply_guardrails(q, draft, evidence, cache)
    return dict(
        draft_text=draft.text,
        status=verdict["status"],
        confidence=verdict.get("confidence"),
        source=f"memory+{draft.model or 'model'}",
        evidence={"items": evidence, "timeline": timeline, "cited": verdict.get("cited", [])},
        agent_note=verdict["agent_note"],
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def answer_question(q: dict, cache: dict | None = None) -> dict:
    cache = cache if cache is not None else load_cache()
    m = mem.get_memory()
    m.ensure_bank(_company_name(cache))
    qtype = q.get("type", "free_text")

    if not cache.get("company"):
        # Fresh agent: no ingest has happened. Nothing deterministic to answer from, memory is empty.
        result = answer_with_memory(q, cache, m)
        if result["status"] == "gap" and not result["evidence"]["items"]:
            result["status"] = "needs_review"
            result["agent_note"] = "no company memory yet; ingest the company profile first"
        return result

    if qtype == "field":
        r = answer_field(q, cache)
        if r:
            return r
    if qtype in ("document", "yes_no"):
        r = answer_document(q, cache, m)
        if r:
            return r
    return answer_with_memory(q, cache, m)


def run_questionnaire(qid: int):
    """Generator: answers every non-approved question in order and yields progress dicts."""
    qn = db.get_questionnaire(qid)
    if not qn:
        yield {"event": "error", "message": "questionnaire not found"}
        return
    cache = load_cache()
    db.set_status(qid, "running")
    todo = [q for q in qn["questions"] if q["status"] != "approved"]
    total = len(todo)
    counts = {"auto": 0, "needs_review": 0, "gap": 0}
    for i, q in enumerate(todo, 1):
        q = dict(q, client=qn["client"], client_type=qn.get("client_type"), questionnaire_title=qn["title"])
        try:
            r = answer_question(q, cache)
        except Exception as exc:
            log.exception("answering question %s failed", q["id"])
            r = dict(draft_text=None, status="needs_review", confidence=None, source="error",
                     evidence={"items": [], "timeline": []}, agent_note=f"unexpected error: {type(exc).__name__}: {exc}")
        db.write_draft(q["id"], **r)
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        yield {"event": "answer", "i": i, "total": total, "question_id": q["id"], "ordinal": q["ordinal"],
               "status": r["status"], "confidence": r.get("confidence"), "source": r.get("source")}
    db.set_status(qid, "reviewed" if total == 0 else "drafted")
    yield {"event": "done", "total": total, "counts": counts}


def brief_for(qid: int) -> str:
    qn = db.get_questionnaire(qid)
    if not qn:
        raise ValueError("questionnaire not found")
    cache = load_cache()
    m = mem.get_memory()
    m.ensure_bank(_company_name(cache))
    text = m.reflect(
        f"Summarise what {_company_name(cache)} should know before answering a {qn.get('client_type') or 'client'} "
        f"questionnaire titled '{qn['title']}' with scope: {qn.get('scope') or 'not stated'}. "
        "Cover: relevant past projects and clients, documents on file and documents missing, "
        "and any answers given on earlier questionnaires that apply. Be specific and short.",
        budget="low",
        context="preparing to answer a vendor pre-qualification questionnaire",
    )
    db.set_status(qid, qn["status"], brief_text=text)
    return text


def retain_review(question: dict, final_text: str, reason: str | None) -> None:
    """The learning loop: a reviewer's correction becomes a memory."""
    m = mem.get_memory()
    content = (f"Reviewer-approved answer on the {question['client']} questionnaire ({question['questionnaire_title']}) "
               f"to the question \"{question['text']}\": {final_text}")
    if reason:
        content += f" Reviewer's note: {reason}"
    payload = dict(content=content, context=f"reviewer correction for {question['client']}",
                   tags=["review", f"pq:{question.get('questionnaire_slug') or 'unknown'}"])
    try:
        m.retain(content, context=payload["context"], timestamp=datetime.utcnow(), tags=payload["tags"])
    except Exception as exc:
        log.warning("retain failed, queued for retry: %s", exc)
        db.queue_retain(payload)


def flush_pending_retains() -> int:
    items = db.pop_pending_retains()
    if not items:
        return 0
    m = mem.get_memory()
    done = 0
    for p in items:
        try:
            m.retain(p["content"], context=p.get("context"), timestamp=datetime.utcnow(), tags=p.get("tags"))
            done += 1
        except Exception:
            db.queue_retain(p)
    return done
