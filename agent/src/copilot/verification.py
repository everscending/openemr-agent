"""Layer 1 — source attribution (T008), deterministic and non-LLM.

ARCHITECTURE.md §5: every clinical claim in model output must cite a
``[ResourceType/id]`` token that appeared in *this* request's tool results,
keyed by **type + id**. This module is the post-generation filter that enforces
it. It imports no LLM client and makes no network calls — it is a pure function
over (draft text, the ``ResourceRef``s the tools returned).

How a sentence is classified
-----------------------------
The draft is segmented into sentences (period/newline heuristics; a period
between two digits, e.g. ``5.8``, never splits). Each sentence is one of:

* **Claim** — it carries at least one ``[ResourceType/id]`` token. It is
  *verified*: every cited token must be present in the tool-result set under
  the same type+id. If any cited resource is absent (unknown id) or present
  only under a different type (type mismatch), the whole sentence is stripped —
  a claim derived from a fabricated source carries the fabricated citation and
  falls with it (dependency-by-citation, §5).

* **Scaffolding** — a *citation-free* sentence that matches the structural
  whitelist below. Kept, not counted, never treated as a claim.

* **Uncited claim** — a citation-free sentence that matches *nothing* in the
  whitelist. Unrecognized phrasing fails closed: it is stripped.

The claim-vs-scaffolding boundary (the rule, rev 3)
---------------------------------------------------
Scaffolding is a statement about *the agent's process*; a clinical claim is a
statement about *the patient*. A citation-free sentence is kept **only** if it
matches one of these structural patterns, and the coverage/absence patterns take
a **data-category object**, never an arbitrary proposition:

* **Coverage** — an (optional ``I``/``we``) + a *retrieval* verb (checked,
  reviewed, searched, looked at, examined, pulled, queried, went through, …)
  whose object consists solely of data-category nouns and grammatical filler,
  with at least one real data-category noun. The category noun set
  (:data:`DATA_CATEGORY_NOUNS`) is derived from the tool categories the service
  returns (:data:`copilot.contracts.tools.SNAPSHOT_CATEGORIES` — the contracts
  layer, so the pure verifier pulls no IO/httpx dependency) plus the generic
  chart/record/records and record-*type* nouns (colonoscopy, imaging, …), so it
  cannot drift from what the tools return. If any object token is neither a
  category noun nor filler — e.g. a ``that``-clause or a predicate — the sentence
  is *not* coverage. ``verified`` and ``confirmed`` are **assertive**, not
  retrieval verbs: "I confirmed she is allergic to penicillin" is a claim
  wearing a process costume and is stripped when uncited.

* **Absence** — "no <data category> on record / on file / recorded / documented /
  found". The **object** is constrained the same way the coverage object is: the
  absent thing must be a data category (a record/study/data type), never a
  clinical *finding*. "No colonoscopy is on record" is coverage (a record type);
  "No signs of infection were documented" and "No evidence of malignancy was
  found" name findings and are stripped when uncited — even though "documented"
  and "found" are valid markers. The marker locates where the object ends; the
  object constraint, not the marker, does the finding-vs-category discrimination.

* **Refusal** ("couldn't/cannot/can't verify|confirm", "unable to verify"),
  **greeting**, **navigation** ("view/see/open … records/chart/source"), and
  **section header** (a line ending in ``:``) are process/UI scaffolding and are
  kept as-is.

Fail-closed emission (no fractional floor)
------------------------------------------
If at least one claim-bearing sentence survives, the survivors are emitted with
the removal marker appended. Only when **no** claim-bearing sentence survives
*and* at least one claim was stripped is the entire response replaced by
:data:`FALLBACK_TEXT` plus the available refs. A draft that made no claims at
all (pure scaffolding) is never a fallback case. There is deliberately no
threshold, fraction, or floor: adding scaffolding to a draft can never change
the fallback decision, because scaffolding is never counted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from copilot.contracts.refs import FhirResourceType, ResourceRef
from copilot.contracts.tools import SNAPSHOT_CATEGORIES
from copilot.contracts.verification import (
    ClaimStatus,
    ClaimVerdict,
    StrippedClaim,
    VerificationCounts,
    VerificationVerdict,
)

# ---------------------------------------------------------------------------
# User-facing constants
# ---------------------------------------------------------------------------

#: Appended to the output whenever any claim was stripped but survivors remain.
CONTENT_REMOVED_MARKER = (
    "[Some statements were removed because they could not be verified "
    "against the source records.]"
)

#: Replaces the whole response when no claim-bearing sentence survives.
FALLBACK_TEXT = (
    "I couldn't verify this against the source records. "
    "View the source records in the chart."
)

# ---------------------------------------------------------------------------
# Data-category noun set — sourced from the tool categories so it cannot drift
# ---------------------------------------------------------------------------

# One synonym cluster per snapshot category. The categories themselves come from
# SNAPSHOT_CATEGORIES (what the tools actually return); if a new category is
# added there with no cluster here, its raw token is still admitted (below), and
# the test suite's sourcing check forces this map to be kept in step.
_CATEGORY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "demographics": ("demographics", "demographic"),
    "medications": (
        "medications",
        "medication",
        "meds",
        "med",
        "prescriptions",
        "prescription",
    ),
    "problems": (
        "problems",
        "problem",
        "conditions",
        "condition",
        "diagnoses",
        "diagnosis",
    ),
    "allergies": ("allergies", "allergy", "intolerances", "intolerance"),
    "labs": (
        "labs",
        "lab",
        "laboratory",
        "labwork",
        "observations",
        "observation",
        "results",
        "vitals",
        "vital",
    ),
    "last_encounter": ("encounters", "encounter", "visits", "visit"),
}

# Generic data-store nouns the tools also surface (ARCHITECTURE.md §5 names
# "the generic chart/record/records"). "list" lets "problem list"/"medication
# list" tokenize as category + generic without multi-word matching.
_GENERIC_DATA_NOUNS: tuple[str, ...] = (
    "chart",
    "charts",
    "record",
    "records",
    "file",
    "files",
    "history",
    "list",
    "lists",
    "data",
    "note",
    "notes",
    "report",
    "reports",
    "document",
    "documents",
)

# Record-*type* nouns: names for a KIND of study/procedure/document the chart
# stores (a colonoscopy report, an imaging study, a biopsy result). These are
# data categories — "is there a record of X?" — not clinical *findings* ("signs
# of infection", "improvement", "malignancy"), which name a patient state and
# are never in this set. The distinction is the whole point of the absence-object
# constraint: "No colonoscopy is on record" is coverage (a record type), while
# "No evidence of malignancy was found" is a diagnostic conclusion (a finding).
_RECORD_TYPE_NOUNS: tuple[str, ...] = (
    "colonoscopy",
    "endoscopy",
    "mammogram",
    "biopsy",
    "imaging",
    "screening",
    "screenings",
    "scan",
    "scans",
    "ultrasound",
    "x-ray",
    "xray",
    "ekg",
    "ecg",
    "mri",
    "ct",
    "study",
    "studies",
    "procedure",
    "procedures",
    "vaccine",
    "vaccines",
    "vaccination",
    "vaccinations",
    "immunization",
    "immunizations",
)


def _build_data_category_nouns() -> frozenset[str]:
    nouns: set[str] = set(_GENERIC_DATA_NOUNS) | set(_RECORD_TYPE_NOUNS)
    for category in SNAPSHOT_CATEGORIES:
        nouns.add(category)  # raw tool token — auto-included, cannot drift out
        nouns.update(_CATEGORY_SYNONYMS.get(category, ()))
    return frozenset(nouns)


#: The single constant of admissible coverage/absence object nouns.
DATA_CATEGORY_NOUNS: frozenset[str] = _build_data_category_nouns()

# Grammatical filler permitted around category nouns in a coverage object.
# Deliberately excludes clause/predicate words ("that", "is", "has", "was",
# pronoun subjects other than possessives) so a proposition never passes.
_COVERAGE_FILLER: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "my",
        "our",
        "your",
        "her",
        "his",
        "their",
        "its",
        "this",
        "these",
        "those",
        "patient",
        "patients",
        "and",
        "plus",
        "also",
        "as",
        "well",
        "for",
    }
)

# Retrieval verbs (multi-word first). These describe *reading* data — a process,
# not an assertion. "verified"/"confirmed" are intentionally absent.
_RETRIEVAL_VERBS: tuple[str, ...] = (
    "looked at",
    "looked through",
    "looked over",
    "went through",
    "went over",
    "pulled up",
    "checked",
    "reviewed",
    "searched",
    "examined",
    "pulled",
    "queried",
)

# Absence markers ("no <object> <marker>"). The object constraint (not the
# marker) does the finding-vs-category discrimination, so the full ticket marker
# list — including bare "found"/"recorded"/"documented" — is admitted here:
# "No evidence of malignancy was found" strips because "evidence of malignancy"
# is not a data category, not because "found" was disallowed.
_RECORD_MARKERS: tuple[str, ...] = (
    "on record",
    "on file",
    "recorded",
    "documented",
    "found",
    "in the record",
    "in the records",
    "in the chart",
    "in the charts",
    "in the file",
)

# Combined marker matcher (longest alternatives first), used to locate where the
# absence object ends.
_ABSENCE_MARKER_RE = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(m) for m in sorted(_RECORD_MARKERS, key=len, reverse=True)
    )
    + r")\b"
)

# Linking/auxiliary verbs that sit between an absence object and its marker
# ("No allergies ARE on record", "No improvement IS recorded"). Trimmed from the
# object's tail before it is checked against the data-category constraint.
_LINKING_TRAILERS: frozenset[str] = frozenset(
    {
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "has",
        "have",
        "had",
        "ever",
        "currently",
    }
)

_REFUSAL_PATTERNS: tuple[str, ...] = (
    "couldn't verify",
    "could not verify",
    "cannot verify",
    "can't verify",
    "couldn't confirm",
    "could not confirm",
    "cannot confirm",
    "can't confirm",
    "unable to verify",
    "unable to confirm",
    "wasn't able to verify",
    "was not able to verify",
)

_GREETING_PATTERNS: tuple[str, ...] = (
    "hello",
    "hi ",
    "hi,",
    "hey",
    "good morning",
    "good afternoon",
    "good evening",
    "here is what i",
    "here's what i",
    "here is a summary",
    "here's a summary",
    "thanks",
    "thank you",
)

_NAV_VERBS: tuple[str, ...] = ("view", "see", "open", "navigate to", "go to")
_NAV_TARGETS: tuple[str, ...] = ("record", "records", "chart", "source")

# A period that is not between two digits, or a newline, terminates a sentence.
_SENTENCE_RE = re.compile(r"[^.\n]+[.\n]?")
_CITATION_RE = re.compile(r"\[([A-Za-z]+)/([^\[\]/\s]+)\]")
_DECIMAL_POINT_RE = re.compile(r"(?<=\d)\.(?=\d)")
_DECIMAL_SENTINEL = "\x00"

_LEADING_SUBJECT_RE = re.compile(
    r"^(?:then\s+)?(?:i|we)(?:'ve| have| just| already)?\s+"
)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def _segment(text: str) -> list[str]:
    """Split into sentences, keeping each sentence's original terminator.

    Periods inside a decimal (``5.8``) do not split; deterministic and
    documented beats clever (T008 hint).
    """
    protected = _DECIMAL_POINT_RE.sub(_DECIMAL_SENTINEL, text)
    segments: list[str] = []
    for match in _SENTENCE_RE.finditer(protected):
        raw = match.group(0).replace(_DECIMAL_SENTINEL, ".").strip()
        if raw:
            segments.append(raw)
    return segments


# ---------------------------------------------------------------------------
# Scaffolding whitelist
# ---------------------------------------------------------------------------


def _tokenize_object(phrase: str) -> list[str]:
    """Lowercase word tokens of a coverage object, dropping possessive ``'s``."""
    tokens: list[str] = []
    for raw in re.split(r"[\s,]+", phrase.strip().lower()):
        word = raw.strip("().;:'\"")
        if word.endswith("'s"):
            word = word[:-2]
        if word:
            tokens.append(word)
    return tokens


def _object_is_pure_data_category(tokens: list[str]) -> bool:
    """The object must be data categories + filler, with at least one category.

    Any clause/predicate/finding token (``that``, ``is``, ``hypertensive``,
    ``signs``, ``improvement``, …) is neither a category noun nor filler, so its
    presence rejects the object. Shared by the coverage and absence branches so
    both constrain their object the same way.
    """
    if not tokens:
        return False
    has_category = False
    for tok in tokens:
        if tok in DATA_CATEGORY_NOUNS:
            has_category = True
        elif tok not in _COVERAGE_FILLER:
            return False
    return has_category


def _is_coverage(lowered: str) -> bool:
    """Retrieval verb over a pure data-category object (§5 coverage rule)."""
    remainder = _LEADING_SUBJECT_RE.sub("", lowered, count=1).lstrip()
    for verb in _RETRIEVAL_VERBS:
        if remainder == verb or remainder.startswith(verb + " "):
            tokens = _tokenize_object(remainder[len(verb):])
            return _object_is_pure_data_category(tokens)
    return False


_ABSENCE_PREFIXES: tuple[str, ...] = (
    "there is no ",
    "there are no ",
    "no known ",
    "no ",
)


def _is_absence(lowered: str) -> bool:
    """"no <data category> <record-marker>": absence of a *record*.

    Constrains the OBJECT, not just the marker: the absent thing must be a data
    category (a record/study/data type), never a clinical finding. "No
    colonoscopy is on record" is coverage; "No signs of infection were
    documented" / "No evidence of malignancy was found" name findings and are
    stripped when uncited.
    """
    prefix = next((p for p in _ABSENCE_PREFIXES if lowered.startswith(p)), None)
    if prefix is None:
        return False
    remainder = lowered[len(prefix):]
    marker = _ABSENCE_MARKER_RE.search(remainder)
    if marker is None:
        return False
    tokens = _tokenize_object(remainder[: marker.start()])
    while tokens and tokens[-1] in _LINKING_TRAILERS:
        tokens.pop()
    return _object_is_pure_data_category(tokens)


def _is_refusal(lowered: str) -> bool:
    return any(pattern in lowered for pattern in _REFUSAL_PATTERNS)


def _is_greeting(lowered: str) -> bool:
    return any(lowered.startswith(pattern) for pattern in _GREETING_PATTERNS)


def _is_navigation(lowered: str) -> bool:
    if not any(re.search(rf"\b{re.escape(v)}\b", lowered) for v in _NAV_VERBS):
        return False
    return any(re.search(rf"\b{t}\b", lowered) for t in _NAV_TARGETS)


def _is_section_header(sentence: str) -> bool:
    return sentence.rstrip().endswith(":")


def _is_scaffolding(sentence: str) -> bool:
    lowered = sentence.strip().lower()
    return (
        _is_greeting(lowered)
        or _is_section_header(sentence)
        or _is_refusal(lowered)
        or _is_navigation(lowered)
        or _is_coverage(lowered)
        or _is_absence(lowered)
    )


# ---------------------------------------------------------------------------
# Citation verification
# ---------------------------------------------------------------------------


def _classify_claim(
    sentence: str,
    by_type_id: set[tuple[str, str]],
    by_id: dict[str, set[str]],
) -> tuple[ClaimStatus, ResourceRef | None]:
    """Verify every citation in a claim; the claim is only as strong as its
    weakest cited resource (a fabricated citation strips the whole sentence)."""
    matched_ref: ResourceRef | None = None
    type_mismatch = False
    for type_str, id_str in _CITATION_RE.findall(sentence):
        if (type_str, id_str) in by_type_id:
            if matched_ref is None:
                matched_ref = ResourceRef(
                    resource_type=type_str, resource_id=id_str
                )
            continue
        # This cited resource is absent under the cited type.
        if id_str in by_id:  # present, but under a different type
            type_mismatch = True
        else:
            return ClaimStatus.STRIPPED_UNKNOWN_CITATION, None
    if type_mismatch:
        return ClaimStatus.STRIPPED_TYPE_MISMATCH, None
    return ClaimStatus.VERIFIED, matched_ref


_STRIP_REASONS: dict[ClaimStatus, str] = {
    ClaimStatus.STRIPPED_UNCITED: (
        "clinical claim stated without a [ResourceType/id] citation"
    ),
    ClaimStatus.STRIPPED_UNKNOWN_CITATION: (
        "cited a resource that was not in this request's tool results"
    ),
    ClaimStatus.STRIPPED_TYPE_MISMATCH: (
        "cited a resource id under a resource type it was not returned as"
    ),
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def verify_response(
    draft: str,
    refs: Iterable[ResourceRef],
) -> VerificationVerdict:
    """Verify a draft against the refs this request's tool calls returned.

    Pure and deterministic: no I/O, no clock, no network, no LLM. See the module
    docstring for the classification and fail-closed rules.
    """
    available_refs = tuple(refs)
    by_type_id = {
        (r.resource_type.value, r.resource_id) for r in available_refs
    }
    by_id: dict[str, set[str]] = {}
    for r in available_refs:
        by_id.setdefault(r.resource_id, set()).add(r.resource_type.value)

    verdicts: list[ClaimVerdict] = []
    stripped: list[StrippedClaim] = []
    kept_sentences: list[str] = []
    passed = 0
    stripped_count = 0

    for sentence in _segment(draft):
        has_citation = _CITATION_RE.search(sentence) is not None

        if not has_citation:
            if _is_scaffolding(sentence):
                verdicts.append(
                    ClaimVerdict(status=ClaimStatus.SCAFFOLDING, text=sentence)
                )
                kept_sentences.append(sentence)
                continue
            # Uncited clinical claim — fail closed.
            reason = _STRIP_REASONS[ClaimStatus.STRIPPED_UNCITED]
            verdicts.append(
                ClaimVerdict(
                    status=ClaimStatus.STRIPPED_UNCITED,
                    text=sentence,
                    reason=reason,
                )
            )
            stripped.append(
                StrippedClaim(
                    text=sentence,
                    reason=reason,
                    status=ClaimStatus.STRIPPED_UNCITED,
                )
            )
            stripped_count += 1
            continue

        status, matched_ref = _classify_claim(sentence, by_type_id, by_id)
        if status is ClaimStatus.VERIFIED:
            verdicts.append(
                ClaimVerdict(
                    status=status, text=sentence, ref=matched_ref
                )
            )
            kept_sentences.append(sentence)
            passed += 1
        else:
            reason = _STRIP_REASONS[status]
            verdicts.append(
                ClaimVerdict(status=status, text=sentence, reason=reason)
            )
            stripped.append(
                StrippedClaim(text=sentence, reason=reason, status=status)
            )
            stripped_count += 1

    counts = VerificationCounts(
        claims_total=passed + stripped_count,
        claims_passed=passed,
        claims_stripped=stripped_count,
    )
    content_removed = stripped_count > 0
    # Fail closed only when no claim-bearing sentence survived AND something was
    # stripped. Scaffolding is never counted, so it can never flip this.
    fallback_triggered = passed == 0 and stripped_count > 0

    if fallback_triggered:
        output_text = FALLBACK_TEXT
    else:
        output_text = " ".join(kept_sentences)
        if content_removed:
            output_text = f"{output_text} {CONTENT_REMOVED_MARKER}".strip()

    return VerificationVerdict(
        output_text=output_text,
        verdicts=tuple(verdicts),
        stripped=tuple(stripped),
        counts=counts,
        available_refs=available_refs,
        content_removed=content_removed,
        fallback_triggered=fallback_triggered,
    )
