"""Tests for the pure helpers in backend/ingest.py. No Hindsight or Groq keys needed.

Run from the repo root with `python -m pytest backend/tests` (bare `pytest` cannot
import the `backend` package).
"""

from datetime import datetime

import pytest

from backend import agent
from backend.ingest import _fy_end, _load, build_fields


def _profile(**overrides) -> dict:
    """Smallest profile build_fields accepts: a proprietorship with no CIN, website or Udyam."""
    p = {
        "legal_name": "Test Interiors",
        "trade_name": "Test",
        "entity_type": "Proprietorship",
        "incorporation_year": 2015,
        "registered_address": "1 Main Road, Hyderabad",
        "contact_person": "A. Person",
        "contact_email": "a@example.com",
        "contact_phone": "+91 90000 00000",
        "gstin": "36AAAAA0000A1Z5",
        "pan": "AAAAA0000A",
        "bank_name": "Some Bank",
        "employees_total": 12,
        "years_in_business": 9,
    }
    p.update(overrides)
    return p


@pytest.mark.parametrize("fy", ["2023-24", "2023-2024"])
def test_fy_end_two_and_four_digit_years(fy):
    assert _fy_end(fy) == datetime(2024, 3, 31)


def test_proprietorship_gets_canned_answers_for_missing_ids():
    fields = build_fields(_profile())
    for key in ("cin", "website", "udyam_registration"):
        assert fields["values"][key] is None
        assert fields["null_answers"][key]
    # The agent turns these into an auto answer instead of a gap.
    ans = agent.answer_field({"field": "cin"}, {"fields": fields})
    assert ans["status"] == "auto"
    assert ans["draft_text"] == fields["null_answers"]["cin"]


def test_office_address_falls_back_to_registered_address():
    assert build_fields(_profile())["values"]["office_address"] == "1 Main Road, Hyderabad"
    p = _profile(office_address="2 Side Street, Hyderabad")
    assert build_fields(p)["values"]["office_address"] == "2 Side Street, Hyderabad"


def test_optional_labels_are_none_when_absent():
    values = build_fields(_profile())["values"]
    assert values["net_worth_label"] is None
    assert values["preferred_zones"] is None
    assert values["bank_name"] == "Some Bank"


def test_seed_profile_builds():
    values = build_fields(_load("company_profile.json"))["values"]
    assert values["entity_type"] == "Proprietorship"
    assert values["net_worth_label"].startswith("Rs 32 lakh")
