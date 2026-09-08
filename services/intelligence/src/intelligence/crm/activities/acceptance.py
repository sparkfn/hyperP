"""Public facade for CRM activity archive acceptance APIs."""

from __future__ import annotations

from intelligence.crm.activities.acceptance_candidates import (
    Attempt,
    PublicationHistory,
    RuntimeReader,
    VerificationHistory,
    accepted_publication,
    publication,
    publication_candidate,
    publication_history,
    read_publication_candidate,
    read_publication_candidates,
    read_verification_candidates,
    status_history,
    verification,
    verification_history,
)
from intelligence.crm.activities.acceptance_descriptor import (
    AcceptanceDescriptor,
    PublicationPointer,
    descriptor_relative_path,
    parse_descriptor,
    publication_descriptor,
    write_publication_descriptor,
)
from intelligence.crm.activities.acceptance_parsing import (
    VerificationCandidate,
    publication_candidate_name,
    verification_candidate_name,
)
from intelligence.crm.activities.acceptance_parsing import (
    parse_publication as _parse_publication,
)
from intelligence.crm.activities.acceptance_parsing import (
    parse_verification as _parse_verification,
)

__all__ = (
    "AcceptanceDescriptor",
    "Attempt",
    "PublicationHistory",
    "PublicationPointer",
    "RuntimeReader",
    "VerificationCandidate",
    "VerificationHistory",
    "accepted_publication",
    "_parse_publication",
    "_parse_verification",
    "descriptor_relative_path",
    "parse_descriptor",
    "publication",
    "publication_candidate",
    "publication_candidate_name",
    "publication_descriptor",
    "publication_history",
    "read_publication_candidate",
    "read_publication_candidates",
    "read_verification_candidates",
    "status_history",
    "verification",
    "verification_candidate_name",
    "verification_history",
    "write_publication_descriptor",
)
