"""Concrete tool wiring for the agent loop (T010).

Binds the six read-only tools (ARCHITECTURE.md §2, UC-1..UC-4) to their T002
input contracts and async executors over a :class:`~copilot.fhir.FhirClient`.
This module pulls the FHIR/httpx client, so — deliberately — neither the loop
nor the agent package ``__init__`` imports it; callers import it explicitly.
The loop stays pure and its whole suite runs against fakes with no network.
"""

from __future__ import annotations

from copilot.agent.tools import Tool, ToolRegistry
from copilot.contracts.tools import (
    GetEncountersSinceInput,
    GetImmunizationsInput,
    GetMedicationHistoryInput,
    GetPatientSnapshotInput,
    SearchDocumentsInput,
    SearchObservationsInput,
)
from copilot.fhir import FhirClient
from copilot.tools.snapshot import build_default_fetchers, get_patient_snapshot
from copilot.tools.targeted import (
    get_encounters_since,
    get_immunizations,
    get_medication_history,
    search_documents,
    search_observations,
)


def build_default_registry(client: FhirClient) -> ToolRegistry:
    """Wire the six default tools over ``client`` (whose token is already bound).

    The snapshot tool reuses ``build_default_fetchers`` over the same client, so
    every read presents the requesting user's token (passthrough, no standing
    agent credentials — §4).
    """

    async def snapshot_exec(tool_input: object) -> object:
        assert isinstance(tool_input, GetPatientSnapshotInput)
        return await get_patient_snapshot(
            tool_input.patient_id,
            token="",  # ignored when fetchers are supplied
            fetchers=build_default_fetchers(client),
        )

    async def observations_exec(tool_input: object) -> object:
        assert isinstance(tool_input, SearchObservationsInput)
        return await search_observations(tool_input, client)

    async def medication_history_exec(tool_input: object) -> object:
        assert isinstance(tool_input, GetMedicationHistoryInput)
        return await get_medication_history(tool_input, client)

    async def encounters_exec(tool_input: object) -> object:
        assert isinstance(tool_input, GetEncountersSinceInput)
        return await get_encounters_since(tool_input, client)

    async def documents_exec(tool_input: object) -> object:
        assert isinstance(tool_input, SearchDocumentsInput)
        return await search_documents(tool_input, client)

    async def immunizations_exec(tool_input: object) -> object:
        assert isinstance(tool_input, GetImmunizationsInput)
        return await get_immunizations(tool_input, client)

    return ToolRegistry(
        [
            Tool(
                name="get_patient_snapshot",
                description=(
                    "Fetch this patient's demographics, active medications, "
                    "problems, allergies, recent labs, and last encounter in "
                    "one parallel call. Use to catch up on a chart (UC-1)."
                ),
                input_model=GetPatientSnapshotInput,
                executor=snapshot_exec,  # type: ignore[arg-type]
            ),
            Tool(
                name="search_observations",
                description=(
                    "Search this patient's observations (labs, vitals) by code, "
                    "category, and/or date range, newest first (UC-2)."
                ),
                input_model=SearchObservationsInput,
                executor=observations_exec,  # type: ignore[arg-type]
            ),
            Tool(
                name="get_medication_history",
                description=(
                    "Fetch this patient's active and historical medications, "
                    "each with its status and source list (UC-2)."
                ),
                input_model=GetMedicationHistoryInput,
                executor=medication_history_exec,  # type: ignore[arg-type]
            ),
            Tool(
                name="get_encounters_since",
                description=(
                    "Fetch this patient's encounters on or after a reference "
                    "date, newest first (UC-3)."
                ),
                input_model=GetEncountersSinceInput,
                executor=encounters_exec,  # type: ignore[arg-type]
            ),
            Tool(
                name="search_documents",
                description=(
                    "Search this patient's document references (metadata only, "
                    "never content) by optional date range (UC-4)."
                ),
                input_model=SearchDocumentsInput,
                executor=documents_exec,  # type: ignore[arg-type]
            ),
            Tool(
                name="get_immunizations",
                description="Fetch this patient's immunization records (UC-4).",
                input_model=GetImmunizationsInput,
                executor=immunizations_exec,  # type: ignore[arg-type]
            ),
        ]
    )
