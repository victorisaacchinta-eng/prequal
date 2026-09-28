"""Prequal API and static frontend.

    uvicorn backend.main:app --reload --port 8000
"""

import csv
import io
import json
import logging
import os
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()  # must run before memory/llm read their env vars

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool  # noqa: E402

from . import agent, db  # noqa: E402
from . import memory as mem  # noqa: E402
from .ingest import DATA_DIR, clear_cache, load_cache, run_ingest  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("prequal")

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = FastAPI(title="Prequal", version="0.1.0")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    log.info("mock mode: %s", mem.MOCK)


@app.get("/")
def landing():
    return FileResponse(os.path.join(FRONTEND_DIR, "landing.html"))


@app.get("/app")
def review_screen():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/api/health")
def health():
    cache = load_cache()
    return {"ok": True, "mock": mem.MOCK, "bank_id": mem.BANK_ID, "ingested": bool(cache.get("company")),
            "company": cache.get("company"), "ingested_at": cache.get("ingested_at")}


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

@app.post("/api/ingest")
async def ingest():
    try:
        counts = await run_in_threadpool(run_ingest)
    except Exception as exc:
        log.exception("ingest failed")
        raise HTTPException(500, f"ingest failed: {type(exc).__name__}: {exc}")
    return {"ok": True, "counts": counts, "company": load_cache().get("company")}


@app.post("/api/memory/reset")
async def memory_reset():
    try:
        await run_in_threadpool(mem.get_memory().reset)
    except Exception as exc:
        raise HTTPException(500, f"reset failed: {type(exc).__name__}: {exc}")
    clear_cache()
    return {"ok": True}


@app.get("/api/memory/log")
def memory_log(limit: int = 50):
    return {"events": mem.events(limit)}


@app.get("/api/memory/status")
def memory_status():
    cache = load_cache()
    docs = cache.get("documents") or {}
    return {
        "ingested": bool(cache.get("company")),
        "company": cache.get("company"),
        "ingested_at": cache.get("ingested_at"),
        "fields": len((cache.get("fields") or {}).get("values") or {}),
        "documents_on_file": sum(1 for d in docs.values() if d["status"] == "on_file"),
        "documents_missing": sum(1 for d in docs.values() if d["status"] != "on_file"),
        "demo_update": cache.get("turnover_update_for_demo"),
        "mock": mem.MOCK,
    }


class RetainBody(BaseModel):
    content: str = Field(min_length=3, max_length=2000)
    context: str | None = None
    tags: list[str] = Field(default_factory=list)
    timestamp: str | None = None  # ISO date


@app.post("/api/memory/retain")
async def memory_retain(body: RetainBody):
    """Retain one fact live. Used in the demo to add a new financial year's turnover on stage."""
    ts = datetime.fromisoformat(body.timestamp) if body.timestamp else None
    try:
        await run_in_threadpool(lambda: mem.get_memory().retain(body.content, context=body.context, timestamp=ts, tags=body.tags))
    except Exception as exc:
        raise HTTPException(502, f"retain failed: {type(exc).__name__}: {exc}")
    return {"ok": True}


@app.post("/api/memory/flush")
async def memory_flush():
    n = await run_in_threadpool(agent.flush_pending_retains)
    return {"ok": True, "retried": n}


# ---------------------------------------------------------------------------
# Questionnaires
# ---------------------------------------------------------------------------

@app.get("/api/questionnaires")
def questionnaires():
    return {"questionnaires": db.list_questionnaires()}


class QuestionIn(BaseModel):
    ordinal: int
    text: str
    type: str = "free_text"
    section: str | None = None
    field: str | None = None
    document_key: str | None = None
    required_document: str | None = None


class QuestionnaireIn(BaseModel):
    slug: str | None = None
    client: str
    client_type: str | None = None
    title: str
    scope: str | None = None
    received_on: str | None = None
    due_on: str | None = None
    questions: list[QuestionIn]


@app.post("/api/questionnaires")
def create_questionnaire(body: QuestionnaireIn):
    data = body.model_dump()
    data["slug"] = data["slug"] or f"{body.client.lower().replace(' ', '-')}-{int(datetime.utcnow().timestamp())}"
    qid = db.upsert_questionnaire(data)
    return {"ok": True, "id": qid}


@app.post("/api/questionnaires/load-samples")
def load_samples():
    folder = os.path.join(DATA_DIR, "questionnaires")
    samples = []
    for name in sorted(os.listdir(folder)):
        if name.endswith(".json"):
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                samples.append(json.load(f))
    samples.sort(key=lambda d: d.get("received_on") or "")  # ids follow the story: Feb, Mar, Apr
    loaded = []
    for data in samples:
        qid = db.upsert_questionnaire(data)
        loaded.append({"id": qid, "slug": data.get("slug"), "client": data["client"], "questions": len(data["questions"])})
    return {"ok": True, "loaded": loaded}


@app.get("/api/questionnaires/{qid}")
def get_questionnaire(qid: int):
    qn = db.get_questionnaire(qid)
    if not qn:
        raise HTTPException(404, "questionnaire not found")
    return qn


@app.post("/api/questionnaires/{qid}/run")
async def run_questionnaire(qid: int):
    if not db.get_questionnaire(qid):
        raise HTTPException(404, "questionnaire not found")

    async def stream():
        async for ev in iterate_in_threadpool(agent.run_questionnaire(qid)):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/questionnaires/{qid}/reset")
def reset_questionnaire(qid: int):
    if not db.get_questionnaire(qid):
        raise HTTPException(404, "questionnaire not found")
    db.reset_answers(qid)
    return {"ok": True}


@app.post("/api/questionnaires/{qid}/brief")
async def brief(qid: int):
    try:
        text = await run_in_threadpool(agent.brief_for, qid)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"reflect failed: {type(exc).__name__}: {exc}")
    return {"ok": True, "brief": text}


@app.get("/api/questionnaires/{qid}/export")
def export(qid: int, format: str = "json"):
    qn = db.get_questionnaire(qid)
    if not qn:
        raise HTTPException(404, "questionnaire not found")
    rows = [{
        "ordinal": q["ordinal"], "section": q["section"], "question": q["text"],
        "answer": q["final_text"] or q["draft_text"] or "", "status": q["status"],
        "confidence": q["confidence"], "source": q["source"],
    } for q in qn["questions"]]
    fname = f"{qn['slug'] or qid}-answers"
    if format == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["ordinal"])
        w.writeheader()
        w.writerows(rows)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename={fname}.csv"})
    return JSONResponse({"questionnaire": {k: qn[k] for k in ("id", "slug", "client", "title", "received_on", "due_on")}, "answers": rows},
                        headers={"Content-Disposition": f"attachment; filename={fname}.json"})


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

class ReviewBody(BaseModel):
    action: str = Field(pattern="^(approve|edit|gap|reopen)$")
    final_text: str | None = None
    note: str | None = None


@app.patch("/api/answers/{answer_id}")
async def review(answer_id: int, body: ReviewBody):
    q = db.get_answer_by_id(answer_id)
    if not q:
        raise HTTPException(404, "answer not found")
    if body.action == "reopen":
        db.reopen_answer(answer_id)
        return {"ok": True, "status": "needs_review"}
    if body.action == "gap":
        db.review_answer(answer_id, final_text=body.final_text or "", status="gap", note=body.note or "marked as gap by reviewer")
        return {"ok": True, "status": "gap"}
    final_text = (body.final_text if body.final_text is not None else q["draft_text"]) or ""
    if not final_text.strip():
        raise HTTPException(400, "final_text is required to approve")
    edited = final_text.strip() != (q["draft_text"] or "").strip()
    db.review_answer(answer_id, final_text=final_text.strip(), status="approved",
                     note=("edited by reviewer" if edited else "approved as drafted") + (f": {body.note}" if body.note else ""))
    # Learning loop: every approval is retained; edits carry the reviewer's reason.
    await run_in_threadpool(agent.retain_review, q, final_text.strip(), body.note if edited or body.note else None)
    return {"ok": True, "status": "approved", "learned": True, "edited": edited}
