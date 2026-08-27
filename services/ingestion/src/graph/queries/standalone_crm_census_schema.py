"""Schema/index Cypher definitions for standalone CRM census."""

from __future__ import annotations

CENSUS_CONSTRAINT_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("standalone_crm_census_id_unique", "StandaloneCrmCensus", ("census_id",)),
    (
        "standalone_crm_census_occurrence_unique",
        "StandaloneCrmCensus",
        (
            "source_key",
            "source_instance_id",
            "control_instance_id",
            "census_kind",
            "occurrence_key",
        ),
    ),
    (
        "standalone_crm_census_scope_lock_unique",
        "StandaloneCrmCensusScopeLock",
        ("source_key", "source_instance_id", "control_instance_id", "census_kind"),
    ),
    (
        "standalone_crm_census_attempt_unique",
        "StandaloneCrmCensusAttempt",
        ("census_id", "generation"),
    ),
    ("standalone_crm_census_unit_unique", "StandaloneCrmCensusUnit", ("census_id", "unit_kind")),
    (
        "standalone_crm_census_checkpoint_unique",
        "StandaloneCrmCensusCheckpoint",
        ("census_id", "unit_kind"),
    ),
    (
        "standalone_crm_census_publication_unique",
        "StandaloneCrmChildPublication",
        ("census_id", "generation", "unit_kind", "sequence"),
    ),
    (
        "standalone_crm_census_call_intent_unique",
        "StandaloneCrmHttpCallReservation",
        ("intent_id",),
    ),
    (
        "standalone_crm_census_fence_unique",
        "StandaloneCrmUnitFence",
        ("census_id", "generation", "unit_kind"),
    ),
)

CENSUS_INDEX_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("standalone_crm_census_recovery_scan", "StandaloneCrmCensusAttempt", ("state", "lease_until")),
    (
        "standalone_crm_census_publication_scan",
        "StandaloneCrmChildPublication",
        ("status", "updated_at"),
    ),
    (
        "standalone_crm_census_call_scan",
        "StandaloneCrmHttpCallReservation",
        ("census_id", "generation", "outcome"),
    ),
    ("standalone_crm_census_fence_scan", "StandaloneCrmUnitFence", ("state", "lease_until")),
)


def create_census_constraint(spec: tuple[str, str, tuple[str, ...]]) -> str:
    name, label, properties = spec
    rendered = ", ".join(f"node.{property}" for property in properties)
    return (
        f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (node:{label}) REQUIRE ({rendered}) IS UNIQUE"
    )


def create_census_index(spec: tuple[str, str, tuple[str, ...]]) -> str:
    name, label, properties = spec
    rendered = ", ".join(f"node.{property}" for property in properties)
    return f"CREATE INDEX {name} IF NOT EXISTS FOR (node:{label}) ON ({rendered})"


CREATE_STANDALONE_CRM_CENSUS_CONSTRAINTS = tuple(
    create_census_constraint(spec) for spec in CENSUS_CONSTRAINT_SPECS
)
CREATE_STANDALONE_CRM_CENSUS_INDEXES = tuple(
    create_census_index(spec) for spec in CENSUS_INDEX_SPECS
)

# Every mutable query uses this exact #272 registry/control topology plus the persisted authority digest.
