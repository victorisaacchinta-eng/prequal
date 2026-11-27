"""Tests for the questionnaire export in backend/main.py. No Hindsight or Groq keys needed.

Run from the repo root with `python -m pytest backend/tests`.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend import main


@pytest.mark.parametrize("cell", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
def test_csv_safe_quotes_formula_cells(cell):
    assert main._csv_safe(cell) == "'" + cell


@pytest.mark.parametrize("cell", ["Yes, ISO 9001", "", "a=b", 3, 0.82, None])
def test_csv_safe_leaves_other_cells(cell):
    assert main._csv_safe(cell) == cell


def _questionnaire():
    return {"id": 1, "slug": "acme", "questions": [{
        "ordinal": 1, "section": "General", "text": "=HYPERLINK(\"http://x\")",
        "final_text": None, "draft_text": "-2 years", "status": "draft",
        "confidence": 0.5, "source": "auto",
    }]}


async def _body(resp) -> str:
    return "".join([c async for c in resp.body_iterator])


def test_csv_export_quotes_question_and_answer(monkeypatch):
    monkeypatch.setattr(main.db, "get_questionnaire", lambda qid: _questionnaire())
    body = asyncio.run(_body(main.export(1, format="csv")))
    row = body.splitlines()[1]
    assert row.startswith("1,General,")
    assert "'=HYPERLINK" in row and ",'-2 years," in row


def test_unknown_format_is_400(monkeypatch):
    monkeypatch.setattr(main.db, "get_questionnaire", lambda qid: _questionnaire())
    with pytest.raises(HTTPException) as exc:
        main.export(1, format="xlsx")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("client, slug", [
    ("Acme Corp", "acme-corp"),
    ("Sharma & Sons; Pvt.", "sharma-sons-pvt"),
    ("शर्मा ट्रेडर्स", "questionnaire"),
    ("₹ Traders", "traders"),
])
def test_slugify_keeps_only_ascii_letters_digits_hyphens(client, slug):
    assert main._slugify(client) == slug


@pytest.mark.parametrize("format", ["json", "csv"])
def test_export_with_non_ascii_slug_sets_safe_filename(monkeypatch, format):
    qn = {**_questionnaire(), "slug": 'शर्मा ₹;"x', "client": "Sharma", "title": "T",
          "received_on": None, "due_on": None}
    monkeypatch.setattr(main.db, "get_questionnaire", lambda qid: qn)
    resp = main.export(1, format=format)
    header = resp.headers["content-disposition"]
    header.encode("latin-1")  # Starlette sends headers as latin-1
    assert f'filename="_x-answers.{format}";' in header
    assert header.endswith(f"filename*=UTF-8''%E0%A4%B6%E0%A4%B0%E0%A5%8D%E0%A4%AE%E0%A4%BE%20%E2%82%B9%3B%22x-answers.{format}")
