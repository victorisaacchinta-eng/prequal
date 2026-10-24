"""SQLite: questionnaires, questions, answers, review state, retain retry queue.

Company facts never live here. That is Hindsight's job (see memory.py).
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.getenv("PREQUAL_DB", os.path.join(os.path.dirname(__file__), "..", "prequal.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS questionnaires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE,
    client TEXT NOT NULL,
    client_type TEXT,
    title TEXT NOT NULL,
    scope TEXT,
    received_on TEXT,
    due_on TEXT,
    status TEXT NOT NULL DEFAULT 'loaded',
    brief_text TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    questionnaire_id INTEGER NOT NULL REFERENCES questionnaires(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    section TEXT,
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    field TEXT,
    document_key TEXT,
    required_document TEXT
);
CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL UNIQUE REFERENCES questions(id) ON DELETE CASCADE,
    draft_text TEXT,
    final_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    confidence REAL,
    source TEXT,
    evidence_json TEXT,
    agent_note TEXT,
    reviewed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_retain (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)


# ---------- questionnaires ----------

def upsert_questionnaire(q: dict) -> int:
    """Insert a questionnaire and its questions. Re-uploading the same slug replaces it."""
    with tx() as conn:
        existing = conn.execute("SELECT id FROM questionnaires WHERE slug = ?", (q.get("slug"),)).fetchone()
        if existing:
            conn.execute("DELETE FROM questionnaires WHERE id = ?", (existing["id"],))
        cur = conn.execute(
            """INSERT INTO questionnaires (slug, client, client_type, title, scope, received_on, due_on, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'loaded', ?)""",
            (q.get("slug"), q["client"], q.get("client_type"), q["title"], q.get("scope"),
             q.get("received_on"), q.get("due_on"), now_iso()),
        )
        qid = cur.lastrowid
        for item in q["questions"]:
            qc = conn.execute(
                """INSERT INTO questions (questionnaire_id, ordinal, section, type, text, field, document_key, required_document)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (qid, item["ordinal"], item.get("section"), item.get("type", "free_text"), item["text"],
                 item.get("field"), item.get("document_key"), item.get("required_document")),
            )
            conn.execute(
                "INSERT INTO answers (question_id, status, updated_at) VALUES (?, 'pending', ?)",
                (qc.lastrowid, now_iso()),
            )
        return qid


def list_questionnaires() -> list[dict]:
    with tx() as conn:
        rows = conn.execute(
            """SELECT q.*,
                      (SELECT COUNT(*) FROM questions x WHERE x.questionnaire_id = q.id) AS question_count,
                      (SELECT COUNT(*) FROM answers a JOIN questions x ON a.question_id = x.id
                        WHERE x.questionnaire_id = q.id AND a.status = 'auto') AS auto_count,
                      (SELECT COUNT(*) FROM answers a JOIN questions x ON a.question_id = x.id
                        WHERE x.questionnaire_id = q.id AND a.status = 'needs_review') AS review_count,
                      (SELECT COUNT(*) FROM answers a JOIN questions x ON a.question_id = x.id
                        WHERE x.questionnaire_id = q.id AND a.status = 'gap') AS gap_count,
                      (SELECT COUNT(*) FROM answers a JOIN questions x ON a.question_id = x.id
                        WHERE x.questionnaire_id = q.id AND a.status = 'approved') AS approved_count
               FROM questionnaires q ORDER BY q.received_on"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_questionnaire(qid: int) -> dict | None:
    with tx() as conn:
        q = conn.execute("SELECT * FROM questionnaires WHERE id = ?", (qid,)).fetchone()
        if not q:
            return None
        rows = conn.execute(
            """SELECT x.*, a.id AS answer_id, a.draft_text, a.final_text, a.status, a.confidence, a.source,
                      a.evidence_json, a.agent_note, a.reviewed_at, a.updated_at
               FROM questions x JOIN answers a ON a.question_id = x.id
               WHERE x.questionnaire_id = ? ORDER BY x.ordinal""",
            (qid,),
        ).fetchall()
        questions = []
        for r in rows:
            d = dict(r)
            d["evidence"] = _evidence(d.pop("evidence_json"))
            questions.append(d)
        out = dict(q)
        out["questions"] = questions
        return out


def get_question(question_id: int) -> dict | None:
    with tx() as conn:
        r = conn.execute(
            """SELECT x.*, a.id AS answer_id, a.draft_text, a.final_text, a.status, a.confidence, a.source,
                      a.evidence_json, a.agent_note, q.client, q.client_type, q.slug AS questionnaire_slug, q.title AS questionnaire_title
               FROM questions x JOIN answers a ON a.question_id = x.id
               JOIN questionnaires q ON q.id = x.questionnaire_id
               WHERE x.id = ?""",
            (question_id,),
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        d["evidence"] = _evidence(d.pop("evidence_json"))
        return d


def _evidence(raw: str | None) -> dict:
    """evidence_json holds {"items": [...recalled facts...], "timeline": [...]}; tolerate the older list shape."""
    if not raw:
        return {"items": [], "timeline": []}
    data = json.loads(raw)
    if isinstance(data, list):
        return {"items": data, "timeline": []}
    data.setdefault("items", [])
    data.setdefault("timeline", [])
    return data


def get_answer_by_id(answer_id: int) -> dict | None:
    with tx() as conn:
        r = conn.execute("SELECT question_id FROM answers WHERE id = ?", (answer_id,)).fetchone()
    return get_question(r["question_id"]) if r else None


def set_status(qid: int, status: str, brief_text: str | None = None) -> None:
    with tx() as conn:
        if brief_text is not None:
            conn.execute("UPDATE questionnaires SET status = ?, brief_text = ? WHERE id = ?", (status, brief_text, qid))
        else:
            conn.execute("UPDATE questionnaires SET status = ? WHERE id = ?", (status, qid))


# ---------- answers ----------

def write_draft(question_id: int, *, draft_text: str | None, status: str, confidence: float | None,
                source: str, evidence: dict, agent_note: str | None) -> None:
    with tx() as conn:
        conn.execute(
            """UPDATE answers SET draft_text = ?, final_text = NULL, status = ?, confidence = ?, source = ?,
                      evidence_json = ?, agent_note = ?, reviewed_at = NULL, updated_at = ?
               WHERE question_id = ?""",
            (draft_text, status, confidence, source, json.dumps(evidence), agent_note, now_iso(), question_id),
        )


def review_answer(answer_id: int, *, final_text: str, status: str, note: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            """UPDATE answers SET final_text = ?, status = ?, agent_note = COALESCE(?, agent_note),
                      reviewed_at = ?, updated_at = ? WHERE id = ?""",
            (final_text, status, note, now_iso(), now_iso(), answer_id),
        )


def reopen_answer(answer_id: int) -> None:
    """Put an answer back to 'pending' so the next run re-drafts it (keeps nothing from the old draft)."""
    with tx() as conn:
        conn.execute(
            """UPDATE answers SET draft_text = NULL, final_text = NULL, status = 'pending', confidence = NULL,
                      source = NULL, evidence_json = NULL, agent_note = 'reopened by reviewer', reviewed_at = NULL, updated_at = ?
               WHERE id = ?""",
            (now_iso(), answer_id),
        )


def reset_answers(qid: int) -> None:
    with tx() as conn:
        conn.execute(
            """UPDATE answers SET draft_text = NULL, final_text = NULL, status = 'pending', confidence = NULL,
                      source = NULL, evidence_json = NULL, agent_note = NULL, reviewed_at = NULL, updated_at = ?
               WHERE question_id IN (SELECT id FROM questions WHERE questionnaire_id = ?)""",
            (now_iso(), qid),
        )
        conn.execute("UPDATE questionnaires SET status = 'loaded', brief_text = NULL WHERE id = ?", (qid,))


# ---------- retain retry queue ----------

def queue_retain(payload: dict) -> None:
    with tx() as conn:
        conn.execute("INSERT INTO pending_retain (payload_json, created_at) VALUES (?, ?)", (json.dumps(payload), now_iso()))


def pop_pending_retains() -> list[dict]:
    with tx() as conn:
        rows = conn.execute("SELECT id, payload_json FROM pending_retain ORDER BY id").fetchall()
        conn.execute("DELETE FROM pending_retain")
        return [json.loads(r["payload_json"]) for r in rows]
