"""FastAPI dependency functions — wire protocol interfaces to active implementations.

To swap a backend, replace the singleton below with a different implementation class.
All injected types are the Protocol interfaces, so route code never imports Neo4j types.
"""

from __future__ import annotations

from src.repositories.neo4j.admin import Neo4jAdminRepository
from src.repositories.neo4j.entity import Neo4jEntityRepository
from src.repositories.neo4j.event import Neo4jEventRepository
from src.repositories.neo4j.ingest import Neo4jIngestRepository
from src.repositories.neo4j.merge import Neo4jMergeRepository
from src.repositories.neo4j.person import Neo4jPersonRepository
from src.repositories.neo4j.report import Neo4jReportRepository
from src.repositories.neo4j.review import Neo4jReviewRepository
from src.repositories.neo4j.sales import Neo4jSalesRepository
from src.repositories.neo4j.survivorship import Neo4jSurvivorshipRepository
from src.repositories.protocols.admin import AdminRepository
from src.repositories.protocols.entity import EntityRepository
from src.repositories.protocols.event import EventRepository
from src.repositories.protocols.ingest import IngestRepository
from src.repositories.protocols.merge import MergeRepository
from src.repositories.protocols.person import PersonRepository
from src.repositories.protocols.report import ReportRepository
from src.repositories.protocols.review import ReviewRepository
from src.repositories.protocols.sales import SalesRepository
from src.repositories.protocols.survivorship import SurvivorshipRepository

_person_repo: PersonRepository = Neo4jPersonRepository()
_entity_repo: EntityRepository = Neo4jEntityRepository()
_sales_repo: SalesRepository = Neo4jSalesRepository()
_merge_repo: MergeRepository = Neo4jMergeRepository()
_review_repo: ReviewRepository = Neo4jReviewRepository()
_survivorship_repo: SurvivorshipRepository = Neo4jSurvivorshipRepository()
_report_repo: ReportRepository = Neo4jReportRepository()
_event_repo: EventRepository = Neo4jEventRepository()
_admin_repo: AdminRepository = Neo4jAdminRepository()
_ingest_repo: IngestRepository = Neo4jIngestRepository()


def get_person_repo() -> PersonRepository:
    return _person_repo


def get_entity_repo() -> EntityRepository:
    return _entity_repo


def get_sales_repo() -> SalesRepository:
    return _sales_repo


def get_merge_repo() -> MergeRepository:
    return _merge_repo


def get_review_repo() -> ReviewRepository:
    return _review_repo


def get_survivorship_repo() -> SurvivorshipRepository:
    return _survivorship_repo


def get_report_repo() -> ReportRepository:
    return _report_repo


def get_event_repo() -> EventRepository:
    return _event_repo


def get_admin_repo() -> AdminRepository:
    return _admin_repo


def get_ingest_repo() -> IngestRepository:
    return _ingest_repo
