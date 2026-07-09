"""Patient-scoped data tools (T005+).

Pure data tools over the read-only FHIR client: no LLM involvement.
"""

from copilot.tools.snapshot import (
    SNAPSHOT_CATEGORIES,
    CategoryFetcher,
    CategoryFetchResult,
    SnapshotFetchers,
    build_default_fetchers,
    get_patient_snapshot,
)
from copilot.tools.targeted import (
    DEFAULT_ENCOUNTERS_CAP,
    get_encounters_since,
    get_immunizations,
    get_medication_history,
    search_documents,
    search_observations,
)

__all__ = [
    "DEFAULT_ENCOUNTERS_CAP",
    "SNAPSHOT_CATEGORIES",
    "CategoryFetcher",
    "CategoryFetchResult",
    "SnapshotFetchers",
    "build_default_fetchers",
    "get_encounters_since",
    "get_immunizations",
    "get_medication_history",
    "get_patient_snapshot",
    "search_documents",
    "search_observations",
]
