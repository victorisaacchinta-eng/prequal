"""Hindsight wrapper: the only place the app talks to memory.

Real mode (default): hindsight-client against Hindsight Cloud or a local Docker instance.
Mock mode (PREQUAL_MOCK=1): an in-process store with keyword scoring, so the whole app
runs offline for development and UI work. The mock is deliberately dumb; do not demo with it.

Every call is logged to an in-memory ring buffer that the UI's memory log panel reads.
"""

import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("prequal.memory")

MOCK = os.getenv("PREQUAL_MOCK", "0") == "1"
BASE_URL = os.getenv("HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io")
API_KEY = os.getenv("HINDSIGHT_API_KEY")
BANK_ID = os.getenv("HINDSIGHT_BANK_ID", "northstar-interiors")

MISSION = (
    "You are the institutional memory of {company}, an interior fit-out contractor in Hyderabad. "
    "You know every fact about the company, every document it holds, every answer it has given on past "
    "questionnaires, and how those facts changed over time. You exist so the company never answers the "
    "same question twice."
)
DIRECTIVES = [
    ("no-fabrication", "Never state a certification, registration, licence or figure that is not in memory. If it is absent, say it is absent.", 1),
    ("timeline", "When a fact has more than one value over time, report the timeline, most recent first, and name the dates.", 2),
    ("reviewer-wins", "Prefer the reviewer's corrected answers over the agent's own earlier drafts.", 3),
]
DISPOSITION = {"skepticism": 4, "literalism": 4, "empathy": 2}


class MemoryEvent(dict):
    pass


_events: deque = deque(maxlen=200)
_events_lock = threading.Lock()


def _log_event(op: str, detail: str, ms: float, ok: bool = True, extra: dict | None = None) -> None:
    ev = MemoryEvent(
        id=uuid.uuid4().hex[:8],
        at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        op=op,
        detail=detail[:220],
        ms=round(ms, 1),
        ok=ok,
        **(extra or {}),
    )
    with _events_lock:
        _events.append(ev)


def events(limit: int = 50) -> list[dict]:
    with _events_lock:
        return list(_events)[-limit:][::-1]


def _norm(r) -> dict:
    """Turn an SDK result object (or dict) into the flat shape the UI and agent use."""
    g = (lambda k, d=None: r.get(k, d)) if isinstance(r, dict) else (lambda k, d=None: getattr(r, k, d))
    occurred_start = g("occurred_start")
    occurred_end = g("occurred_end")
    return {
        "id": str(g("id") or uuid.uuid4().hex[:12]),
        "text": g("text") or "",
        "type": g("type") or "world",
        "context": g("context"),
        "occurred_start": str(occurred_start) if occurred_start else None,
        "occurred_end": str(occurred_end) if occurred_end else None,
        "entities": list(g("entities") or []),
        "tags": list(g("tags") or []),
    }


# =============================================================================
# Real client
# =============================================================================

class HindsightMemory:
    def __init__(self) -> None:
        from hindsight_client import Hindsight  # imported lazily so mock mode needs no install

        self.client = Hindsight(base_url=BASE_URL, api_key=API_KEY, timeout=60.0)
        self.bank_id = BANK_ID
        self._bank_ready = False

    # ---- bank ----
    def ensure_bank(self, company_name: str) -> None:
        if self._bank_ready:
            return
        t0 = time.time()
        try:
            self.client.create_bank(
                bank_id=self.bank_id,
                name=f"Prequal: {company_name}",
                mission=MISSION.format(company=company_name),
                disposition=DISPOSITION,
            )
            _log_event("create_bank", f"bank {self.bank_id} created", (time.time() - t0) * 1000)
        except Exception as exc:  # exists already, or SDK differences; not fatal
            _log_event("create_bank", f"bank {self.bank_id}: {type(exc).__name__} (probably exists)", (time.time() - t0) * 1000)
        self._ensure_directives()
        self._bank_ready = True

    def _ensure_directives(self) -> None:
        """Bank directives (hindsight-client >= 0.10: create_directive / list_directives). Idempotent by name."""
        existing = set()
        try:
            listed = self.client.list_directives(bank_id=self.bank_id)
            items = getattr(listed, "directives", None) or getattr(listed, "items", None) or listed or []
            existing = {getattr(d, "name", None) or (d.get("name") if isinstance(d, dict) else None) for d in items}
        except Exception as exc:
            log.debug("list_directives failed: %s", exc)
        for name, content, priority in DIRECTIVES:
            t0 = time.time()
            if name in existing:
                _log_event("directive", f"{name}: already set", (time.time() - t0) * 1000)
                continue
            try:
                self.client.create_directive(bank_id=self.bank_id, name=name, content=content, priority=priority)
                _log_event("directive", f"{name}: set", (time.time() - t0) * 1000)
            except Exception as exc:
                log.warning("directive %s not set: %s", name, exc)
                _log_event("directive", f"{name}: skipped ({type(exc).__name__})", (time.time() - t0) * 1000, ok=False)

    # ---- retain ----
    def retain(self, content: str, *, context: str | None = None, timestamp: datetime | None = None,
               tags: list[str] | None = None, document_id: str | None = None) -> dict:
        t0 = time.time()
        kwargs = dict(bank_id=self.bank_id, content=content, retain_async=False)
        if context:
            kwargs["context"] = context
        if timestamp:
            kwargs["timestamp"] = timestamp
        if document_id:
            kwargs["document_id"] = document_id
        try:
            try:
                self.client.retain(**kwargs, tags=tags or [])
            except TypeError:  # older SDK without tags
                self.client.retain(**kwargs)
            _log_event("retain", content, (time.time() - t0) * 1000, extra={"tags": tags or []})
            return {"ok": True}
        except Exception as exc:
            _log_event("retain", f"FAILED {type(exc).__name__}: {content}", (time.time() - t0) * 1000, ok=False)
            raise

    def retain_batch(self, items: list[dict], document_id: str | None = None) -> dict:
        """items: [{content, context?, timestamp?, tags?}]. Falls back to one-by-one if batch is unavailable."""
        t0 = time.time()
        fn = getattr(self.client, "retain_batch", None)
        if fn:
            payload = []
            for it in items:
                row = {"content": it["content"]}
                if it.get("context"):
                    row["context"] = it["context"]
                if it.get("timestamp"):
                    row["timestamp"] = it["timestamp"]
                if it.get("tags"):
                    row["tags"] = it["tags"]
                payload.append(row)
            try:
                kwargs = dict(bank_id=self.bank_id, items=payload, retain_async=False)
                if document_id:
                    kwargs["document_id"] = document_id
                fn(**kwargs)
                _log_event("retain_batch", f"{len(items)} items", (time.time() - t0) * 1000)
                return {"ok": True, "count": len(items)}
            except Exception as exc:
                log.warning("retain_batch failed (%s); falling back to single retains", exc)
        n = 0
        for it in items:
            self.retain(it["content"], context=it.get("context"), timestamp=it.get("timestamp"), tags=it.get("tags"), document_id=document_id)
            n += 1
        return {"ok": True, "count": n}

    # ---- recall ----
    def recall(self, query: str, *, types: list[str] | None = None, tags: list[str] | None = None,
               budget: str = "mid", max_tokens: int = 1500) -> list[dict]:
        t0 = time.time()
        kwargs = dict(bank_id=self.bank_id, query=query, budget=budget, max_tokens=max_tokens)
        if types:
            kwargs["types"] = types
        try:
            try:
                res = self.client.recall(**kwargs, tags=tags) if tags else self.client.recall(**kwargs)
            except TypeError:
                res = self.client.recall(**kwargs)
            raw = getattr(res, "results", None) or (res.get("results") if isinstance(res, dict) else []) or []
            out = [_norm(r) for r in raw]
            _log_event("recall", query, (time.time() - t0) * 1000, extra={"hits": len(out)})
            return out
        except Exception as exc:
            _log_event("recall", f"FAILED {type(exc).__name__}: {query}", (time.time() - t0) * 1000, ok=False)
            raise

    # ---- reflect ----
    def reflect(self, query: str, *, budget: str = "low", context: str | None = None) -> str:
        t0 = time.time()
        kwargs = dict(bank_id=self.bank_id, query=query, budget=budget)
        if context:
            kwargs["context"] = context
        try:
            res = self.client.reflect(**kwargs)
            text = getattr(res, "text", None) or (res.get("text") if isinstance(res, dict) else "") or ""
            _log_event("reflect", query, (time.time() - t0) * 1000)
            return text
        except Exception as exc:
            _log_event("reflect", f"FAILED {type(exc).__name__}: {query}", (time.time() - t0) * 1000, ok=False)
            raise

    # ---- reset ----
    def reset(self) -> dict:
        """Clear every memory in the bank (the 'fresh agent' demo state). Bank config and directives stay."""
        t0 = time.time()
        try:
            res = self.client.memory.clear_bank_memories(bank_id=self.bank_id)
            deleted = getattr(res, "deleted_count", None)
            _log_event("reset", f"bank {self.bank_id} cleared ({deleted if deleted is not None else '?'} memories)", (time.time() - t0) * 1000)
            return {"ok": True, "deleted": deleted}
        except Exception as exc:
            _log_event("reset", f"FAILED {type(exc).__name__}", (time.time() - t0) * 1000, ok=False)
            raise


# =============================================================================
# Mock (offline development only)
# =============================================================================

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "with", "your", "you", "do", "does",
         "is", "are", "any", "if", "by", "at", "as", "be", "it", "its", "this", "that", "which", "what",
         "how", "many", "give", "details", "describe", "list", "attach", "please", "state", "name", "number"}


def _tokens(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if w not in _STOP and len(w) > 2}


class MockMemory:
    def __init__(self) -> None:
        self.bank_id = BANK_ID + "-mock"
        self.facts: list[dict] = []
        self._bank_ready = False

    def ensure_bank(self, company_name: str) -> None:
        if not self._bank_ready:
            _log_event("create_bank", f"[mock] bank {self.bank_id}", 0.2)
            for name, _, _ in DIRECTIVES:
                _log_event("directive", f"[mock] {name}: set", 0.1)
            self._bank_ready = True

    def retain(self, content, *, context=None, timestamp=None, tags=None, document_id=None):
        t0 = time.time()
        ftype = "experience" if (tags and ("review" in tags or any(t.startswith("pq:") for t in tags))) else "world"
        self.facts.append({
            "id": uuid.uuid4().hex[:12], "text": content, "type": ftype, "context": context,
            "occurred_start": timestamp.isoformat() if timestamp else None,
            "occurred_end": timestamp.isoformat() if timestamp else None,
            "entities": [], "tags": tags or [], "_tok": _tokens(content + " " + (context or "")),
        })
        _log_event("retain", "[mock] " + content, (time.time() - t0) * 1000, extra={"tags": tags or []})
        return {"ok": True}

    def retain_batch(self, items, document_id=None):
        for it in items:
            self.retain(it["content"], context=it.get("context"), timestamp=it.get("timestamp"), tags=it.get("tags"))
        return {"ok": True, "count": len(items)}

    def recall(self, query, *, types=None, tags=None, budget="mid", max_tokens=1500):
        t0 = time.time()
        q = _tokens(query)
        n = len(self.facts) or 1
        df = {}
        for f in self.facts:
            for t in f["_tok"]:
                df[t] = df.get(t, 0) + 1
        # Tokens that appear in most facts (the company name, "lakh") carry almost no signal.
        weight = {t: (0.1 if df.get(t, 0) / n > 0.4 else 1.0) for t in q}
        denom = sum(weight.values()) or 1
        scored = []
        for i, f in enumerate(self.facts):
            if types and f["type"] not in types:
                continue
            if tags and not set(tags) & set(f["tags"]):
                continue
            score = sum(weight[t] for t in q & f["_tok"]) / denom
            if score > 0:
                if "review" in f["tags"]:
                    score *= 1.5  # crude stand-in for the real bank's "reviewer wins" directive
                scored.append((score, -i, f))  # newer facts win ties
        scored.sort(key=lambda x: (-x[0], x[1]))
        out = [dict((k, v) for k, v in f.items() if k != "_tok") for s, _, f in scored[:8] if s >= 0.15]
        _log_event("recall", "[mock] " + query, (time.time() - t0) * 1000, extra={"hits": len(out)})
        return out

    def reflect(self, query, *, budget="low", context=None):
        hits = self.recall(query, budget=budget)
        _log_event("reflect", "[mock] " + query, 0.5)
        if not hits:
            return "Nothing in memory yet for this company."
        return "From memory: " + " ".join(h["text"] for h in hits[:5])

    def reset(self):
        self.facts.clear()
        _log_event("reset", f"[mock] bank {self.bank_id} cleared", 0.1)
        return {"ok": True}


# =============================================================================

_memory = None
_memory_lock = threading.Lock()


def get_memory():
    global _memory
    with _memory_lock:
        if _memory is None:
            if MOCK:
                log.warning("PREQUAL_MOCK=1: using the in-process mock memory. Do not demo with this.")
                _memory = MockMemory()
            else:
                if not API_KEY and "localhost" not in BASE_URL and "127.0.0.1" not in BASE_URL:
                    raise RuntimeError("HINDSIGHT_API_KEY is not set. Set it in .env, or set PREQUAL_MOCK=1 for offline development.")
                _memory = HindsightMemory()
        return _memory
