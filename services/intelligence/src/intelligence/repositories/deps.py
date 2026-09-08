"""Environment-only construction of Intelligence's read-only Neo4j repository."""

from __future__ import annotations

from intelligence.repositories.neo4j.crm_deal_refs import Neo4jCrmDealRefsRepository
from intelligence.repositories.protocols.crm_deal_refs import CrmDealRefsRepository


def get_crm_deal_refs_repository() -> CrmDealRefsRepository:
    """Create a new repository; it opens its Neo4j connection only in the child process."""
    return Neo4jCrmDealRefsRepository.from_environment()
