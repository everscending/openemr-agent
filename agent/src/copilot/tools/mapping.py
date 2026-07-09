"""Shared FHIR resource -> typed-record mapping (T005 / T006).

One home for Bundle-entry extraction and per-resource-type record
mapping, shared by the snapshot tool (T005) and the targeted retrieval
tools (T006) so the mapping logic is never duplicated. Mapping is
best-effort and lenient: a resource missing the fields a record requires
maps to ``None`` and is skipped, never raised.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Iterable, TypeVar

from copilot.contracts.refs import FhirResourceType, ResourceRef
from copilot.contracts.tools import (
    AllergyRecord,
    ConditionRecord,
    DocumentRecord,
    EncounterRecord,
    ImmunizationRecord,
    MedicationRecord,
    ObservationRecord,
    OutputRecord,
    PatientRecord,
)

MEDICATION_SOURCE_PRESCRIPTIONS = "prescriptions"

RecordT = TypeVar("RecordT", bound=OutputRecord)


def extract_records(
    resources: Iterable[dict[str, Any]],
    mapper: Callable[[dict[str, Any]], RecordT | None],
) -> tuple[RecordT, ...]:
    """Map Bundle-entry resources to typed records, skipping unmappable ones."""
    return tuple(
        record
        for record in (mapper(resource) for resource in resources)
        if record is not None
    )


# ---------------------------------------------------------------------------
# Field-level helpers
# ---------------------------------------------------------------------------


def resource_id(resource: dict[str, Any]) -> str | None:
    """The resource's ``id``, if present and non-empty."""
    value = resource.get("id")
    if isinstance(value, str) and value:
        return value
    return None


def codeable_text(concept: Any) -> str | None:
    """Best-effort display text from a FHIR CodeableConcept."""
    if not isinstance(concept, dict):
        return None
    text = concept.get("text")
    if isinstance(text, str) and text:
        return text
    codings = concept.get("coding")
    if isinstance(codings, list):
        for coding in codings:
            if isinstance(coding, dict):
                display = coding.get("display")
                if isinstance(display, str) and display:
                    return display
    return None


def codeable_code(concept: Any) -> str | None:
    """First non-empty coding code from a FHIR CodeableConcept."""
    if not isinstance(concept, dict):
        return None
    codings = concept.get("coding")
    if isinstance(codings, list):
        for coding in codings:
            if isinstance(coding, dict):
                code = coding.get("code")
                if isinstance(code, str) and code:
                    return code
    return None


def parse_aware_datetime(value: Any) -> datetime | None:
    """Parse an ISO datetime string; naive or malformed values map to None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def human_name(names: Any) -> str | None:
    """Best-effort display name from a FHIR HumanName list."""
    if not isinstance(names, list):
        return None
    for name in names:
        if not isinstance(name, dict):
            continue
        text = name.get("text")
        if isinstance(text, str) and text:
            return text
        given = name.get("given")
        given_parts = (
            [part for part in given if isinstance(part, str) and part]
            if isinstance(given, list)
            else []
        )
        family = name.get("family")
        parts = given_parts + (
            [family] if isinstance(family, str) and family else []
        )
        if parts:
            return " ".join(parts)
    return None


# ---------------------------------------------------------------------------
# Resource -> record mappers
# ---------------------------------------------------------------------------


def patient_record_from(resource: dict[str, Any]) -> PatientRecord | None:
    rid = resource_id(resource)
    if rid is None:
        return None
    name = human_name(resource.get("name")) or "Unknown"
    birth_raw = resource.get("birthDate")
    birth_date: date | None = None
    if isinstance(birth_raw, str) and birth_raw:
        try:
            birth_date = date.fromisoformat(birth_raw)
        except ValueError:
            birth_date = None
    return PatientRecord(
        ref=ResourceRef(resource_type=FhirResourceType.PATIENT, resource_id=rid),
        name=name,
        birth_date=birth_date,
    )


def medication_record_from(
    resource: dict[str, Any], *, source: str
) -> MedicationRecord | None:
    rid = resource_id(resource)
    if rid is None:
        return None
    medication = codeable_text(resource.get("medicationCodeableConcept"))
    if medication is None:
        return None
    status = resource.get("status")
    return MedicationRecord(
        ref=ResourceRef(
            resource_type=FhirResourceType.MEDICATION_REQUEST, resource_id=rid
        ),
        medication=medication,
        status=status if isinstance(status, str) and status else None,
        source=source,
        rxnorm=rxnorm_code(resource.get("medicationCodeableConcept")),
    )


def rxnorm_code(concept: Any) -> str | None:
    """Extract the RxNorm code from a FHIR CodeableConcept, if coded.

    Only a coding whose ``system`` names RxNorm counts — free-text-only
    concepts (AUDIT.md D3) yield ``None`` so downstream reconciliation can
    mark the medication uncoded.
    """
    if not isinstance(concept, dict):
        return None
    codings = concept.get("coding")
    if not isinstance(codings, list):
        return None
    for coding in codings:
        if not isinstance(coding, dict):
            continue
        system = coding.get("system")
        code = coding.get("code")
        if (
            isinstance(system, str)
            and "rxnorm" in system.lower()
            and isinstance(code, str)
            and code
        ):
            return code
    return None


def condition_record_from(resource: dict[str, Any]) -> ConditionRecord | None:
    rid = resource_id(resource)
    if rid is None:
        return None
    display = codeable_text(resource.get("code"))
    if display is None:
        return None
    return ConditionRecord(
        ref=ResourceRef(resource_type=FhirResourceType.CONDITION, resource_id=rid),
        display=display,
    )


def allergy_record_from(resource: dict[str, Any]) -> AllergyRecord | None:
    rid = resource_id(resource)
    if rid is None:
        return None
    display = codeable_text(resource.get("code"))
    if display is None:
        return None
    return AllergyRecord(
        ref=ResourceRef(
            resource_type=FhirResourceType.ALLERGY_INTOLERANCE, resource_id=rid
        ),
        display=display,
    )


def observation_record_from(resource: dict[str, Any]) -> ObservationRecord | None:
    rid = resource_id(resource)
    if rid is None:
        return None
    code = codeable_code(resource.get("code")) or codeable_text(resource.get("code"))
    display = codeable_text(resource.get("code"))
    if code is None or display is None:
        return None
    return ObservationRecord(
        ref=ResourceRef(resource_type=FhirResourceType.OBSERVATION, resource_id=rid),
        code=code,
        display=display,
        value=observation_value(resource),
        effective=parse_aware_datetime(resource.get("effectiveDateTime")),
    )


def observation_value(resource: dict[str, Any]) -> str | None:
    quantity = resource.get("valueQuantity")
    if isinstance(quantity, dict):
        value = quantity.get("value")
        unit = quantity.get("unit")
        if isinstance(value, (int, float)):
            if isinstance(unit, str) and unit:
                return f"{value} {unit}"
            return str(value)
    value_string = resource.get("valueString")
    if isinstance(value_string, str) and value_string:
        return value_string
    return None


def encounter_record_from(resource: dict[str, Any]) -> EncounterRecord | None:
    rid = resource_id(resource)
    if rid is None:
        return None
    period = resource.get("period")
    start = (
        parse_aware_datetime(period.get("start"))
        if isinstance(period, dict)
        else None
    )
    if start is None:
        return None
    reason: str | None = None
    reason_codes = resource.get("reasonCode")
    if isinstance(reason_codes, list):
        for concept in reason_codes:
            reason = codeable_text(concept)
            if reason is not None:
                break
    return EncounterRecord(
        ref=ResourceRef(resource_type=FhirResourceType.ENCOUNTER, resource_id=rid),
        start=start,
        reason=reason,
    )


def document_record_from(resource: dict[str, Any]) -> DocumentRecord | None:
    """Map a DocumentReference to its metadata record (never its content)."""
    rid = resource_id(resource)
    if rid is None:
        return None
    title = _document_title(resource)
    if title is None:
        return None
    return DocumentRecord(
        ref=ResourceRef(
            resource_type=FhirResourceType.DOCUMENT_REFERENCE, resource_id=rid
        ),
        title=title,
        created=parse_aware_datetime(resource.get("date")),
    )


def _document_title(resource: dict[str, Any]) -> str | None:
    description = resource.get("description")
    if isinstance(description, str) and description:
        return description
    type_text = codeable_text(resource.get("type"))
    if type_text is not None:
        return type_text
    contents = resource.get("content")
    if isinstance(contents, list):
        for content in contents:
            if not isinstance(content, dict):
                continue
            attachment = content.get("attachment")
            if isinstance(attachment, dict):
                title = attachment.get("title")
                if isinstance(title, str) and title:
                    return title
    return None


def immunization_record_from(
    resource: dict[str, Any],
) -> ImmunizationRecord | None:
    rid = resource_id(resource)
    if rid is None:
        return None
    vaccine = codeable_text(resource.get("vaccineCode"))
    if vaccine is None:
        return None
    return ImmunizationRecord(
        ref=ResourceRef(
            resource_type=FhirResourceType.IMMUNIZATION, resource_id=rid
        ),
        vaccine=vaccine,
        administered=parse_aware_datetime(resource.get("occurrenceDateTime")),
    )
