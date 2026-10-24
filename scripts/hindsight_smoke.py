"""Hindsight smoke test. Run this once in prep, before writing any app code.

    pip install hindsight-client python-dotenv
    export HINDSIGHT_API_KEY=...        # from https://ui.hindsight.vectorize.io
    python scripts/hindsight_smoke.py

What it checks, in order:
  1. the client can reach the API with your key
  2. a bank can be created (or already exists)
  3. three dated facts about one quantity can be retained
  4. recall returns them, and prints every field the result object exposes
     (we need occurred_start / occurred_end for the turnover timeline)
  5. reflect answers a question over them

Paste the printed output into the team chat. If step 4 does not show dates,
or shows only one consolidated value, tell Victor: the contradiction beat
needs a different recall setting (types=["world"] without observations).
"""

import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

try:
    from hindsight_client import Hindsight
except ImportError:
    sys.exit("hindsight-client is not installed: pip install hindsight-client")

API_KEY = os.getenv("HINDSIGHT_API_KEY")
BASE_URL = os.getenv("HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io")
BANK = os.getenv("HINDSIGHT_SMOKE_BANK", "prequal-smoke")

if not API_KEY and "localhost" not in BASE_URL:
    sys.exit("HINDSIGHT_API_KEY is not set (or point HINDSIGHT_BASE_URL at a local Docker instance)")


def show(label, obj):
    """Print an SDK object with every public attribute, so we learn the real shape."""
    print(f"\n--- {label} ---")
    if hasattr(obj, "model_dump"):
        print(obj.model_dump())
    elif hasattr(obj, "__dict__"):
        print({k: v for k, v in vars(obj).items() if not k.startswith("_")})
    else:
        print(repr(obj))


client = Hindsight(base_url=BASE_URL, api_key=API_KEY, timeout=60.0)
print(f"1. client ready against {BASE_URL}")

try:
    client.create_bank(
        bank_id=BANK,
        name="Prequal smoke test",
        mission="You are the institutional memory of a small interior contractor. You know its facts and how they changed over time.",
        disposition={"skepticism": 4, "literalism": 4, "empathy": 2},
    )
    print(f"2. bank '{BANK}' created")
except Exception as exc:  # already exists, or the SDK names things differently
    print(f"2. create_bank raised {type(exc).__name__}: {exc} (fine if the bank already exists)")

facts = [
    ("Turnover of Northstar Interiors for FY 2022-23 was Rs 48 lakh.", datetime(2023, 3, 31)),
    ("Turnover of Northstar Interiors for FY 2023-24 was Rs 59 lakh.", datetime(2024, 3, 31)),
    ("Turnover of Northstar Interiors for FY 2024-25 was Rs 53 lakh.", datetime(2025, 3, 31)),
]
for content, ts in facts:
    kwargs = dict(bank_id=BANK, content=content, context="annual turnover from audited accounts", timestamp=ts, retain_async=False)
    try:
        res = client.retain(**kwargs, tags=["profile", "financial"])
    except TypeError:
        res = client.retain(**kwargs)  # SDK version without tags
    print(f"3. retained: {content}")
    show("retain response", res)

print("\n4. recall: 'annual turnover for the last three years'")
results = client.recall(
    bank_id=BANK,
    query="annual turnover of Northstar Interiors for the last three financial years",
    types=["world", "observation"],
    budget="mid",
    max_tokens=2000,
)
show("recall response object", results)
for i, r in enumerate(getattr(results, "results", []) or []):
    show(f"result {i}", r)
    print("  text:", getattr(r, "text", None))
    print("  type:", getattr(r, "type", None))
    print("  occurred_start:", getattr(r, "occurred_start", None), " occurred_end:", getattr(r, "occurred_end", None))
    print("  entities:", getattr(r, "entities", None))

print("\n4b. recall, world facts only (what the timeline will use)")
results_world = client.recall(bank_id=BANK, query="turnover by financial year", types=["world"], budget="mid")
for r in getattr(results_world, "results", []) or []:
    print(" ", getattr(r, "occurred_start", None), "|", getattr(r, "text", None))

print("\n5. reflect")
answer = client.reflect(
    bank_id=BANK,
    query="Summarise this company's turnover trend over the last three years, most recent first, with the years named.",
    budget="low",
)
show("reflect response", answer)
print("reflect text:", getattr(answer, "text", None))

print("\nDone. Bank left in place; delete it from the Hindsight UI if you want a clean slate.")
