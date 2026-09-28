# Prequal

**The vendor questionnaire agent that never answers the same question twice.**

Small contractors get pre-qualification questionnaires (PQs) and RFPs from banks, NBFCs, insurers and hospital chains: 30 to 60 questions plus a folder of supporting documents, usually due in days. The answers are almost always the same as last time, but last time lives in someone's email. So the questionnaire takes days, or never gets submitted, and the tender is lost.

Prequal keeps the company's institutional memory in [Hindsight](https://github.com/vectorize-io/hindsight) and answers the next questionnaire from it: every fact, every document, every answer ever given, and every correction a reviewer made. What it cannot back with evidence it flags as a gap. It never invents a certificate.

## How it works

```
seed data (JSON)  ──POST /ingest──▶  Hindsight memory bank  (retain: profile, documents, past answers)
                                             │
questionnaire (JSON) ──POST /run──▶  per question:
                                       1. classify (deterministic)
                                       2. exact field?      → answer from the ingested fields map, no model
                                       3. document check?   → on file / not held, deterministic
                                       4. otherwise         → recall() evidence → Groq drafts with tool calls
                                       5. guardrails        → auto / needs review / gap
                                             │
reviewer approves or edits ──PATCH──▶  retain() the correction   (the learning loop)
```

Three statuses per answer:

| Status | Meaning |
| --- | --- |
| auto | Answered with confidence from an exact field, a document on file, or cited evidence |
| needs review | Thin evidence, low confidence, or the model did not return a valid tool call |
| gap | Nothing in memory answers it, or the document is not held. The agent will not guess |

## How Hindsight is used

Hindsight is the only store of company facts. SQLite holds questionnaires, answers and review state, nothing else.

- **One memory bank per company** (`HINDSIGHT_BANK_ID`), created with a mission and a conservative disposition (skepticism 4, literalism 4).
- **Directives** on the bank: never state a certification or figure that is not in memory; report changed facts as a dated timeline; prefer reviewer corrections.
- **retain()** at ingest: company profile facts (world), one dated fact per financial year for turnover, one fact per document (on file or not held, tagged with the document key), and one experience fact per past questionnaire answer (tagged `pq:<client>`), plus outcomes.
- **recall()** per question, with type and tag scoping: financial questions recall world facts tagged `financial` so the UI can render a timeline from `occurred_start`; document questions recall by the document's tag; everything else recalls world, experience and observation facts. The recalled items are shown inline as the evidence behind each draft, with the ones the model cited marked.
- **reflect()** for the pre-answer brief: what the company should know before answering this client's questionnaire.
- **The learning loop**: every approval and edit is retained with the question, the client and the reviewer's reason. The next questionnaire's similar question recalls the corrected answer first.
- **Facts that change**: a new financial year's turnover retained live shows up at the top of the timeline on the next run, with the older values below it.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # add HINDSIGHT_API_KEY and GROQ_API_KEY
uvicorn backend.main:app --reload --port 8000
# open http://localhost:8000
```

In the UI: **Ingest company memory**, pick a questionnaire, **Run agent**, click any question to see its answer and the evidence. Approve or edit; edits become memories. **Fresh agent** clears the bank so you can see the agent with no memory.

Offline development without keys: `PREQUAL_MOCK=1 uvicorn backend.main:app --port 8000`. The mock memory is a keyword matcher and the mock drafter pastes evidence; it exists for UI work only.

Before writing app code against a new Hindsight account, run `python scripts/hindsight_smoke.py` to confirm retain, recall and reflect, and to see which fields recall returns.

## Repository

```
backend/    FastAPI app (main.py), Hindsight wrapper (memory.py), answer loop (agent.py),
            Groq tool-calling client (llm.py), SQLite (db.py), seed ingest (ingest.py)
frontend/   one static page: index.html, app.js, styles.css
data/       synthetic company profile, document inventory, past answers, three questionnaires
scripts/    hindsight_smoke.py
```

The seed company, its identifiers and figures are fictional, modelled on the structure of a real 32-person interior fit-out firm and the real questionnaire formats it receives. Questionnaires are JSON (see `data/questionnaires/`); parsing Excel or PDF forms is not part of this version.

## Error handling

- Tool arguments are validated with pydantic. Invalid arguments get one retry with the schema echoed back; a second failure becomes `needs review` with the raw text kept.
- A plain-text reply instead of a tool call is `needs review`, never an answer.
- Groq rate limits and server errors back off twice, then fall back to `qwen/qwen3-32b`. If both fail, `needs review`.
- If Hindsight is unreachable during recall, the answer is `needs review` with a note. If it is unreachable during retain, the memory is queued in SQLite and retried on `POST /api/memory/flush`.
- Every guardrail decision is logged with its reason and shown under the answer.

## API

| Method and path | Purpose |
| --- | --- |
| `POST /api/ingest` | Retain seed data into the bank; build the deterministic caches |
| `POST /api/memory/reset` | Clear the bank (fresh agent) |
| `POST /api/memory/retain` | Retain one fact live |
| `GET /api/memory/log` | Recent retain, recall and reflect calls |
| `POST /api/questionnaires/load-samples` | Load the three sample questionnaires |
| `POST /api/questionnaires` | Upload a questionnaire JSON |
| `POST /api/questionnaires/{id}/run` | Run the agent (server-sent events) |
| `POST /api/questionnaires/{id}/brief` | reflect() brief |
| `GET /api/questionnaires/{id}` | Questions, answers, evidence, statuses |
| `PATCH /api/answers/{id}` | approve, edit, gap, reopen |
| `GET /api/questionnaires/{id}/export?format=csv` | Final answers |

## Licence

MIT.
