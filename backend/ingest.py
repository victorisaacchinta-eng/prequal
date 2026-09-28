"""Load the seed data into Hindsight and build the deterministic caches.

Two outputs:
  1. Memories in the Hindsight bank (world facts about the company and its documents,
     experience facts for past answers and outcomes), tagged and timestamped.
  2. A small local cache (.prequal_cache.json) with the exact-match fields map and the
     document inventory, used by the deterministic layer. It is written only by ingest
     and deleted by reset, so the "fresh agent" state is truly empty.
"""

import json
import logging
import os
from datetime import datetime

from . import memory as mem

log = logging.getLogger("prequal.ingest")

DATA_DIR = os.getenv("PREQUAL_DATA", os.path.join(os.path.dirname(__file__), "..", "data"))
CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", ".prequal_cache.json")


def _load(name: str):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def _fy_end(fy: str) -> datetime:
    """'2023-24' -> 2024-03-31"""
    end_year = int("20" + fy.split("-")[1]) if len(fy.split("-")[1]) == 2 else int(fy.split("-")[1])
    return datetime(end_year, 3, 31)


def load_cache() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {"fields": {}, "documents": {}, "company": None}
    with open(CACHE_PATH, encoding="utf-8") as f:
        return json.load(f)


def clear_cache() -> None:
    if os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)


def build_fields(p: dict) -> dict:
    """Exact-answer fields. A None value means 'not applicable' and gets a canned honest answer."""
    nw = p.get("net_worth_inr")
    fields = {
        "legal_name": p["legal_name"],
        "trade_name": p.get("trade_name"),
        "entity_type": p["entity_type"],
        "incorporation_year": str(p["incorporation_year"]),
        "registered_address": p["registered_address"],
        "office_address": p.get("office_address") or p["registered_address"],
        "contact_person": p["contact_person"],
        "contact_email": p["contact_email"],
        "contact_phone": p["contact_phone"],
        "website": p.get("website"),
        "gstin": p["gstin"],
        "pan": p["pan"],
        "cin": p.get("cin"),
        "udyam_registration": p.get("udyam_registration"),
        "bank_name": f"{p['bank_name']}, {p['bank_branch']}" if p.get("bank_branch") else p["bank_name"],
        "employees_total": str(p["employees_total"]),
        "years_in_business": str(p["years_in_business"]),
        "equipment": p.get("equipment"),
        "net_worth_label": f"Rs {nw / 100000:.0f} lakh as per the latest audited balance sheet (FY 2024-25)" if nw else None,
        "preferred_zones": ", ".join(p.get("preferred_zones") or []) or None,
    }
    null_answers = {
        "cin": "Not applicable. The entity is a proprietorship and does not have a Corporate Identity Number.",
        "udyam_registration": "Not registered under Udyam at present.",
        "website": "No website at present.",
    }
    return {"values": fields, "null_answers": null_answers}


def profile_facts(p: dict) -> list[dict]:
    name = p["trade_name"]
    f = []

    def w(content, context="company profile", ts=None, tags=("profile",)):
        row = {"content": content, "context": context, "tags": list(tags)}
        if ts:
            row["timestamp"] = ts
        f.append(row)

    w(f"{p['legal_name']} (trading as {name}) is a {p['entity_type'].lower()} founded in {p['incorporation_year']}. {p['registration']}.")
    w(f"{name} is owned by {p['proprietor']}, {p['proprietor_background']}. The proprietor has {p['proprietor_experience_years']} years of experience in interiors.")
    w(f"{name}'s registered and office address is {p['registered_address']}.")
    w(f"{name}'s authorised contact is {p['contact_person']}, email {p['contact_email']}, phone {p['contact_phone']}.")
    w(f"{name}'s GSTIN is {p['gstin']} and PAN is {p['pan']}. It has no CIN because it is a proprietorship.")
    w(f"{name} is not registered under Udyam (MSME status: {p['msme_status']}).")
    w(f"{name} has been in the interior fit-out business for {p['years_in_business']} years.")
    w(f"{name}'s service lines: " + "; ".join(p["service_lines"]) + ".")
    w(f"{name} does not offer: " + "; ".join(p["not_offered"]) + ".", context="capabilities and exclusions")
    b = p["employees_breakdown"]
    w(f"{name} employs {p['employees_total']} people: {b['architects_interior_designers']} architect and interior designer, "
      f"{b['planning_and_commercial']} planning and commercial lead, {b['project_coordinators']} project coordinators, "
      f"{b['site_engineers_supervisors']} site engineers and supervisors, {b['skilled_technicians']} skilled technicians and "
      f"{b['labour']} labour. {p['consultants']}.", context="organisation")
    w(f"{name} has no in-house factory. {p['modular_furniture_source']}.", context="organisation")
    w(f"{name}'s equipment: {p['equipment']}.", context="organisation")
    w(f"{name}'s preferred working zone is {', '.join(p['preferred_zones'])}; it has executed sites in {', '.join(p['states_executed'])}.", context="capacity")
    w(f"{name} can run {p['simultaneous_sites_capacity']} sites simultaneously and typically completes a 750 sq ft branch in {p['typical_branch_turnaround_days']} working days from site handover.", context="capacity")
    for fy, v in p["turnover_by_fy"].items():
        w(f"{name}'s annual turnover for FY {fy} was {v['label']} (audited).", context="annual turnover from audited accounts",
          ts=_fy_end(fy), tags=("profile", "financial"))
    w(f"{name}'s net worth is Rs {p['net_worth_inr'] / 100000:.0f} lakh as per the FY 2024-25 audited balance sheet.", context="financials", tags=("profile", "financial"))
    w(f"{name}'s principal banker is {p['bank_name']}, {p['bank_branch']} ({p['bank_account_type']}).", context="financials")
    w(f"{name}'s largest single project: {p['largest_single_project']}, value Rs {p['largest_single_project_inr'] / 100000:.0f} lakh.", context="experience", tags=("profile", "experience"))
    w(f"{name}'s clients in the last five years include " + ", ".join(p["clients"]) + ".", context="experience", tags=("profile", "experience"))
    for pr in p["past_projects"]:
        cert = "Completion certificate available." if pr.get("completion_certificate") else "No completion certificate on file."
        w(f"{name} executed for {pr['client']}: {pr['scope']}, at {pr['location']}, about {pr['area_sqft']:,} sq ft, "
          f"value Rs {pr['value_inr'] / 100000:.1f} lakh, in {pr['period']}. {cert}",
          context="past project", ts=_fy_end(pr["period"]) if "-" in pr["period"] else None, tags=("profile", "experience"))
    for og in p.get("ongoing_projects", []):
        w(f"{name} currently has in progress: {og['scope']} for {og['client']} at {og['location']}, about {og['area_sqft']:,} sq ft, expected completion {og['expected_completion']}.",
          context="ongoing project", tags=("profile", "experience"))
    w(f"{name}: litigation status: {p['litigation']}.", context="declarations")
    w(f"{name}: blacklisting status: {p['blacklisting']}.", context="declarations")
    w(f"{name}: safety record: {p['safety_record']}. PPE is mandatory on all sites.", context="quality and compliance")
    w(f"{name}: statutory compliance: {p['statutory_compliance']}. PF registration {p['pf_registration']}, ESI registration {p['esi_registration']}.", context="quality and compliance")
    w(f"{name}: insurance: {p['insurance']}.", context="quality and compliance")
    w(f"{name} does not hold ISO 9001, ISO 14001 or OHSAS 45001 certification, has no written quality manual and no written EHS policy. "
      f"Site quality is managed with a written site checklist and a two-stage handover inspection.", context="quality and compliance")
    return f


def document_facts(docs: list[dict]) -> list[dict]:
    f = []
    for d in docs:
        if d["status"] == "on_file":
            extra = []
            if d.get("issued"):
                extra.append(f"issued {d['issued']}")
            if d.get("valid_until"):
                extra.append(f"valid until {d['valid_until']}")
            content = f"Document on file: {d['name']} ({d['file']}{', ' + ', '.join(extra) if extra else ''}). Satisfies requests for: {', '.join(d['satisfies'])}."
        else:
            content = f"Document NOT held: {d['name']}. The company cannot attach this; requests for {', '.join(d['satisfies'])} must be answered as not available."
        f.append({"content": content, "context": "document inventory", "tags": ["document", d["key"]]})
    return f


def past_answer_facts(past: list[dict]) -> list[dict]:
    f = []
    for pq in past:
        ts = datetime.fromisoformat(pq["answered_on"])
        tag = f"pq:{pq['client_slug']}"
        for a in pq["answers"]:
            f.append({
                "content": f"On the {pq['client']} questionnaire ({pq['questionnaire']}), answered on {pq['answered_on']}, "
                           f"the question \"{a['question']}\" was answered: {a['answer']}",
                "context": f"past questionnaire answer for {pq['client']}",
                "timestamp": ts,
                "tags": [tag, "past-answer"],
            })
        f.append({
            "content": f"Outcome of the {pq['client']} questionnaire answered on {pq['answered_on']}: {pq['outcome']}",
            "context": "questionnaire outcome",
            "timestamp": ts,
            "tags": [tag, "outcome"],
        })
    return f


def run_ingest() -> dict:
    profile = _load("company_profile.json")
    docs = _load("documents.json")
    past = _load("past_answers.json")

    m = mem.get_memory()
    m.ensure_bank(profile["trade_name"])

    counts = {}
    batches = [
        ("profile", profile_facts(profile)),
        ("documents", document_facts(docs)),
        ("past_answers", past_answer_facts(past)),
    ]
    for label, items in batches:
        res = m.retain_batch(items, document_id=f"seed-{label}")
        counts[label] = res.get("count", len(items))

    cache = {
        "company": {"slug": profile["slug"], "name": profile["trade_name"], "legal_name": profile["legal_name"]},
        "fields": build_fields(profile),
        "documents": {d["key"]: {"name": d["name"], "status": d["status"], "file": d.get("file"),
                                 "issued": d.get("issued"), "valid_until": d.get("valid_until"),
                                 "satisfies": d["satisfies"]} for d in docs},
        "turnover_update_for_demo": profile.get("turnover_update_for_demo"),
        "ingested_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    counts["total"] = sum(v for k, v in counts.items())
    log.info("ingest complete: %s", counts)
    return counts
