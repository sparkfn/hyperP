"""Active-filter Cypher fragments for the person listing query."""

from __future__ import annotations

ADDRESS_FILTERS = frozenset(
    {"addr_street", "addr_unit", "addr_city", "addr_postal", "addr_country"}
)


def build_common_filter_clause(active_filters: frozenset[str]) -> str:
    """Build only predicates whose values are present on this request.

    A static query containing null-guarded graph ``EXISTS`` predicates still
    burdens Neo4j's planner and can retain expensive traversal operators. The
    repository provides the set of non-null filters so the default listing
    emits only its invariant ``Person`` predicate.
    """
    clauses = ["p.status <> 'merged'"]
    simple_filters = {
        "is_high_value": "p.is_high_value = $is_high_value",
        "is_high_risk": "p.is_high_risk = $is_high_risk",
        "has_phone": "(p.preferred_phone IS NOT NULL) = $has_phone",
        "has_email": "(p.preferred_email IS NOT NULL) = $has_email",
        "has_any_contact": (
            "(p.preferred_email IS NOT NULL OR p.preferred_phone IS NOT NULL) = $has_any_contact"
        ),
        "updated_after": "p.updated_at >= datetime($updated_after)",
        "updated_before": "p.updated_at <= datetime($updated_before)",
        "has_dob": "(p.preferred_dob IS NOT NULL) = $has_dob",
        "dob_from": "p.preferred_dob >= $dob_from",
        "dob_to": "p.preferred_dob <= $dob_to",
        "dob_year": "substring(p.preferred_dob, 0, 4) = $dob_year",
        "dob_month": "substring(p.preferred_dob, 5, 2) = $dob_month",
        "dob_day": "substring(p.preferred_dob, 8, 2) = $dob_day",
        "has_address": "(p.preferred_address_id IS NOT NULL) = $has_address",
        "addr_street": "toLower(addr.street_name) CONTAINS toLower($addr_street)",
        "addr_unit": "toLower(addr.unit_number) CONTAINS toLower($addr_unit)",
        "addr_city": "toLower(addr.city) CONTAINS toLower($addr_city)",
        "addr_postal": "toLower(addr.postal_code) CONTAINS toLower($addr_postal)",
        "addr_country": "toLower(addr.country_code) CONTAINS toLower($addr_country)",
    }
    clauses.extend(simple_filters[name] for name in simple_filters if name in active_filters)

    if "has_bankruptcy_case" in active_filters:
        clauses.append(
            """(EXISTS {
  MATCH (p)-[bankruptcy_rel:HAS_BANKRUPTCY_CASE]->(:BankruptcyCase)
  WHERE coalesce(bankruptcy_rel.is_active, true) = true
}) = $has_bankruptcy_case"""
        )
    if "has_possible_match" in active_filters:
        clauses.append(
            """(EXISTS {
  MATCH (p)-[p_possible_id:IDENTIFIED_BY]->(:Identifier)<-[pm_id:IDENTIFIED_BY]-(pm:Person)
  WHERE coalesce(p_possible_id.is_active, true) = true
    AND coalesce(pm_id.is_active, true) = true
    AND pm.person_id <> p.person_id AND pm.status <> 'merged'
}) = $has_possible_match"""
        )
    if "has_system_match" in active_filters:
        clauses.append(f"({_match_decision_exists()}) = $has_system_match")
    if "has_any_match" in active_filters:
        clauses.append(
            """(
  EXISTS {
    MATCH (p)-[p_any_id:IDENTIFIED_BY]->(:Identifier)<-[am_id:IDENTIFIED_BY]-(am:Person)
    WHERE coalesce(p_any_id.is_active, true) = true
      AND coalesce(am_id.is_active, true) = true
      AND am.person_id <> p.person_id AND am.status <> 'merged'
  }
  OR """
            + _match_decision_exists()
            + ") = $has_any_match"
        )
    return "WHERE " + "\n  AND ".join(clauses) + "\n"


def _match_decision_exists() -> str:
    return """EXISTS {
  MATCH (md:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(p)
  WHERE EXISTS { (md)-[:ABOUT_LEFT]->(:Person) }
    AND EXISTS { (md)-[:ABOUT_RIGHT]->(:Person) }
    AND EXISTS {
      (rc:ReviewCase)-[:FOR_DECISION]->(md)
      WHERE NOT rc.queue_state IN ['resolved', 'cancelled']
    }
}"""


def build_entity_filter_clause(
    entity_mode: str,
    source_mode: str,
    active_filters: frozenset[str],
    *,
    include_preferred_address: bool,
) -> str:
    """Apply only requested source/entity constraints, then load display address."""
    clauses: list[str] = []
    if "entity_keys" in active_filters:
        if entity_mode == "and":
            clauses.append(
                """ALL(ek IN $entity_keys WHERE EXISTS {
  MATCH (sr_e:SourceRecord)-[link:LINKED_TO]->(p)
  WHERE coalesce(link.is_active, true) = true
    AND (sr_e.lifecycle_status = 'active'
      OR (sr_e.lifecycle_status IS NULL AND sr_e.is_latest = true))
    AND (
      EXISTS { MATCH (sr_e)-[:OWNED_BY]->(e:Entity) WHERE e.entity_key = ek }
      OR (
        NOT EXISTS { MATCH (sr_e)-[:OWNED_BY]->(:Entity) }
        AND EXISTS {
          MATCH (sr_e)-[:FROM_SOURCE]->(:SourceSystem)-[:OPERATED_BY]->(e:Entity)
          WHERE e.entity_key = ek
        }
      )
    )
})"""
            )
        else:
            clauses.append(
                """EXISTS {
  MATCH (sr_e:SourceRecord)-[link:LINKED_TO]->(p)
  WHERE coalesce(link.is_active, true) = true
    AND (sr_e.lifecycle_status = 'active'
      OR (sr_e.lifecycle_status IS NULL AND sr_e.is_latest = true))
    AND (
      EXISTS {
        MATCH (sr_e)-[:OWNED_BY]->(e:Entity)
        WHERE e.entity_key IN $entity_keys
      }
      OR (
        NOT EXISTS { MATCH (sr_e)-[:OWNED_BY]->(:Entity) }
        AND EXISTS {
          MATCH (sr_e)-[:FROM_SOURCE]->(:SourceSystem)-[:OPERATED_BY]->(e:Entity)
          WHERE e.entity_key IN $entity_keys
        }
      )
    )
}"""
            )
    if "source_keys" in active_filters:
        source_operator = (
            "ALL(sk IN $source_keys WHERE EXISTS" if source_mode == "and" else "EXISTS"
        )
        source_key_filter = (
            "ss.source_key = sk" if source_mode == "and" else "ss.source_key IN $source_keys"
        )
        if source_mode == "and":
            clauses.append(
                f"""{source_operator} {{
  MATCH (sr_s:SourceRecord)-[link:LINKED_TO]->(p)
  MATCH (sr_s)-[:FROM_SOURCE]->(ss:SourceSystem)
  WHERE coalesce(link.is_active, true) = true
    AND (sr_s.lifecycle_status = 'active'
      OR (sr_s.lifecycle_status IS NULL AND sr_s.is_latest = true))
    AND {source_key_filter}
}})"""
            )
        else:
            clauses.append(
                f"""{source_operator} {{
  MATCH (sr_s:SourceRecord)-[link:LINKED_TO]->(p)
  MATCH (sr_s)-[:FROM_SOURCE]->(ss:SourceSystem)
  WHERE coalesce(link.is_active, true) = true
    AND (sr_s.lifecycle_status = 'active'
      OR (sr_s.lifecycle_status IS NULL AND sr_s.is_latest = true))
    AND {source_key_filter}
}}"""
            )
    if "source_record_type" in active_filters:
        clauses.append(
            """EXISTS {
  MATCH (sr_t:SourceRecord)-[link:LINKED_TO]->(p)
  WHERE coalesce(link.is_active, true) = true
    AND (sr_t.lifecycle_status = 'active'
      OR (sr_t.lifecycle_status IS NULL AND sr_t.is_latest = true))
    AND sr_t.record_type = $source_record_type
}"""
        )
    has_address_filter = bool(active_filters & ADDRESS_FILTERS)
    if clauses:
        result = "WITH p, score\nWHERE " + "\nAND ".join(clauses) + "\n"
        if has_address_filter:
            result += "WITH DISTINCT p, score\n"
    elif has_address_filter:
        result = "WITH DISTINCT p, score\n"
    else:
        result = "WITH p, score\n"
    if include_preferred_address:
        return result + "OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})\n"
    return result
