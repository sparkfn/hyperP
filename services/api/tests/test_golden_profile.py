from __future__ import annotations

from src.graph.golden_profile import _field_trust_tier
from src.graph.queries import CREATE_RECOMPUTE_AUDIT


def test_field_trust_tier_reads_json_string_property() -> None:
    trust = '{"phone": "tier_3", "dob": "tier_4"}'

    assert _field_trust_tier(trust, "dob") == "tier_4"
    assert _field_trust_tier(trust, "email") == "tier_4"


def test_recompute_audit_stores_metadata_as_string_property() -> None:
    assert "metadata: '{}'" in CREATE_RECOMPUTE_AUDIT
    assert "metadata: {}" not in CREATE_RECOMPUTE_AUDIT
