In February, a lender sent my father's interior fit-out company a vendor pre-qualification questionnaire. Forty-odd questions, a folder of supporting documents, and a deadline of the next afternoon. The company was qualified. It had already built six gold-loan branches for another lender. Five months and two reminder emails later, the questionnaire was still not submitted.

Nobody was lazy. The answers existed. They were just scattered across old email threads, previous submissions and one person's head. Every new questionnaire started from zero.

That is the problem we built Prequal to solve: an agent that answers vendor questionnaires from a permanent memory of one company, shows the evidence behind every answer, and gets better every time someone corrects it.

## Why a normal LLM agent fails here

Our first instinct was the obvious one: paste the company profile into a prompt and let a model fill in the questionnaire. It fails in three ways.

1. It forgets. Each questionnaire is a fresh session, so last month's corrections are gone.
2. It guesses. Ask a model "Do you hold ISO 9001?" with thin context and it will happily produce a confident, plausible, wrong answer.
3. It can't show its work. A reviewer has no idea which fact an answer came from.

For vendor paperwork, the second failure is the dangerous one. A fabricated certification on a pre-qualification form is worse than no answer at all.

So the design goal became: the agent should only say what it can prove, and its memory should outlive any single session. That's where [Hindsight agent memory](https://github.com/vectorize-io/hindsight) came in.

## How the system hangs together

Prequal is a small FastAPI backend, a single-page review screen, SQLite for questionnaire state, Groq for the language model, and Hindsight as the only place company knowledge lives.

Every question goes through the same pipeline:

1. **Classify.** Each question has a type: exact field, document request, yes/no, numeric by year, or free text.
2. **Deterministic answers first.** GSTIN, PAN, entity type, address: these are looked up directly. No model call.
3. **Document check.** "Attach your ISO 9001 certificate" is checked against the company's document inventory. On file, or not held. Still no model.
4. **Recall.** Everything else pulls evidence from Hindsight.
5. **Draft with tools.** The model must call `submit_answer` with the evidence IDs it used, or `flag_gap`. Plain text is never treated as an answer.
6. **Guardrails.** Low confidence or uncited drafts go to human review. Any positive claim about a certification the company doesn't hold is blocked.

Automation before AI. The model only sees the questions that actually need judgment.

## The core story: memory is the product

Here is the part that surprised us. Without memory, Prequal is useless. With an empty memory bank, every single question comes back as "needs review". The agent has nothing to stand on, so it says so.

That's exactly the behaviour we wanted, and it's only possible because the agent treats [Hindsight](https://hindsight.vectorize.io/) as its source of truth rather than as a nice-to-have.

We give each company its own memory bank with a mission and three directives:

```python
DIRECTIVES = [
    ("no-fabrication", "Never state a certification, registration, licence or figure that is not in memory. If it is absent, say it is absent.", 1),
    ("timeline", "When a fact has more than one value over time, report the timeline, most recent first, and name the dates.", 2),
    ("reviewer-wins", "Prefer the reviewer's corrected answers over the agent's own earlier drafts.", 3),
]
```

On day one we retain three kinds of memory: company facts, one fact per document (on file or not held), and every answer the company has given on past questionnaires. Turnover is retained once per financial year, with a timestamp:

```python
for fy, v in p["turnover_by_fy"].items():
    w(f"{name}'s annual turnover for FY {fy} was {v['label']} (audited).",
      context="annual turnover from audited accounts",
      ts=_fy_end(fy), tags=("profile", "financial"))
```

That timestamp matters. When a questionnaire asks for "turnover for the last three years", recall returns dated facts, and the review screen renders them as a timeline, newest first. When a new year's figure is retained, it simply appears at the top. No overwriting, no stale numbers.

## The learning loop

The part engineers ask about most is how the agent improves. It's almost boring:

```python
def retain_review(question, final_text, reason):
    content = (f"Reviewer-approved answer on the {question['client']} questionnaire "
               f"to the question \"{question['text']}\": {final_text}")
    if reason:
        content += f" Reviewer's note: {reason}"
    m.retain(content, context=f"reviewer correction for {question['client']}",
             timestamp=datetime.utcnow(), tags=["review", ...])
```

Every approval and every edit becomes a memory, with the reviewer's reason attached. On the next questionnaire, a similar question recalls the corrected answer and the model cites it. If a reviewer says "always lead with the six-branch project", that preference is in memory for every future questionnaire, from every client.

This is the difference between an agent with a longer prompt and an agent with memory. A prompt resets. Memory accumulates.

## What it looks like in use

A reviewer opens a new questionnaire and clicks run. Questions stream in with one of three statuses:

- **Auto-filled:** answered from an exact field, a document on file, or cited evidence.
- **Needs you:** thin evidence or low confidence.
- **Gap:** nothing in memory answers it, or the document isn't held.

Click any answer and the evidence panel shows exactly which memories it came from, with the cited ones marked. A live log on the right shows every retain, recall and reflect call as it happens. Before answering, one [reflect](https://hindsight.vectorize.io/) call produces a short brief: which past projects matter for this client, which documents are missing.

The moment that sold us on the approach: asked "Do you hold ISO 9001 certification?", the agent answers "No. We do not hold an ISO 9001 certificate at present" and marks it as a gap for the reviewer to decide. It doesn't invent a certificate number. It tells you what's missing.

On a full run against a 43-question questionnaire from an NBFC, with real Hindsight memory and Groq's gpt-oss-120b drafting, Prequal answered 38 questions, sent 1 to a human for review, and flagged 4 as gaps. All four gaps were documents the company does not hold. The company in the demo is fictional, modelled on a real firm's structure with synthetic figures.

Here's a two-minute walkthrough:

{% embed https://youtu.be/cmx4w6JXkEw %}

## Lessons learned

**1. Deterministic first, model second.** In our 43-question sample, 15 questions are exact fields and 13 are document checks. Sending those to a model adds cost, latency and risk for nothing.

**2. Force the model to cite, then check the citations.** Tool calls with evidence IDs, validated after the fact, turned "trust me" answers into auditable ones.

**3. Timestamps are underrated.** Retaining facts with the date they became true made "what changed" a query instead of a data-cleaning job.

**4. Make the empty state honest.** An agent that says "I don't know this company yet" is more trustworthy than one that fills the silence.

**5. Function calling fails. Plan for it.** We validate every tool argument, retry once with the schema echoed back, fall back to a second model, and treat anything else as "needs review". A crash is never an answer.

## What's next

Parsing Excel and PDF questionnaires directly, writing answers back into the client's own template, and document expiry alerts so a solvency certificate never lapses silently.

The bigger lesson is about [what agent memory actually is](https://vectorize.io/what-is-agent-memory). It isn't a bigger context window. It's a record that outlives the session, knows when things were true, and gets better every time a human corrects it. For a small company answering the same forty questions over and over, that's the whole product.

Code: https://github.com/victorisaacchinta-eng/prequal
Demo: https://youtu.be/cmx4w6JXkEw
