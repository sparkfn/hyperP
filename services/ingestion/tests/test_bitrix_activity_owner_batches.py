"""Activity owner resolution remains durable and fail-closed."""

from src.graph.queries.bitrix_deal_scope import GET_CURRENT_DEAL_SCOPE_BATCH


def test_activity_owner_lookup_is_explicit_for_every_requested_deal() -> None:
    assert "UNWIND $deal_ids AS requested_deal_id" in GET_CURRENT_DEAL_SCOPE_BATCH
    assert "OPTIONAL MATCH (deal:CrmLogicalDeal" in GET_CURRENT_DEAL_SCOPE_BATCH
    assert "deal.current_scope_state AS scope_state" in GET_CURRENT_DEAL_SCOPE_BATCH
    assert "ORDER BY deal_id" in GET_CURRENT_DEAL_SCOPE_BATCH
