"""Groq drafting with tool calling, strict argument parsing, retries and model fallback.

The model never answers in free text. It must call one of two tools:
    submit_answer(text, confidence, evidence_ids)
    flag_gap(reason)
Anything else is treated as "needs review", never as an answer. The guardrails in agent.py
have the final say regardless of what the model returns.

Mock mode (PREQUAL_MOCK=1) uses a rule-based drafter so the app runs offline.
"""

import json
import logging
import os
import re
import time
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger("prequal.llm")

MOCK = os.getenv("PREQUAL_MOCK", "0") == "1"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PRIMARY_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "qwen/qwen3-32b")

SYSTEM_PROMPT = """You draft answers to vendor pre-qualification questionnaires on behalf of a small interior contractor.
You are given ONE question and a list of EVIDENCE items recalled from the company's memory, each with an id.

Rules, in priority order:
1. Answer only from the evidence. Never add a certification, registration, licence, client, figure or capability that the evidence does not state.
2. If the evidence does not answer the question, call flag_gap with a one-line reason. Do not guess.
3. Prefer evidence marked as a reviewer correction or a past answer over general facts.
4. When a figure has several dated values, list them most recent first with the period named.
5. Write in the first person plural ("We ..."), plain and specific, 1 to 4 sentences. No marketing language.
6. Always respond by calling exactly one tool. Never reply in plain text.
Set confidence between 0 and 1: 0.9+ when the evidence answers the question directly, 0.6 to 0.8 when you had to combine or infer, below 0.6 when the evidence is thin."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_answer",
            "description": "Submit a drafted answer grounded in the evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The answer text, 1 to 4 sentences."},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}, "description": "Ids of the evidence items used."},
                },
                "required": ["text", "confidence", "evidence_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_gap",
            "description": "Declare that the evidence does not answer the question.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]


class SubmitAnswer(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("empty answer")
        return v


class FlagGap(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class DraftResult(BaseModel):
    tool: Literal["submit_answer", "flag_gap", "none"]
    text: str | None = None
    confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str | None = None
    model: str | None = None
    attempts: int = 0
    raw: str | None = None


def _evidence_block(evidence: list[dict]) -> str:
    if not evidence:
        return "(no evidence recalled)"
    lines = []
    for e in evidence:
        when = f" [{e['occurred_start'][:10]}]" if e.get("occurred_start") else ""
        kind = e.get("type", "world")
        tags = ",".join(e.get("tags") or [])
        lines.append(f"- id={e['id']} type={kind}{when}{' tags=' + tags if tags else ''}: {e['text']}")
    return "\n".join(lines)


def _user_prompt(question: dict, evidence: list[dict]) -> str:
    return (
        f"Client: {question.get('client')} ({question.get('client_type') or 'unknown type'})\n"
        f"Questionnaire: {question.get('questionnaire_title')}\n"
        f"Section: {question.get('section')}\n"
        f"Question type: {question.get('type')}\n"
        f"QUESTION: {question['text']}\n\n"
        f"EVIDENCE:\n{_evidence_block(evidence)}\n\n"
        "Call submit_answer or flag_gap."
    )


def _parse_tool_call(name: str, arguments: str) -> DraftResult:
    data = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    if name == "submit_answer":
        a = SubmitAnswer.model_validate(data)
        return DraftResult(tool="submit_answer", text=a.text, confidence=a.confidence, evidence_ids=a.evidence_ids)
    if name == "flag_gap":
        g = FlagGap.model_validate(data)
        return DraftResult(tool="flag_gap", reason=g.reason)
    raise ValueError(f"unknown tool {name}")


# =============================================================================
# Groq
# =============================================================================

_groq_client = None


def _groq():
    global _groq_client
    if _groq_client is None:
        from groq import Groq

        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY is not set. Set it in .env, or set PREQUAL_MOCK=1.")
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


def _call_model(model: str, messages: list[dict]):
    """One chat completion with backoff on rate limits and server errors."""
    from groq import APIConnectionError, APIStatusError, RateLimitError

    delay = 1.5
    last_exc = None
    for attempt in range(3):
        try:
            return _groq().chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="required",
                temperature=0,
                max_tokens=600,
            )
        except RateLimitError as exc:
            last_exc = exc
            log.warning("%s rate limited (attempt %d)", model, attempt + 1)
        except APIStatusError as exc:
            last_exc = exc
            if exc.status_code < 500:
                raise
            log.warning("%s server error %s (attempt %d)", model, exc.status_code, attempt + 1)
        except APIConnectionError as exc:
            last_exc = exc
            log.warning("%s connection error (attempt %d)", model, attempt + 1)
        time.sleep(delay)
        delay *= 2
    raise last_exc


def draft_with_groq(question: dict, evidence: list[dict]) -> DraftResult:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(question, evidence)},
    ]
    attempts = 0
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        schema_retry_done = False
        while True:
            attempts += 1
            try:
                resp = _call_model(model, messages)
            except Exception as exc:
                log.error("model %s failed: %s", model, exc)
                break  # try the fallback model
            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []
            if not calls:
                # Plain text instead of a tool call. Never an answer.
                return DraftResult(tool="none", model=model, attempts=attempts, raw=(msg.content or "")[:500],
                                   reason="model replied in text instead of calling a tool")
            call = calls[0]
            try:
                out = _parse_tool_call(call.function.name, call.function.arguments)
                out.model, out.attempts = model, attempts
                return out
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                if schema_retry_done:
                    return DraftResult(tool="none", model=model, attempts=attempts, raw=str(call.function.arguments)[:500],
                                       reason=f"tool arguments invalid twice: {type(exc).__name__}")
                schema_retry_done = True
                messages = messages + [
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": call.id, "type": "function",
                         "function": {"name": call.function.name, "arguments": str(call.function.arguments)}}]},
                    {"role": "tool", "tool_call_id": call.id, "content":
                        f"Rejected: {exc}. Call the tool again with arguments that match this schema exactly: "
                        f"{json.dumps(TOOLS[0]['function']['parameters'])} or {json.dumps(TOOLS[1]['function']['parameters'])}"},
                ]
    return DraftResult(tool="none", attempts=attempts, reason="all models failed")


# =============================================================================
# Mock drafter (offline development only)
# =============================================================================

_WORD = re.compile(r"[a-z0-9]+")


def _tok(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if len(w) > 2}


def draft_mock(question: dict, evidence: list[dict]) -> DraftResult:
    if not evidence:
        return DraftResult(tool="flag_gap", reason="[mock] no evidence recalled", model="mock", attempts=1)
    q = _tok(question["text"])
    best = sorted(evidence, key=lambda e: -len(q & _tok(e["text"])))
    top = best[:2]
    overlap = len(q & _tok(top[0]["text"])) / (len(q) or 1)
    confidence = round(min(0.95, 0.45 + overlap), 2)
    text = " ".join(e["text"] for e in top)
    if not text.startswith("We"):
        text = text
    return DraftResult(tool="submit_answer", text=text, confidence=confidence,
                       evidence_ids=[e["id"] for e in top], model="mock", attempts=1)


def draft(question: dict, evidence: list[dict]) -> DraftResult:
    return draft_mock(question, evidence) if MOCK else draft_with_groq(question, evidence)
