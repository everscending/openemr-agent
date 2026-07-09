"""T002: contract tests for tool I/O, citations, and coverage models.

Access pattern: `copilot.contracts` is imported at module level (so collection
always succeeds), and individual model classes are resolved dynamically inside
each test. Before implementation every test fails with AttributeError on the
missing name — an unambiguous "not built yet" signal.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from copilot import contracts

AWARE = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
NAIVE = datetime(2026, 7, 1, 12, 0, 0)

ALL_RESOURCE_TYPES = [
    "Patient",
    "MedicationRequest",
    "Condition",
    "AllergyIntolerance",
    "Observation",
    "Encounter",
    "DocumentReference",
    "Immunization",
]


def make_ref(resource_type: str = "Observation", resource_id: str = "obs-1"):
    return contracts.ResourceRef(
        resource_type=resource_type, resource_id=resource_id
    )


# --------------------------------------------------------------------------
# Sample-instance builders (used by round-trip / frozen / embedding tests).
# Deferred via lambdas so that pre-implementation failure is AttributeError
# inside the test body, not a collection error.
# --------------------------------------------------------------------------


def build_patient_record():
    return contracts.PatientRecord(
        ref=make_ref("Patient", "pat-1"),
        name="Jane Doe",
        birth_date=date(1980, 1, 15),
    )


def build_condition_record():
    return contracts.ConditionRecord(
        ref=make_ref("Condition", "cond-1"), display="Hypertension"
    )


def build_allergy_record():
    return contracts.AllergyRecord(
        ref=make_ref("AllergyIntolerance", "alg-1"), display="Penicillin"
    )


def build_observation_record():
    return contracts.ObservationRecord(
        ref=make_ref("Observation", "obs-1"),
        code="8480-6",
        display="Systolic blood pressure",
        value="128 mmHg",
        effective=AWARE,
    )


def build_medication_record():
    return contracts.MedicationRecord(
        ref=make_ref("MedicationRequest", "med-1"),
        medication="Lisinopril 10mg",
        status="active",
    )


def build_encounter_record():
    return contracts.EncounterRecord(
        ref=make_ref("Encounter", "enc-1"),
        start=AWARE,
        reason="Annual physical",
    )


def build_document_record():
    return contracts.DocumentRecord(
        ref=make_ref("DocumentReference", "doc-1"),
        title="Discharge summary",
        created=AWARE,
    )


def build_immunization_record():
    return contracts.ImmunizationRecord(
        ref=make_ref("Immunization", "imm-1"),
        vaccine="Influenza, seasonal",
        administered=AWARE,
    )


RECORD_BUILDERS = [
    pytest.param(build_patient_record, id="PatientRecord"),
    pytest.param(build_condition_record, id="ConditionRecord"),
    pytest.param(build_allergy_record, id="AllergyRecord"),
    pytest.param(build_observation_record, id="ObservationRecord"),
    pytest.param(build_medication_record, id="MedicationRecord"),
    pytest.param(build_encounter_record, id="EncounterRecord"),
    pytest.param(build_document_record, id="DocumentRecord"),
    pytest.param(build_immunization_record, id="ImmunizationRecord"),
]


def build_snapshot_output():
    return contracts.PatientSnapshotOutput(
        patient=build_patient_record(),
        conditions=(build_condition_record(),),
        allergies=(build_allergy_record(),),
    )


def build_observations_output():
    return contracts.SearchObservationsOutput(
        records=(build_observation_record(),)
    )


def build_medications_output():
    return contracts.GetMedicationHistoryOutput(
        records=(build_medication_record(),)
    )


def build_encounters_output():
    return contracts.GetEncountersSinceOutput(
        records=(build_encounter_record(),)
    )


def build_documents_output():
    return contracts.SearchDocumentsOutput(records=(build_document_record(),))


def build_immunizations_output():
    return contracts.GetImmunizationsOutput(
        records=(build_immunization_record(),)
    )


OUTPUT_BUILDERS = [
    pytest.param(build_snapshot_output, id="PatientSnapshotOutput"),
    pytest.param(build_observations_output, id="SearchObservationsOutput"),
    pytest.param(build_medications_output, id="GetMedicationHistoryOutput"),
    pytest.param(build_encounters_output, id="GetEncountersSinceOutput"),
    pytest.param(build_documents_output, id="SearchDocumentsOutput"),
    pytest.param(build_immunizations_output, id="GetImmunizationsOutput"),
]

# (input model name, extra required kwargs beyond patient_id)
INPUT_SPECS = [
    pytest.param("GetPatientSnapshotInput", {}, id="get_patient_snapshot"),
    pytest.param("SearchObservationsInput", {}, id="search_observations"),
    pytest.param("GetMedicationHistoryInput", {}, id="get_medication_history"),
    pytest.param(
        "GetEncountersSinceInput", {"since": AWARE}, id="get_encounters_since"
    ),
    pytest.param("SearchDocumentsInput", {}, id="search_documents"),
    pytest.param("GetImmunizationsInput", {}, id="get_immunizations"),
]


def build_input_instances():
    return [
        contracts.GetPatientSnapshotInput(patient_id="pat-1"),
        contracts.SearchObservationsInput(
            patient_id="pat-1", code="8480-6", start=AWARE - timedelta(days=30), end=AWARE
        ),
        contracts.GetMedicationHistoryInput(patient_id="pat-1"),
        contracts.GetEncountersSinceInput(patient_id="pat-1", since=AWARE),
        contracts.SearchDocumentsInput(
            patient_id="pat-1", query="discharge", start=AWARE - timedelta(days=7), end=AWARE
        ),
        contracts.GetImmunizationsInput(patient_id="pat-1"),
    ]


def build_coverage_instances():
    return [
        contracts.CoverageOk(category="medications", record_count=4),
        contracts.CoverageVerifiedEmpty(
            category="immunizations",
            query_description="Immunization?patient=pat-1",
            scope="all immunization records for patient pat-1",
            timestamp=AWARE,
        ),
        contracts.CoverageUnavailable(
            category="documents", reason="FHIR endpoint returned HTTP 500"
        ),
    ]


# ==========================================================================
# Criterion 1 — one input model per tool; patient_id required; date-range
# inputs reject start > end.
# ==========================================================================


@pytest.mark.parametrize(("model_name", "extra"), INPUT_SPECS)
def test_input_model_requires_patient_id(model_name, extra):
    model = getattr(contracts, model_name)
    with pytest.raises(ValidationError):
        model(**extra)


@pytest.mark.parametrize(("model_name", "extra"), INPUT_SPECS)
def test_input_model_rejects_empty_patient_id(model_name, extra):
    model = getattr(contracts, model_name)
    with pytest.raises(ValidationError):
        model(patient_id="", **extra)


@pytest.mark.parametrize(("model_name", "extra"), INPUT_SPECS)
def test_input_model_accepts_valid_patient_id(model_name, extra):
    model = getattr(contracts, model_name)
    instance = model(patient_id="pat-1", **extra)
    assert instance.patient_id == "pat-1"


@pytest.mark.parametrize(
    "model_name", ["SearchObservationsInput", "SearchDocumentsInput"]
)
def test_date_range_input_rejects_start_after_end(model_name):
    model = getattr(contracts, model_name)
    with pytest.raises(ValidationError):
        model(patient_id="pat-1", start=AWARE, end=AWARE - timedelta(days=1))


@pytest.mark.parametrize(
    "model_name", ["SearchObservationsInput", "SearchDocumentsInput"]
)
def test_date_range_input_accepts_valid_range(model_name):
    model = getattr(contracts, model_name)
    instance = model(
        patient_id="pat-1", start=AWARE - timedelta(days=1), end=AWARE
    )
    assert instance.start < instance.end


def test_encounters_since_rejects_naive_datetime():
    with pytest.raises(ValidationError):
        contracts.GetEncountersSinceInput(patient_id="pat-1", since=NAIVE)


@pytest.mark.parametrize(
    "model_name", ["SearchObservationsInput", "SearchDocumentsInput"]
)
def test_date_range_input_rejects_naive_datetimes(model_name):
    model = getattr(contracts, model_name)
    with pytest.raises(ValidationError):
        model(patient_id="pat-1", start=NAIVE, end=AWARE)


# ==========================================================================
# Criterion 2 — ResourceRef: FHIR resource-type enum, non-empty id, citation
# token render + parse round-trip, embedded in every output record.
# ==========================================================================


def test_resource_type_enum_covers_all_fhir_types():
    for value in ALL_RESOURCE_TYPES:
        member = contracts.FhirResourceType(value)
        assert member.value == value


def test_resource_type_enum_rejects_unknown_type():
    with pytest.raises(ValueError):
        contracts.FhirResourceType("Basic")


def test_resource_ref_rejects_empty_resource_id():
    with pytest.raises(ValidationError):
        contracts.ResourceRef(resource_type="Patient", resource_id="")


def test_resource_ref_rejects_unknown_resource_type():
    with pytest.raises(ValidationError):
        contracts.ResourceRef(resource_type="Basic", resource_id="x-1")


def test_resource_ref_citation_token_format():
    ref = make_ref("Observation", "obs-42")
    assert ref.citation_token == "[Observation/obs-42]"


def test_resource_ref_parse_citation_token():
    parsed = contracts.ResourceRef.parse_citation_token("[Encounter/enc-7]")
    assert parsed.resource_type == contracts.FhirResourceType("Encounter")
    assert parsed.resource_id == "enc-7"


@pytest.mark.parametrize("resource_type", ALL_RESOURCE_TYPES)
def test_resource_ref_citation_round_trip(resource_type):
    ref = make_ref(resource_type, "id-123")
    assert contracts.ResourceRef.parse_citation_token(ref.citation_token) == ref


@pytest.mark.parametrize(
    "token",
    [
        "Observation/obs-1",  # missing brackets
        "[Observation]",  # missing id
        "[Basic/x-1]",  # unknown resource type
        "[Observation/]",  # empty id
        "",
    ],
)
def test_resource_ref_parse_rejects_malformed_token(token):
    with pytest.raises(ValueError):
        contracts.ResourceRef.parse_citation_token(token)


@pytest.mark.parametrize("builder", RECORD_BUILDERS)
def test_every_output_record_embeds_resource_ref(builder):
    record = builder()
    assert isinstance(record.ref, contracts.ResourceRef)


def test_output_record_requires_resource_ref():
    with pytest.raises(ValidationError):
        contracts.ObservationRecord(
            code="8480-6", display="Systolic blood pressure"
        )


# ==========================================================================
# Criterion 3 — CategoryCoverage discriminated union: ok / verified_empty /
# unavailable; unavailable can never carry records.
# ==========================================================================


def test_coverage_ok_carries_record_count():
    cov = contracts.CoverageOk(category="medications", record_count=4)
    assert cov.status == "ok"
    assert cov.record_count == 4


def test_coverage_verified_empty_carries_query_receipt():
    cov = contracts.CoverageVerifiedEmpty(
        category="immunizations",
        query_description="Immunization?patient=pat-1",
        scope="all immunization records for patient pat-1",
        timestamp=AWARE,
    )
    assert cov.status == "verified_empty"
    assert cov.query_description == "Immunization?patient=pat-1"
    assert cov.scope == "all immunization records for patient pat-1"
    assert cov.timestamp == AWARE


def test_coverage_verified_empty_requires_receipt_fields():
    with pytest.raises(ValidationError):
        contracts.CoverageVerifiedEmpty(category="immunizations")


def test_coverage_verified_empty_rejects_naive_timestamp():
    with pytest.raises(ValidationError):
        contracts.CoverageVerifiedEmpty(
            category="immunizations",
            query_description="Immunization?patient=pat-1",
            scope="all immunization records for patient pat-1",
            timestamp=NAIVE,
        )


def test_coverage_unavailable_requires_reason():
    with pytest.raises(ValidationError):
        contracts.CoverageUnavailable(category="documents")


@pytest.mark.parametrize(
    "records_kwargs",
    [
        {"record_count": 3},
        {"records": ({"resource_id": "doc-1"},)},
    ],
    ids=["record_count", "records"],
)
def test_coverage_unavailable_cannot_carry_records(records_kwargs):
    with pytest.raises(ValidationError):
        contracts.CoverageUnavailable(
            category="documents", reason="HTTP 500", **records_kwargs
        )


def test_coverage_union_cannot_be_unavailable_with_records():
    adapter = TypeAdapter(contracts.CategoryCoverage)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "status": "unavailable",
                "category": "documents",
                "reason": "HTTP 500",
                "record_count": 3,
            }
        )


def test_coverage_discriminated_union_parses_by_status():
    adapter = TypeAdapter(contracts.CategoryCoverage)
    ok = adapter.validate_python(
        {"status": "ok", "category": "medications", "record_count": 2}
    )
    assert isinstance(ok, contracts.CoverageOk)
    empty = adapter.validate_python(
        {
            "status": "verified_empty",
            "category": "immunizations",
            "query_description": "Immunization?patient=pat-1",
            "scope": "all immunization records for patient pat-1",
            "timestamp": "2026-07-01T12:00:00+00:00",
        }
    )
    assert isinstance(empty, contracts.CoverageVerifiedEmpty)
    unavailable = adapter.validate_python(
        {"status": "unavailable", "category": "documents", "reason": "HTTP 500"}
    )
    assert isinstance(unavailable, contracts.CoverageUnavailable)


def test_coverage_discriminated_union_rejects_unknown_status():
    adapter = TypeAdapter(contracts.CategoryCoverage)
    with pytest.raises(ValidationError):
        adapter.validate_python({"status": "partial", "category": "documents"})


# ==========================================================================
# Criterion 4 — missing required field / wrong type raises ValidationError;
# at least one representative case per tool (inputs covered above too).
# ==========================================================================


@pytest.mark.parametrize(
    ("model_name", "bad_kwargs"),
    [
        pytest.param(
            "GetPatientSnapshotInput",
            {"patient_id": 123},
            id="get_patient_snapshot-wrong-type",
        ),
        pytest.param(
            "SearchObservationsInput",
            {"patient_id": "pat-1", "start": "not-a-datetime"},
            id="search_observations-wrong-type",
        ),
        pytest.param(
            "GetMedicationHistoryInput",
            {"patient_id": ["pat-1"]},
            id="get_medication_history-wrong-type",
        ),
        pytest.param(
            "GetEncountersSinceInput",
            {"patient_id": "pat-1", "since": "not-a-datetime"},
            id="get_encounters_since-wrong-type",
        ),
        pytest.param(
            "SearchDocumentsInput",
            {"patient_id": {"id": "pat-1"}},
            id="search_documents-wrong-type",
        ),
        pytest.param(
            "GetImmunizationsInput",
            {"patient_id": 3.14},
            id="get_immunizations-wrong-type",
        ),
    ],
)
def test_input_model_rejects_wrong_types(model_name, bad_kwargs):
    model = getattr(contracts, model_name)
    with pytest.raises(ValidationError):
        model(**bad_kwargs)


@pytest.mark.parametrize(
    ("model_name", "bad_kwargs"),
    [
        pytest.param(
            "PatientSnapshotOutput",
            {"patient": "not-a-record"},
            id="get_patient_snapshot-output",
        ),
        pytest.param(
            "SearchObservationsOutput",
            {"records": 123},
            id="search_observations-output",
        ),
        pytest.param(
            "GetMedicationHistoryOutput",
            {"records": ("not-a-record",)},
            id="get_medication_history-output",
        ),
        pytest.param(
            "GetEncountersSinceOutput",
            {"records": "not-a-sequence-of-records"},
            id="get_encounters_since-output",
        ),
        pytest.param(
            "SearchDocumentsOutput",
            {"records": ({"title": "missing ref"},)},
            id="search_documents-output",
        ),
        pytest.param(
            "GetImmunizationsOutput",
            {},  # records is required
            id="get_immunizations-output",
        ),
    ],
)
def test_output_model_rejects_missing_or_wrong_types(model_name, bad_kwargs):
    model = getattr(contracts, model_name)
    with pytest.raises(ValidationError):
        model(**bad_kwargs)


# ==========================================================================
# Criterion 5 — all models frozen; lossless JSON round-trip.
# ==========================================================================


def all_sample_instances():
    instances = [make_ref()]
    instances.extend(builder.values[0]() for builder in RECORD_BUILDERS)
    instances.extend(builder.values[0]() for builder in OUTPUT_BUILDERS)
    instances.extend(build_input_instances())
    instances.extend(build_coverage_instances())
    return instances


def test_all_models_are_frozen():
    checked = 0
    for instance in all_sample_instances():
        field_name = next(iter(type(instance).model_fields))
        with pytest.raises(ValidationError):
            setattr(instance, field_name, "mutated")
        checked += 1
    assert checked >= 22  # ref + 8 records + 6 outputs + 6 inputs + 3 coverage


def test_all_models_json_round_trip_losslessly():
    checked = 0
    for instance in all_sample_instances():
        restored = type(instance).model_validate_json(
            instance.model_dump_json()
        )
        assert restored == instance
        assert restored.model_dump() == instance.model_dump()
        checked += 1
    assert checked >= 22
