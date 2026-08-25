"""Read-only graph adapter for CRM-deal identity repair inventory."""

from __future__ import annotations

from src.crm_deal_identity_repair.inventory import RepairInventory, collect_repair_inventory

__all__ = ["RepairInventory", "collect_repair_inventory"]
