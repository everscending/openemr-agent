# USER.md — Target User & Use Cases

This document defines who the Clinical Co-Pilot is for, the workflow it enters,
and the specific use cases it addresses. It is the source of truth for
[ARCHITECTURE.md](ARCHITECTURE.md): every agent capability must trace back to a
use case here, and each use case includes an explicit answer to *why an agent
is the right solution* — including where it honestly isn't the whole answer.

---

## 1. Target User

**Dr. Sarah Chen — outpatient primary care physician.**

- Sees **18–22 patients per day** in 15–20 minute slots at a community
  primary care practice running OpenEMR.
- Panel of ~1,800 patients: mostly chronic disease management (diabetes,
  hypertension, hyperlipidemia, COPD), preventive care, and acute minor
  complaints.
- Patients also generate records *outside* her visits: lab results arriving
  between visits, ED discharge notes, specialist consults, refill requests
  handled by staff.
- Runs **behind schedule by mid-morning on a typical day**. Time is the
  scarcest resource; anything that costs more than a minute doesn't get used,
  no matter how good it is.

### Who this is explicitly NOT for (v1)

- **Not nurses or medical assistants** — different permissions, different
  questions (rooming workflow, intake), different ACL surface. Deferred, not
  forgotten: the architecture's inherited-authorization design (see
  ARCHITECTURE.md §4) is what makes adding them later safe.
- **Not ED residents or hospitalists** — their workflows (overnight intake,
  rounding on a census) have different latency tolerances and data needs. One
  user, done well.
- **Not patients** — this is a clinician-facing tool inside the chart.
- **Not billing/administrative staff** — no billing, coding, or scheduling
  capabilities.

Choosing one narrow user is deliberate: the PRD's own framing is that
"physicians need help finding information" is a failed-product thesis. Every
capability below is sized to Dr. Chen's 90-second window, and anything that
doesn't serve it is out of scope.

---

## 2. The Workflow Moment

**8:56 AM.** Dr. Chen closes the door on patient #2 and has until 9:00 to be
ready for patient #3 — Margaret Reyes, 67, T2 diabetes and hypertension, last
seen four months ago. In those minutes she must recall: why is Margaret here
today, what changed since last visit, are there results on file she hasn't
seen, and is there anything that must not be missed?

**The thirty seconds before she opens the co-pilot:** she has just opened
Margaret's chart in OpenEMR from today's schedule. Today, answering those
questions means the summary screen, the meds list, the labs tab, the last
encounter note, and possibly scanned documents — five navigations, each a
page load, under time pressure, while the patient waits.

**What she needs from the co-pilot:** one trustworthy, cited answer to "catch
me up" — in seconds — plus the ability to pull one or two threads ("trend her
A1c", "why did cardiology stop the statin?") before walking in.

**What she does with the output:** walks into the room primed. She may glance
at the panel once more during the visit for a follow-up question. She does
*not* document with it, order with it, or bill with it — the co-pilot is
read-only preparation, not a documentation tool.

---

## 3. Use Cases

Each use case states the trigger, what the agent must do, success criteria,
and an explicit answer to **"why is an agent the right shape?"**

### UC-1: Pre-visit snapshot — "Catch me up on this patient"

- **Trigger:** Chart open, 1–4 minutes before entering the room.
- **Agent behavior:** Produce a prioritized summary relative to *today's
  visit*: reason for visit, what changed since the last encounter, active
  problems and meds, recent/pending results, overdue preventive items. Every
  claim cited to a specific record. Explicitly states what was checked and
  what wasn't.
- **Success criteria:** First content < 3s; complete < 10s; zero uncited
  clinical claims; explicitly notes data gaps ("no labs since October").
- **Why an agent?** Honest answer: the *initial* snapshot alone doesn't
  require conversation — a well-designed summary card could show it. The
  agent earns its shape two ways: (1) prioritization is contextual, not
  template-driven — "what matters today" depends on the visit reason and
  what changed, which is synthesis, not layout; (2) the snapshot is the
  entry point for UC-2 — the follow-up question is where a dashboard dead-ends
  and conversation is the only shape that works. UC-1 without UC-2 would not
  justify an agent.

### UC-2: Follow-up interrogation — pulling a thread

- **Trigger:** The snapshot (UC-1) surfaces something that needs one more
  step: "trend her A1c over two years", "when did she start metformin?",
  "why was atorvastatin discontinued?", "what did the ED note say?"
- **Agent behavior:** Multi-turn conversation with context carried from the
  snapshot ("her" = Margaret; "the ED note" = the one just mentioned).
  Targeted tool calls to retrieve the specific history, trend, or document.
  Citations on every claim.
- **Success criteria:** Follow-up answers < 5s; correctly resolves
  referring expressions from conversation context; says "not on file" rather
  than guessing when the record doesn't contain the answer.
- **Why an agent?** This is the load-bearing case for the conversational
  shape. Follow-up questions are unbounded and contextual — no dashboard can
  pre-build every drill-down, and a search bar without conversation state
  makes the physician re-specify patient and context on every query. This use
  case is also why multi-turn context exists at all (PRD rule: no multi-turn
  without a use case requiring it — this is that use case).

### UC-3: Interval-change review — "What's new since I last saw her?"

- **Trigger:** Returning patient with months between visits; records have
  accumulated from outside Dr. Chen's own encounters (lab results, ED visits,
  specialist notes, med changes by other prescribers).
- **Agent behavior:** Diff the record against the date of Dr. Chen's last
  encounter with this patient: new results, new/changed/stopped medications,
  new encounters elsewhere, new problems. Ranked by clinical salience, not
  chronology. Cited.
- **Success criteria:** Catches every new record since the reference date
  (completeness is the invariant here — a missed ED visit is the worst
  failure); clearly separates "new since you last saw her" from "pre-existing."
- **Why an agent?** The raw diff is mechanical, but *salience ranking and
  synthesis* ("two ED visits for the same complaint, and a new med that
  interacts with an existing one") requires reasoning across record types.
  OpenEMR has no interval-summary view; building one as a static feature
  would hard-code one prioritization scheme, where the agent can rank against
  today's context and answer "why is that flagged?" (UC-2) about its own
  output.

### UC-4: Coverage & absence questions — "Do we have...?"

- **Trigger:** Quick factual checks with possibly-negative answers: "Do we
  have any imaging on file?", "Has she had a pneumonia vaccine?", "Any
  colonoscopy result since 2020?"
- **Agent behavior:** Search the relevant record categories; answer
  positively with citations, or *negatively with scope* — "no imaging reports
  on file; I checked documents and encounter notes" — never a bare "no."
- **Success criteria:** Negative answers always state what was searched;
  positive answers cite the record; < 5s.
- **Why an agent?** A negative answer requires exhaustively checking several
  places a human would have to navigate individually (documents, immunization
  table, encounter notes, outside records). Natural language is the right
  input because the question space is open-ended; the *scoped-absence*
  answer format ("checked X and Y, found nothing") is something the agent
  architecture guarantees (see ARCHITECTURE.md §8 invariant) and a search
  bar does not.

---

## 4. Explicit Refusal Boundary

The co-pilot **retrieves, synthesizes, and cites this patient's record. It
does not practice medicine.** It refuses, with a clear statement of why:

- Treatment recommendations, medication dosing advice, diagnosis suggestions
  ("what should I prescribe?" → refuse; "what is she currently prescribed?" →
  answer).
- General medical knowledge questions not grounded in this patient's record
  (that is what UpToDate is for; ungrounded answers cannot cite the chart,
  and uncitable claims violate the verification invariant).
- Questions about patients other than the one whose chart is open (the agent
  is patient-context-bound by construction; see ARCHITECTURE.md §4).
- Any request to modify the record (read-only in v1).

The one deliberate nuance: the agent *does* surface OpenEMR's own clinical
decision support outputs (drug-interaction flags, care reminders) because
those are data in the record's ecosystem, not the agent's medical opinion —
and it attributes them as such.

---

## 5. Traceability

| Capability in ARCHITECTURE.md | Justified by |
|---|---|
| Conversational, multi-turn interface | UC-2 (and only UC-2 — stated honestly) |
| Parallel snapshot tool (`get_patient_snapshot`) | UC-1 latency budget |
| Targeted retrieval tools (observations, med history, notes/documents) | UC-2, UC-3, UC-4 |
| Interval-diff logic with reference date | UC-3 |
| Scoped-absence answer format ("checked X, Y — nothing found") | UC-4; failure-mode invariant |
| Citation-per-claim + deterministic verification | All UCs (trust is the product) |
| Read-only tool set | §4 refusal boundary; §2 workflow (preparation, not documentation) |
| Refusal handling in system prompt + evals | §4 |

Anything not in this table does not get built.
