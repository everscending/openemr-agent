"""FHIR resource references and citation tokens.

Every record returned by a tool carries a ``ResourceRef`` so downstream
claims can cite a stable FHIR ID (``[ResourceType/id]``) that is
verifiable against the source system (ARCHITECTURE.md section 5).
"""

import re
from enum import Enum

from pydantic import Field

from copilot.contracts.base import ContractModel


class FhirResourceType(str, Enum):
    """FHIR resource types the copilot tools read."""

    PATIENT = "Patient"
    MEDICATION_REQUEST = "MedicationRequest"
    CONDITION = "Condition"
    ALLERGY_INTOLERANCE = "AllergyIntolerance"
    OBSERVATION = "Observation"
    ENCOUNTER = "Encounter"
    DOCUMENT_REFERENCE = "DocumentReference"
    IMMUNIZATION = "Immunization"


_CITATION_TOKEN_RE = re.compile(
    r"^\[(?P<resource_type>[A-Za-z]+)/(?P<resource_id>[^\[\]/\s]+)\]$"
)


class ResourceRef(ContractModel):
    """A stable reference to a single FHIR resource."""

    resource_type: FhirResourceType
    resource_id: str = Field(min_length=1)

    @property
    def citation_token(self) -> str:
        """Render the citation token, e.g. ``[Observation/obs-1]``."""
        return f"[{self.resource_type.value}/{self.resource_id}]"

    @classmethod
    def parse_citation_token(cls, token: str) -> "ResourceRef":
        """Parse a ``[ResourceType/id]`` token back into a ``ResourceRef``.

        Raises ``ValueError`` for malformed tokens or unknown resource types.
        """
        match = _CITATION_TOKEN_RE.match(token)
        if match is None:
            raise ValueError(f"Malformed citation token: {token!r}")
        return cls(
            resource_type=FhirResourceType(match.group("resource_type")),
            resource_id=match.group("resource_id"),
        )
