"""Groq smoke test: one tool call per model, prints the exact error if it fails.

    python scripts/groq_smoke.py
"""

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from groq import Groq  # noqa: E402

from backend.llm import TOOLS  # noqa: E402

key = os.getenv("GROQ_API_KEY")
if not key:
    sys.exit("GROQ_API_KEY is empty in .env")
print(f"key loaded: {key[:4]}...{key[-4:]} ({len(key)} chars)")

client = Groq(api_key=key)
messages = [
    {"role": "system", "content": "Answer only from the evidence. Always call exactly one tool."},
    {"role": "user", "content": "QUESTION: How many sites can you run at once?\nEVIDENCE:\n- id=e1 type=world: Northstar Interiors can run 4 sites simultaneously.\nCall submit_answer or flag_gap."},
]
for model in (os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"), os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")):
    print(f"\n--- {model} ---")
    try:
        r = client.chat.completions.create(model=model, messages=messages, tools=TOOLS, tool_choice="required", temperature=0, max_tokens=600)
        m = r.choices[0].message
        calls = m.tool_calls or []
        if calls:
            print("OK tool call:", calls[0].function.name, json.loads(calls[0].function.arguments))
        else:
            print("NO tool call. text:", (m.content or "")[:300])
        print("usage:", r.usage)
    except Exception as exc:
        print("FAILED:", type(exc).__name__)
        print(str(exc)[:800])
