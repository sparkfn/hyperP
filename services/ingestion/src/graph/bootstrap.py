"""Idempotent bootstrap of Entity and SourceSystem graph metadata.

Run on every ingestion startup (after :func:`apply_schema`) so a reset or
fresh deploy always has the full entity/source-system fabric in place
before any records are written.
"""

from __future__ import annotations

import json
import logging
from typing import NotRequired, TypedDict

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.bitrix_source_instances import BITRIX_SOURCE_KEY, BitrixSourceInstanceRepository
from src.graph.client import Neo4jClient
from src.source_instances import LEGACY_DEFAULT_SOURCE_INSTANCE_ID

logger = logging.getLogger(__name__)


class _EntitySeed(TypedDict):
    entity_key: str
    display_name: str
    entity_type: str
    country_code: str


class _SourceSystemSeed(TypedDict):
    source_key: str
    display_name: str
    system_type: str
    entity_key: str | None
    field_trust: dict[str, str]
    match_only: NotRequired[bool]


_ENTITIES: tuple[_EntitySeed, ...] = (
    {
        "entity_key": "fundbox",
        "display_name": "Fundbox",
        "entity_type": "lender",
        "country_code": "SG",
    },
    {
        "entity_key": "speedzone",
        "display_name": "SpeedZone",
        "entity_type": "retailer",
        "country_code": "SG",
    },
    {
        "entity_key": "eko",
        "display_name": "Eko",
        "entity_type": "retailer",
        "country_code": "SG",
    },
    {
        "entity_key": "onediver",
        "display_name": "OneDiver",
        "entity_type": "retailer",
        "country_code": "SG",
    },
)


_FUNDBOX_TRUST: dict[str, str] = {
    "phone": "tier_3",
    "email": "tier_3",
    "full_name": "tier_3",
    "dob": "tier_4",
    "nric": "tier_4",
    "address": "tier_4",
}

_POS_TRUST: dict[str, str] = {
    "phone": "tier_2",
    "email": "tier_3",
    "full_name": "tier_3",
    "dob": "tier_4",
    "nric": "tier_4",
    "address": "tier_4",
}

#: Chat / conversation records carry LLM-extracted data — start from tier_3
#: (heuristic-derived) since extraction confidence is tracked per record.
_CHAT_TRUST: dict[str, str] = {
    "phone": "tier_3",
    "email": "tier_3",
    "full_name": "tier_3",
    "dob": "tier_4",
    "nric": "tier_4",
    "address": "tier_4",
}

_GOVERNMENT_REGISTRY_TRUST: dict[str, str] = {
    "full_name": "tier_4",
    "nric": "tier_4",
    "address": "tier_4",
}

#: OneDiver is a dive-school / water-sports customer platform: customer-entered
#: profiles (self-claimed email/phone/name) plus sales orders. Govt IDs (NRIC)
#: and address/DOB sit at tier_4 like the other consumer backends.
_ONEDIVER_TRUST: dict[str, str] = {
    "phone": "tier_3",
    "email": "tier_3",
    "full_name": "tier_3",
    "dob": "tier_4",
    "nric": "tier_4",
    "address": "tier_4",
}


_SOURCE_SYSTEMS: tuple[_SourceSystemSeed, ...] = (
    {
        "source_key": "fundbox",
        "display_name": "Fundbox",
        "system_type": "consumer_backend",
        "entity_key": "fundbox",
        "field_trust": _FUNDBOX_TRUST,
    },
    {
        "source_key": "fundbox:contacts",
        "display_name": "Fundbox — contacts",
        "system_type": "consumer_backend",
        "entity_key": "fundbox",
        "field_trust": _FUNDBOX_TRUST,
    },
    {
        "source_key": "fundbox:legacy",
        "display_name": "Fundbox — legacy profiles",
        "system_type": "consumer_backend",
        "entity_key": "fundbox",
        "field_trust": _FUNDBOX_TRUST,
    },
    {
        "source_key": "fundbox:merged",
        "display_name": "Fundbox — merged users",
        "system_type": "consumer_backend",
        "entity_key": "fundbox",
        "field_trust": _FUNDBOX_TRUST,
    },
    {
        "source_key": "fundbox:sales",
        "display_name": "Fundbox — orders / sales",
        "system_type": "consumer_backend",
        "entity_key": "fundbox",
        "field_trust": _FUNDBOX_TRUST,
    },
    {
        "source_key": "speedzone_phppos",
        "display_name": "SpeedZone phppos",
        "system_type": "pos",
        "entity_key": "speedzone",
        "field_trust": _POS_TRUST,
    },
    {
        "source_key": "speedzone_phppos:sales",
        "display_name": "SpeedZone phppos — sales",
        "system_type": "pos",
        "entity_key": "speedzone",
        "field_trust": _POS_TRUST,
    },
    {
        "source_key": "eko_phppos",
        "display_name": "Eko phppos",
        "system_type": "pos",
        "entity_key": "eko",
        "field_trust": _POS_TRUST,
    },
    {
        "source_key": "eko_phppos:sales",
        "display_name": "Eko phppos — sales",
        "system_type": "pos",
        "entity_key": "eko",
        "field_trust": _POS_TRUST,
    },
    {
        "source_key": "whatsapp_chat",
        "display_name": "WhatsApp Chat (multi-tenant)",
        "system_type": "chat",
        "entity_key": "fundbox",  # placeholder; actual tenant from session_name
        "field_trust": _CHAT_TRUST,
    },
    {
        "source_key": "bitrix_chat",
        "display_name": "Bitrix24 Chat (multi-tenant)",
        "system_type": "chat",
        "entity_key": None,
        "field_trust": _CHAT_TRUST,
    },
    {
        "source_key": "sgbankruptcy",
        "display_name": "SG Bankruptcy Register",
        "system_type": "government_registry",
        "entity_key": None,
        "field_trust": _GOVERNMENT_REGISTRY_TRUST,
        "match_only": True,
    },
    {
        "source_key": "sgrentalflats",
        "display_name": "SG Rental Flats",
        "system_type": "government_registry",
        "entity_key": None,
        "field_trust": _GOVERNMENT_REGISTRY_TRUST,
    },
    {
        "source_key": "onediver",
        "display_name": "OneDiver",
        "system_type": "consumer_backend",
        "entity_key": "onediver",
        "field_trust": _ONEDIVER_TRUST,
    },
    {
        "source_key": "onediver:sales",
        "display_name": "OneDiver Sales",
        "system_type": "consumer_backend",
        "entity_key": "onediver",
        "field_trust": _ONEDIVER_TRUST,
    },
)


#: Derived from ``_SOURCE_SYSTEMS`` so the source_key → entity_key mapping
#: has one source of truth. Consumers (e.g. the sales pipeline) use this
#: instead of string-prefix matching.
SOURCE_KEY_TO_ENTITY: dict[str, str] = {
    source["source_key"]: source["entity_key"]
    for source in _SOURCE_SYSTEMS
    if source["entity_key"] is not None
}


#: Source systems whose ingestion is match-only — they link to existing
#: persons only, never create one, and drop records with no usable match.
#: Derived from ``_SOURCE_SYSTEMS`` so adding a match-only source is a
#: seed-data flip, not a pipeline edit.
MATCH_ONLY_SOURCE_KEYS: frozenset[str] = frozenset(
    source["source_key"] for source in _SOURCE_SYSTEMS if source.get("match_only")
)


def bootstrap_entities_and_sources(client: Neo4jClient) -> None:
    """Upsert the Entity and SourceSystem nodes, with OPERATED_BY edges for
    entity-bound sources only.
    """

    def _work(tx: ManagedTransaction) -> None:
        for entity in _ENTITIES:
            tx.run(
                queries.UPSERT_ENTITY,
                entity_key=entity["entity_key"],
                display_name=entity["display_name"],
                entity_type=entity["entity_type"],
                country_code=entity["country_code"],
            )
        for source in _SOURCE_SYSTEMS:
            if source["entity_key"] is None:
                tx.run(
                    queries.UPSERT_SOURCE_SYSTEM,
                    source_key=source["source_key"],
                    display_name=source["display_name"],
                    system_type=source["system_type"],
                    field_trust=json.dumps(source["field_trust"]),
                )
            else:
                tx.run(
                    queries.UPSERT_SOURCE_SYSTEM_WITH_ENTITY,
                    entity_key=source["entity_key"],
                    source_key=source["source_key"],
                    display_name=source["display_name"],
                    system_type=source["system_type"],
                    field_trust=json.dumps(source["field_trust"]),
                )

    with client.session() as session:
        session.execute_write(_work)
    logger.info(
        "Bootstrapped %d entities and %d source systems", len(_ENTITIES), len(_SOURCE_SYSTEMS)
    )


def bootstrap_legacy_bitrix_source_instance(client: Neo4jClient) -> None:
    """Create only the reserved compatibility registration after control blocking."""
    BitrixSourceInstanceRepository(client).register(
        BITRIX_SOURCE_KEY, LEGACY_DEFAULT_SOURCE_INSTANCE_ID
    )
    logger.info("Bootstrapped the reserved legacy Bitrix source instance")
