"""Public facade for CRM activity archive acceptance APIs."""

from __future__ import annotations

from intelligence.crm.activities.acceptance_candidates import (
    VerificationCandidate,
    publication,
    publication_candidate,
    publication_candidate_name,
    read_publication_candidate,
    read_publication_candidates,
    read_verification_candidates,
    verification,
    verification_candidate_name,
)
from intelligence.crm.activities.acceptance_candidates import (
    parse_publication as _parse_publication,
)
from intelligence.crm.activities.acceptance_candidates import (
    parse_verification as _parse_verification,
)
from intelligence.crm.activities.acceptance_descriptor import (
    AcceptanceDescriptor,
    PublicationPointer,
    descriptor_relative_path,
    parse_descriptor,
    publication_descriptor,
    write_publication_descriptor,
)

__all__ = (
    "AcceptanceDescriptor",
    "PublicationPointer",
    "VerificationCandidate",
    "_parse_publication",
    "_parse_verification",
    "descriptor_relative_path",
    "parse_descriptor",
    "publication",
    "publication_candidate",
    "publication_candidate_name",
    "publication_descriptor",
    "read_publication_candidate",
    "read_publication_candidates",
    "read_verification_candidates",
    "verification",
    "verification_candidate_name",
    "write_publication_descriptor",
)
