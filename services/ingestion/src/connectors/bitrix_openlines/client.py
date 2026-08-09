"""Read-only Bitrix REST client for Open Lines ingestion."""

from __future__ import annotations

import logging
import time
from collections.abc import Collection, Iterator, Mapping
from datetime import datetime
from typing import cast
from urllib.parse import urlencode

import httpx

from src.connectors.bitrix_openlines.crm_deal_filter import (
    CrmDealPage,
    crm_deal_capability_filter,
    crm_deal_category_filter,
    normalize_crm_category_ids,
    parse_crm_deal_capability_page,
)
from src.connectors.bitrix_openlines.crm_status_catalog import (
    deal_stage_status_entity_id,
    parse_crm_deal_stage_catalog_page,
)
from src.connectors.bitrix_openlines.models import (
    ChatReference,
    CrmActivity,
    CrmActivityCapabilityPage,
    CrmContact,
    CrmDeal,
    CrmDealCapabilityPage,
    CrmDealStageCatalogPage,
    CrmDiscoveryPage,
    DialogMetadata,
    OpenLineConfig,
    OpenLineMessage,
)
from src.connectors.bitrix_openlines.response_helpers import (
    RETRYABLE_ERRORS,
    activity_ids,
    deduplicate_references,
    envelope_retry_delay,
    history_message_page,
    message_page,
    next_start,
    numeric_chat_id,
    optional_datetime,
    owner_reference,
    provider_chat_id,
    provider_references,
    recent_references,
    retry_delay,
)
from src.connectors.bitrix_stage_history.models import (
    StageHistoryPage,
    parse_stage_history_page,
)
from src.models import JsonValue

logger = logging.getLogger(__name__)


class BitrixOpenLinesClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_attempts: int,
        request_delay_seconds: float = 0.0,
        http: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Bitrix Open Lines API base URL is required")
        self._base_url = base_url.strip().rstrip("/")
        self._max_attempts = max_attempts
        self._request_delay_seconds = request_delay_seconds
        self._last_request_at = 0.0
        self._request_count = 0
        self._activities_scanned = 0
        self._http = http or httpx.Client(timeout=timeout_seconds)

    def list_active_configs(self) -> list[OpenLineConfig]:
        configs: list[OpenLineConfig] = []
        start = 0
        while True:
            payload = self._request("imopenlines.config.list.get", {"start": start})
            result = payload.get("result")
            if not isinstance(result, list):
                raise RuntimeError("Bitrix config list returned an invalid result")
            for item in result:
                if not isinstance(item, dict):
                    raise RuntimeError("Bitrix config list contained an invalid item")
                config_id = item.get("ID")
                line_name = item.get("LINE_NAME")
                active = item.get("ACTIVE")
                if not isinstance(config_id, str) or not isinstance(line_name, str):
                    raise RuntimeError("Bitrix config list omitted required fields")
                if active == "Y":
                    configs.append(OpenLineConfig(config_id, line_name))
            next_page = next_start(payload, start)
            if next_page is None:
                return configs
            start = next_page

    def get_dialog(self, chat_id: int) -> DialogMetadata:
        result = self._call("im.dialog.get", {"DIALOG_ID": f"chat{chat_id}"})
        if not isinstance(result, dict):
            raise RuntimeError("Bitrix dialog returned an invalid result")
        entity_link = result.get("entity_link")
        if not isinstance(entity_link, dict):
            raise RuntimeError("Bitrix dialog omitted its Open Lines origin")
        origin = entity_link.get("id")
        if not isinstance(origin, str):
            raise RuntimeError("Bitrix dialog omitted its Open Lines origin ID")
        parts = origin.split("|")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise RuntimeError("Bitrix dialog returned an invalid Open Lines origin ID")
        return DialogMetadata(chat_id, parts[1], parts[0])

    def iter_crm_chat_refs(self) -> list[ChatReference]:
        refs: list[ChatReference] = []
        for page in self.iter_crm_chat_ref_pages():
            refs.extend(page)
        return deduplicate_references(refs)

    def iter_crm_chat_ref_pages(self) -> Iterator[list[ChatReference]]:
        for page in self.iter_crm_discovery_pages():
            yield page.references

    def iter_crm_discovery_pages(self, *, start: int = 0) -> Iterator[CrmDiscoveryPage]:
        chat_ids_by_owner: dict[tuple[str, int], list[int]] = {}
        started_at = time.monotonic()
        while True:
            payload = self._request(
                "crm.activity.list",
                {
                    "filter": {"PROVIDER_ID": "IMOPENLINES_SESSION"},
                    "select": [
                        "ID",
                        "OWNER_TYPE_ID",
                        "OWNER_ID",
                        "PROVIDER_PARAMS",
                        "LAST_UPDATED",
                    ],
                    "order": {"ID": "ASC"},
                    "start": start,
                },
            )
            result = payload.get("result")
            if not isinstance(result, list):
                raise RuntimeError("Bitrix CRM activities returned an invalid result")
            page_refs = deduplicate_references(self._crm_references(result, chat_ids_by_owner))
            next_page = next_start(payload, start)
            elapsed = max(time.monotonic() - started_at, 0.001)
            processed = start + len(result)
            total = payload.get("total")
            eta_seconds = (
                max(float(total) - processed, 0.0) / (processed / elapsed)
                if isinstance(total, (int, float)) and processed > 0
                else None
            )
            logger.info(
                "Bitrix CRM discovery page start=%d activities=%d chats=%d "
                "processed=%d total=%s requests=%d rate=%.2f_per_second "
                "request_rate=%.2f_per_second eta_seconds=%s",
                start,
                len(result),
                len(page_refs),
                processed,
                total if isinstance(total, (int, float)) else "unknown",
                self._request_count,
                processed / elapsed,
                self._request_count / elapsed,
                f"{eta_seconds:.1f}" if eta_seconds is not None else "unknown",
            )
            self._activities_scanned += len(result)
            yield CrmDiscoveryPage(page_refs, next_page)
            if next_page is None:
                logger.info(
                    "Bitrix CRM discovery complete activities_scanned=%d requests=%d",
                    self._activities_scanned,
                    self._request_count,
                )
                return
            start = next_page

    def _crm_references(
        self,
        result: list[JsonValue],
        chat_ids_by_owner: dict[tuple[str, int], list[int]],
    ) -> list[ChatReference]:
        missing_owners = sorted(
            {
                (owner.owner_type, owner.owner_id)
                for item in result
                if isinstance(item, dict)
                if provider_chat_id(item.get("PROVIDER_PARAMS")) is None
                if (owner := owner_reference(item)) is not None
                if (owner.owner_type, owner.owner_id) not in chat_ids_by_owner
            }
        )
        if len(missing_owners) == 1:
            owner_key = missing_owners[0]
            chat_ids_by_owner[owner_key] = self._crm_chat_ids(*owner_key)
        elif missing_owners:
            chat_ids_by_owner.update(self._crm_chat_ids_batch(missing_owners))

        refs: list[ChatReference] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            changed_at = optional_datetime(item.get("LAST_UPDATED"))
            activity_id_values = activity_ids(item.get("ID"))
            owner = owner_reference(item)
            owner_references = () if owner is None else (owner,)
            provider_reference_values = provider_references(item.get("PROVIDER_PARAMS"))
            chat_id = provider_chat_id(item.get("PROVIDER_PARAMS"))
            if chat_id is not None:
                refs.append(
                    ChatReference(
                        chat_id,
                        changed_at,
                        "crm_activity",
                        activity_id_values,
                        owner_references,
                        provider_reference_values,
                    )
                )
                continue
            if owner is None:
                continue
            owner_key = (owner.owner_type, owner.owner_id)
            for resolved_chat_id in chat_ids_by_owner[owner_key]:
                refs.append(
                    ChatReference(
                        resolved_chat_id,
                        changed_at,
                        "crm_activity",
                        activity_id_values,
                        owner_references,
                        provider_reference_values,
                    )
                )
        return refs

    def _crm_chat_ids_batch(
        self,
        owners: list[tuple[str, int]],
    ) -> dict[tuple[str, int], list[int]]:
        resolved: dict[tuple[str, int], list[int]] = {owner: [] for owner in owners}
        pending = {owner: 0 for owner in owners}
        while pending:
            batch_owners = list(pending)[:50]
            command_owners = {f"owner_{index}": owner for index, owner in enumerate(batch_owners)}
            commands: dict[str, JsonValue] = {
                command_key: self._crm_chat_lookup_command(owner, pending[owner])
                for command_key, owner in command_owners.items()
            }
            raw_batch = self._call("batch", {"halt": 0, "cmd": commands})
            if not isinstance(raw_batch, dict):
                raise RuntimeError("Bitrix CRM chat batch returned an invalid result")
            raw_results = raw_batch.get("result")
            raw_next = raw_batch.get("result_next")
            raw_errors = raw_batch.get("result_error")
            if not isinstance(raw_results, dict):
                raise RuntimeError("Bitrix CRM chat batch omitted command results")
            if isinstance(raw_errors, dict) and raw_errors:
                raise RuntimeError("Bitrix CRM chat batch contained a command error")
            next_starts = raw_next if isinstance(raw_next, dict) else {}
            for command_key, owner in command_owners.items():
                raw_items = raw_results.get(command_key)
                if not isinstance(raw_items, list):
                    raise RuntimeError("Bitrix CRM chat batch returned an invalid collection")
                for item in raw_items:
                    if isinstance(item, dict):
                        chat_id = numeric_chat_id(item.get("CHAT_ID"))
                        if chat_id is not None:
                            resolved[owner].append(chat_id)
                next_start = numeric_chat_id(next_starts.get(command_key))
                if next_start is None:
                    pending.pop(owner)
                elif next_start <= pending[owner]:
                    raise RuntimeError("Bitrix CRM chat batch pagination did not advance")
                else:
                    pending[owner] = next_start
        return resolved

    @staticmethod
    def _crm_chat_lookup_command(owner: tuple[str, int], start: int) -> str:
        owner_type, owner_id = owner
        query = urlencode(
            {
                "CRM_ENTITY_TYPE": owner_type,
                "CRM_ENTITY": owner_id,
                "ACTIVE_ONLY": "N",
                "start": start,
            }
        )
        return f"imopenlines.crm.chat.get?{query}"

    def _crm_chat_ids(self, owner_type: str, owner_id: int) -> list[int]:
        chat_ids: list[int] = []
        start = 0
        while True:
            payload = self._request(
                "imopenlines.crm.chat.get",
                {
                    "CRM_ENTITY_TYPE": owner_type,
                    "CRM_ENTITY": owner_id,
                    "ACTIVE_ONLY": "N",
                    "start": start,
                },
            )
            result = payload.get("result")
            if not isinstance(result, list):
                raise RuntimeError("Bitrix CRM chat lookup returned an invalid result")
            for item in result:
                if isinstance(item, dict):
                    chat_id = numeric_chat_id(item.get("CHAT_ID"))
                    if chat_id is not None:
                        chat_ids.append(chat_id)
            next_page = next_start(payload, start)
            if next_page is None:
                return chat_ids
            start = next_page

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        refs: list[ChatReference] = []
        offset = 0
        while True:
            result = self._call(
                "im.recent.list",
                {"LIMIT": page_size, "OFFSET": offset, "GET_ORIGINAL_TEXT": "Y"},
            )
            if isinstance(result, dict):
                raw_items = result.get("items")
                has_more = result.get("hasMore") is True
            else:
                raw_items = result
                has_more = isinstance(result, list) and len(result) >= page_size
            if not isinstance(raw_items, list):
                raise RuntimeError("Bitrix recent dialogs returned an invalid result")
            refs.extend(recent_references(raw_items))
            if not has_more:
                return refs
            if not raw_items:
                raise RuntimeError("Bitrix recent-dialog pagination did not advance")
            offset += page_size

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        return self._get_message_collection(
            "im.dialog.messages.get",
            {"DIALOG_ID": f"chat{chat_id}"},
        )

    def get_history(self, chat_id: int) -> list[OpenLineMessage]:
        """Return history for a CRM-only Open Lines chat using its numeric ID."""
        result = self._call("imopenlines.session.history.get", {"CHAT_ID": chat_id})
        return sorted(
            history_message_page(result),
            key=lambda item: (item.date, item.id),
        )

    def iter_crm_deal_pages(self, category_ids: Collection[str]) -> Iterator[CrmDealPage]:
        """Yield filtered CRM deal pages independently of Open Lines discovery."""
        normalized_categories = normalize_crm_category_ids(category_ids)
        if not normalized_categories:
            logger.info(
                "Bitrix CRM deal source filter skipped reason=empty_category_allowlist "
                "crm_categories_requested=0 crm_deal_api_pages=0 crm_deals_returned=0"
            )
            return
        start = 0
        page_count = 0
        returned_count = 0
        seen_deal_ids: set[str] = set()
        while True:
            payload = self._request(
                "crm.deal.list",
                {
                    "filter": crm_deal_category_filter(normalized_categories),
                    "order": {"ID": "ASC"},
                    "start": start,
                },
            )
            result = payload.get("result")
            if not isinstance(result, list):
                raise RuntimeError("Bitrix CRM deal list returned an invalid result")
            page_count += 1
            returned_count += len(result)
            logger.info(
                "Bitrix CRM deal page crm_categories_requested=%d crm_deal_api_pages=%d "
                "crm_deals_returned=%d",
                len(normalized_categories),
                page_count,
                returned_count,
            )
            deals: list[CrmDeal] = []
            for item in result:
                if not isinstance(item, dict):
                    raise RuntimeError("Bitrix CRM deal list contained an invalid item")
                deal_id = _positive_id_string(item.get("ID"))
                if deal_id is None or not deal_id.isdigit():
                    raise RuntimeError("Bitrix CRM deal list omitted a valid ID")
                if deal_id in seen_deal_ids:
                    continue
                seen_deal_ids.add(deal_id)
                deals.append(self._deal_from_payload(int(deal_id), item))
            yield CrmDealPage(tuple(deals), len(result))
            next_page = next_start(payload, start)
            if next_page is None:
                return
            start = next_page

    def iter_crm_deals(self, category_ids: Collection[str]) -> Iterator[CrmDeal]:
        """Yield filtered CRM deals while retaining a simple item iterator."""
        for page in self.iter_crm_deal_pages(category_ids):
            yield from page.deals

    def list_crm_deal_capability_page(
        self,
        *,
        category_ids: Collection[str],
        greater_than_id: int | None = None,
        less_than_or_equal_to_id: int | None = None,
        order_direction: str = "ASC",
    ) -> CrmDealCapabilityPage:
        """Fetch one minimal, read-only keyset page for a deal capability census.

        This boundary deliberately does not create ``CrmDeal`` values because
        those hydrate contacts and leads. It sends only source fields necessary
        for a bounded owner census and has no graph, checkpoint, or enrichment
        side effects.
        """
        if not isinstance(order_direction, str) or order_direction not in {"ASC", "DESC"}:
            raise ValueError("Bitrix CRM deal capability order_direction must be ASC or DESC")
        payload = self._request(
            "crm.deal.list",
            {
                "filter": crm_deal_capability_filter(
                    category_ids,
                    greater_than_id=greater_than_id,
                    less_than_or_equal_to_id=less_than_or_equal_to_id,
                ),
                "select": ["ID", "CATEGORY_ID", "STAGE_ID"],
                "order": {"ID": order_direction},
                "start": -1,
            },
        )
        return parse_crm_deal_capability_page(payload)

    def list_stage_history_page(
        self,
        *,
        entity_type_id: int,
        filters: Mapping[str, JsonValue] | None = None,
        order_direction: str = "ASC",
        start: int = -1,
    ) -> StageHistoryPage:
        """Fetch one read-only, typed ``crm.stagehistory.list`` page.

        This method is intentionally capability-only: it returns source evidence
        and does not create records, checkpoints, or side effects.
        """
        if isinstance(entity_type_id, bool) or entity_type_id < 1:
            raise ValueError("Bitrix stage-history entity_type_id must be positive")
        if isinstance(start, bool) or start < -1:
            raise ValueError("Bitrix stage-history start must be -1 or non-negative")
        if order_direction not in {"ASC", "DESC"}:
            raise ValueError("Bitrix stage-history order_direction must be ASC or DESC")
        payload = self._request(
            "crm.stagehistory.list",
            {
                "entityTypeId": entity_type_id,
                "filter": dict(filters or {}),
                "order": {"ID": order_direction},
                "start": start,
            },
        )
        return parse_stage_history_page(
            payload,
            entity_type_id=str(entity_type_id),
            current_start=start,
        )

    def list_crm_deal_stage_catalog_page(
        self,
        *,
        category_id: int,
        start: int = 0,
    ) -> CrmDealStageCatalogPage:
        """Fetch one read-only current stage-catalog page for a deal category.

        This capability boundary uses ``crm.status.list`` only. It neither
        reads deal/activity records nor creates graph, checkpoint, or
        ingestion side effects.
        """
        entity_id = deal_stage_status_entity_id(category_id)
        payload = self._request(
            "crm.status.list",
            {
                "filter": {"ENTITY_ID": entity_id},
                "order": {"SORT": "ASC"},
                "start": start,
            },
        )
        return parse_crm_deal_stage_catalog_page(
            payload,
            category_id=category_id,
            current_start=start,
        )

    def get_deal(self, deal_id: int) -> CrmDeal:
        """Fetch a deal and the primary contact/lead identity evidence."""
        result = self._call("crm.deal.get", {"id": deal_id})
        return self._deal_from_payload(deal_id, result)

    def get_deals(self, deal_ids: Collection[int]) -> list[CrmDeal]:
        """Hydrate one capability page through Bitrix batch requests."""
        ordered_ids = list(deal_ids)
        if len(ordered_ids) > 50:
            raise ValueError("Bitrix deal hydration accepts at most 50 deals")
        if any(isinstance(deal_id, bool) or deal_id < 1 for deal_id in ordered_ids):
            raise ValueError("Bitrix deal hydration IDs must be positive")
        if len(ordered_ids) != len(set(ordered_ids)):
            raise ValueError("Bitrix deal hydration IDs must be unique")
        if not ordered_ids:
            return []

        raw_deals = self._batch_entity_results("crm.deal.get", ordered_ids, "deal")
        raw_contact_items = self._batch_entity_results(
            "crm.deal.contact.items.get", ordered_ids, "deal_contacts"
        )
        contact_ids_by_deal: dict[int, tuple[str, ...]] = {}
        contact_ids: list[str] = []
        lead_ids: list[str] = []
        for deal_id in ordered_ids:
            raw_deal = raw_deals[str(deal_id)]
            if not isinstance(raw_deal, dict):
                raise RuntimeError("Bitrix deal batch returned an invalid deal")
            raw_items = raw_contact_items[str(deal_id)]
            if not isinstance(raw_items, list):
                raise RuntimeError("Bitrix deal contact batch returned an invalid collection")
            associated_ids = self._contact_ids_from_items(raw_items)
            if not associated_ids:
                associated_ids = _string_values(raw_deal.get("CONTACT_IDS"))
            explicit_contact_id = _positive_id_string(raw_deal.get("CONTACT_ID"))
            if explicit_contact_id is not None and explicit_contact_id not in associated_ids:
                associated_ids = (explicit_contact_id, *associated_ids)
            contact_ids_by_deal[deal_id] = associated_ids
            contact_ids.extend(associated_ids)
            if not associated_ids:
                lead_id = _positive_id_string(raw_deal.get("LEAD_ID"))
                if lead_id is not None:
                    lead_ids.append(lead_id)

        contacts_by_id = self._batch_crm_contacts("crm.contact.get", contact_ids, "contact")
        leads_by_id = self._batch_crm_contacts("crm.lead.get", lead_ids, "lead")
        return [
            self._deal_from_hydrated_payload(
                deal_id,
                raw_deals[str(deal_id)],
                contact_ids_by_deal[deal_id],
                contacts_by_id,
                leads_by_id,
            )
            for deal_id in ordered_ids
        ]

    def _batch_entity_results(
        self,
        method: str,
        entity_ids: Collection[int | str],
        command_prefix: str,
    ) -> dict[str, JsonValue]:
        ordered_ids = list(dict.fromkeys(str(entity_id) for entity_id in entity_ids))
        results: dict[str, JsonValue] = {}
        for offset in range(0, len(ordered_ids), 50):
            chunk = ordered_ids[offset : offset + 50]
            command_ids = {
                f"{command_prefix}_{index}": entity_id for index, entity_id in enumerate(chunk)
            }
            commands: dict[str, JsonValue] = {
                command_key: f"{method}?{urlencode({'id': entity_id})}"
                for command_key, entity_id in command_ids.items()
            }
            batch_results = self._validated_batch_results(commands, command_prefix)
            for command_key, entity_id in command_ids.items():
                results[entity_id] = batch_results[command_key]
        return results

    def _validated_batch_results(
        self,
        commands: Mapping[str, JsonValue],
        context: str,
    ) -> dict[str, JsonValue]:
        raw_batch = self._call("batch", {"halt": 0, "cmd": dict(commands)})
        if not isinstance(raw_batch, dict):
            raise RuntimeError(f"Bitrix {context} batch returned an invalid result")
        raw_results = raw_batch.get("result")
        raw_errors = raw_batch.get("result_error")
        if not isinstance(raw_results, dict):
            raise RuntimeError(f"Bitrix {context} batch omitted command results")
        if raw_errors is not None and not isinstance(raw_errors, dict | list):
            raise RuntimeError(f"Bitrix {context} batch returned invalid command errors")
        if isinstance(raw_errors, dict | list) and raw_errors:
            raise RuntimeError(f"Bitrix {context} batch contained a command error")
        missing = set(commands).difference(raw_results)
        if missing:
            raise RuntimeError(f"Bitrix {context} batch omitted a command result")
        return raw_results

    def _batch_crm_contacts(
        self,
        method: str,
        entity_ids: Collection[str],
        kind: str,
    ) -> dict[str, CrmContact]:
        raw_results = self._batch_entity_results(method, entity_ids, kind)
        contacts: dict[str, CrmContact] = {}
        for requested_id, result in raw_results.items():
            contact = _crm_contact(result, kind=kind)
            if contact.id != requested_id:
                raise RuntimeError(f"Bitrix {kind} batch returned a mismatched ID")
            contacts[requested_id] = contact
        return contacts

    def _deal_from_hydrated_payload(
        self,
        deal_id: int,
        result: JsonValue,
        contact_ids: tuple[str, ...],
        contacts_by_id: Mapping[str, CrmContact],
        leads_by_id: Mapping[str, CrmContact],
    ) -> CrmDeal:
        if not isinstance(result, dict):
            raise RuntimeError("Bitrix deal batch returned an invalid result")
        raw_id = _positive_id_string(result.get("ID"))
        if raw_id != str(deal_id):
            raise RuntimeError("Bitrix deal batch returned a mismatched ID")
        explicit_contact_id = _positive_id_string(result.get("CONTACT_ID"))
        contacts = tuple(contacts_by_id[contact_id] for contact_id in contact_ids)
        primary_contact: CrmContact | None = None
        if explicit_contact_id is not None:
            primary_contact = contacts_by_id[explicit_contact_id]
        elif len(contacts) == 1:
            primary_contact = contacts[0]
        elif not contacts:
            lead_id = _positive_id_string(result.get("LEAD_ID"))
            if lead_id is not None:
                primary_contact = leads_by_id[lead_id]
                contacts = (primary_contact,)
        return CrmDeal(
            id=raw_id,
            title=_string(result.get("TITLE")) or "",
            category_id=_string(result.get("CATEGORY_ID")),
            stage_id=_string(result.get("STAGE_ID")),
            observed_at=_first_datetime(result, "DATE_MODIFY", "DATE_CREATE"),
            primary_contact=primary_contact,
            contacts=contacts,
            contact_count=len(contact_ids),
            has_ambiguous_contacts=len(contact_ids) > 1 and explicit_contact_id is None,
            raw_payload=result,
        )

    @staticmethod
    def _contact_ids_from_items(items: list[JsonValue]) -> tuple[str, ...]:
        contact_ids: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError("Bitrix deal contact batch contained an invalid item")
            contact_id = _positive_id_string(item.get("CONTACT_ID"))
            if contact_id is not None:
                contact_ids.append(contact_id)
        return tuple(dict.fromkeys(contact_ids))

    def get_deal_or_none(self, deal_id: int) -> CrmDeal | None:
        """Return ``None`` only for an explicit healthy Bitrix not-found response."""
        payload = self._request(
            "crm.deal.get",
            {"id": deal_id},
            allowed_errors=frozenset({"ERROR_NOT_FOUND", "CRM_DEAL_NOT_FOUND"}),
        )
        error = payload.get("error")
        if isinstance(error, str):
            return None
        return self._deal_from_payload(deal_id, payload.get("result"))

    def _deal_from_payload(self, deal_id: int, result: JsonValue) -> CrmDeal:
        """Convert a deal-list or deal-get response into the shared CRM model."""
        if not isinstance(result, dict):
            raise RuntimeError("Bitrix deal returned an invalid result")
        raw = result
        raw_id = _positive_id_string(raw.get("ID"))
        if raw_id is None:
            raise RuntimeError("Bitrix deal omitted its ID")
        explicit_contact_id = _positive_id_string(raw.get("CONTACT_ID"))
        contact_ids = self._deal_contact_ids(deal_id, raw)
        if explicit_contact_id is not None and explicit_contact_id not in contact_ids:
            contact_ids = (explicit_contact_id, *contact_ids)
        primary_contact: CrmContact | None = None
        contacts: tuple[CrmContact, ...] = ()
        if contact_ids:
            contacts = tuple(self.get_contact(contact_id) for contact_id in contact_ids)
            if explicit_contact_id is not None:
                primary_contact = next(
                    contact for contact in contacts if contact.id == explicit_contact_id
                )
            elif len(contacts) == 1:
                primary_contact = contacts[0]
        elif not contact_ids:
            lead_id = _positive_id_string(raw.get("LEAD_ID"))
            if lead_id is not None:
                primary_contact = self.get_lead(lead_id)
                contacts = (primary_contact,)
        return CrmDeal(
            id=raw_id,
            title=_string(raw.get("TITLE")) or "",
            category_id=_string(raw.get("CATEGORY_ID")),
            stage_id=_string(raw.get("STAGE_ID")),
            observed_at=_first_datetime(raw, "DATE_MODIFY", "DATE_CREATE"),
            primary_contact=primary_contact,
            contacts=contacts,
            contact_count=len(contact_ids),
            has_ambiguous_contacts=len(contact_ids) > 1 and explicit_contact_id is None,
            raw_payload=raw,
        )

    def _deal_contact_ids(
        self,
        deal_id: int,
        deal: Mapping[str, JsonValue],
    ) -> tuple[str, ...]:
        """Read all deal contacts, falling back to legacy deal fields.

        ``CONTACT_ID`` identifies the primary contact, but it does not include
        every contact associated with the deal. The association endpoint is
        therefore required to enforce the no-primary multi-contact policy.
        """
        fallback = _string_values(deal.get("CONTACT_IDS"))
        result = self._call("crm.deal.contact.items.get", {"id": deal_id})
        if not isinstance(result, list):
            return fallback
        contact_ids: list[str] = []
        for item in result:
            if isinstance(item, dict):
                contact_id = _positive_id_string(item.get("CONTACT_ID"))
                if contact_id is not None:
                    contact_ids.append(contact_id)
        return tuple(dict.fromkeys(contact_ids)) or fallback

    def get_contact(self, contact_id: str) -> CrmContact:
        result = self._call("crm.contact.get", {"id": contact_id})
        return _crm_contact(result, kind="contact")

    def get_lead(self, lead_id: str) -> CrmContact:
        result = self._call("crm.lead.get", {"id": lead_id})
        return _crm_contact(result, kind="lead")

    def list_deal_activities(self, deal_id: int) -> list[CrmActivity]:
        """Return all current activities for a deal; callers make them immutable."""
        return list(
            self._iter_crm_activities(
                {"OWNER_TYPE_ID": 2, "OWNER_ID": deal_id},
                invalid_result_message="Bitrix deal activities returned an invalid result",
            )
        )

    def iter_crm_activities(self) -> Iterator[CrmActivity]:
        """Yield all deal-owned CRM activities in one paginated discovery scan."""
        yield from self._iter_crm_activities(
            {"OWNER_TYPE_ID": 2},
            invalid_result_message="Bitrix CRM activities returned an invalid result",
        )

    def list_crm_activity_capability_page(
        self,
        *,
        greater_than_id: int | None,
        less_than_or_equal_to_id: int,
        order_direction: str = "ASC",
    ) -> CrmActivityCapabilityPage:
        """Fetch one strict activity-ID keyset page without offset fallback."""
        if order_direction not in {"ASC", "DESC"}:
            raise ValueError("Bitrix CRM activity order_direction must be ASC or DESC")
        if isinstance(less_than_or_equal_to_id, bool) or less_than_or_equal_to_id < 1:
            raise ValueError("Bitrix CRM activity upper ID must be positive")
        filters: dict[str, JsonValue] = {
            "OWNER_TYPE_ID": 2,
            "<=ID": less_than_or_equal_to_id,
        }
        if greater_than_id is not None:
            if isinstance(greater_than_id, bool) or greater_than_id < 1:
                raise ValueError("Bitrix CRM activity lower ID must be positive")
            if greater_than_id >= less_than_or_equal_to_id:
                raise ValueError("Bitrix CRM activity keyset bounds must increase")
            filters[">ID"] = greater_than_id
        payload = self._request(
            "crm.activity.list",
            {
                "filter": filters,
                "select": [
                    "ID",
                    "OWNER_TYPE_ID",
                    "OWNER_ID",
                    "TYPE_ID",
                    "PROVIDER_ID",
                    "PROVIDER_TYPE_ID",
                    "SUBJECT",
                    "LAST_UPDATED",
                    "CREATED",
                    "START_TIME",
                    "END_TIME",
                    "DURATION",
                    "DIRECTION",
                    "RESULT_STATUS",
                    "COMPLETED",
                    "PROVIDER_PARAMS",
                    "SETTINGS",
                ],
                "order": {"ID": order_direction},
                "start": -1,
            },
        )
        raw_items = payload.get("result")
        if not isinstance(raw_items, list):
            raise RuntimeError("Bitrix CRM activity capability returned an invalid result")
        items: list[CrmActivity] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise RuntimeError("Bitrix CRM activity capability contained an invalid item")
            activity = _crm_activity(raw)
            if activity is None or not activity.id.isdigit():
                raise RuntimeError("Bitrix CRM activity capability omitted a numeric identity")
            items.append(activity)
        timing = payload.get("time")
        if timing is not None and not isinstance(timing, dict):
            raise RuntimeError("Bitrix CRM activity capability returned invalid timing")
        timing_map = timing if isinstance(timing, dict) else {}
        total = _optional_non_negative_number(payload.get("total"))
        # ``start=-1`` enables Bitrix fast keyset pagination and uses zero as
        # an unavailable-total sentinel, including on non-empty pages.
        if total == 0:
            total = None
        return CrmActivityCapabilityPage(
            items=tuple(items),
            total=total,
            operating=_optional_number(timing_map.get("operating")),
            operating_reset_at=_optional_number(timing_map.get("operating_reset_at")),
        )

    def _iter_crm_activities(
        self,
        filters: dict[str, JsonValue],
        *,
        invalid_result_message: str,
    ) -> Iterator[CrmActivity]:
        start = 0
        while True:
            payload = self._request(
                "crm.activity.list",
                {
                    "filter": filters,
                    "select": [
                        "ID",
                        "OWNER_TYPE_ID",
                        "OWNER_ID",
                        "TYPE_ID",
                        "PROVIDER_ID",
                        "PROVIDER_TYPE_ID",
                        "SUBJECT",
                        "LAST_UPDATED",
                        "CREATED",
                        "START_TIME",
                        "END_TIME",
                        "DURATION",
                        "DIRECTION",
                        "RESULT_STATUS",
                        "COMPLETED",
                        "PROVIDER_PARAMS",
                        "SETTINGS",
                    ],
                    "order": {"ID": "ASC"},
                    "start": start,
                },
            )
            result = payload.get("result")
            if not isinstance(result, list):
                raise RuntimeError(invalid_result_message)
            for item in result:
                if isinstance(item, dict):
                    activity = _crm_activity(item)
                    if activity is not None:
                        yield activity
            next_page = next_start(payload, start)
            if next_page is None:
                return
            start = next_page

    def _get_message_collection(
        self,
        method: str,
        base_params: Mapping[str, JsonValue],
    ) -> list[OpenLineMessage]:
        messages: dict[int, OpenLineMessage] = {}
        last_id: int | None = None
        while True:
            params = dict(base_params)
            params["LIMIT"] = 50
            if last_id is not None:
                params["LAST_ID"] = last_id
            result = self._call(method, params)
            page = message_page(result)
            for message in page:
                messages[message.id] = message
            if len(page) < 50:
                return sorted(messages.values(), key=lambda item: (item.date, item.id))
            next_last_id = min(item.id for item in page)
            if next_last_id == last_id:
                raise RuntimeError("Bitrix message pagination did not advance")
            last_id = next_last_id

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, params: Mapping[str, JsonValue]) -> JsonValue:
        return self._request(method, params)["result"]

    def _request(
        self,
        method: str,
        params: Mapping[str, JsonValue],
        *,
        allowed_errors: frozenset[str] = frozenset(),
    ) -> dict[str, JsonValue]:
        for attempt in range(1, self._max_attempts + 1):
            try:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < self._request_delay_seconds:
                    time.sleep(self._request_delay_seconds - elapsed)
                response = self._http.post(f"{self._base_url}/{method}", json=dict(params))
                self._request_count += 1
                self._last_request_at = time.monotonic()
                response.raise_for_status()
                payload = cast(object, response.json())
                if not isinstance(payload, dict):
                    raise RuntimeError(f"Bitrix method {method} returned an invalid envelope")
                typed_payload = cast(dict[str, JsonValue], payload)
                error = typed_payload.get("error")
                if isinstance(error, str):
                    if error in allowed_errors:
                        return typed_payload
                    if error not in RETRYABLE_ERRORS:
                        raise RuntimeError(f"Bitrix method {method} failed with {error}")
                    if attempt == self._max_attempts:
                        raise RuntimeError(f"Bitrix method {method} failed with {error}")
                    time.sleep(envelope_retry_delay(typed_payload, attempt))
                    continue
                if "result" not in typed_payload:
                    raise RuntimeError(f"Bitrix method {method} returned an invalid envelope")
                return typed_payload
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 and exc.response.status_code < 500:
                    raise RuntimeError(
                        f"Bitrix method {method} failed with HTTP {exc.response.status_code}"
                    ) from None
                if attempt == self._max_attempts:
                    raise RuntimeError(f"Bitrix method {method} request failed") from None
                delay = retry_delay(exc.response, attempt)
                time.sleep(delay)
            except httpx.TransportError:
                if attempt == self._max_attempts:
                    raise RuntimeError(f"Bitrix method {method} request failed") from None
                time.sleep(min(2 ** (attempt - 1), 8))
        raise AssertionError("unreachable")


def _string(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_non_negative_number(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RuntimeError("Bitrix capability returned an invalid total")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise RuntimeError("Bitrix capability returned an invalid total")


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeError("Bitrix capability returned invalid timing")
    return float(value)


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    values = [_positive_id_string(item) for item in value]
    return tuple(item for item in values if item is not None)


def _positive_id_string(value: object) -> str | None:
    parsed = _string(value)
    if parsed is None:
        return None
    try:
        return parsed if int(parsed) > 0 else None
    except ValueError:
        return parsed


def _first_datetime(payload: Mapping[str, JsonValue], *keys: str) -> datetime | None:
    for key in keys:
        value = optional_datetime(payload.get(key))
        if value is not None:
            return value
    return None


def _crm_contact(result: JsonValue, *, kind: str) -> CrmContact:
    if not isinstance(result, dict):
        raise RuntimeError(f"Bitrix {kind} returned an invalid result")
    payload = result
    contact_id = _positive_id_string(payload.get("ID"))
    if contact_id is None:
        raise RuntimeError(f"Bitrix {kind} omitted its ID")
    name_parts = [_string(payload.get(key)) for key in ("NAME", "SECOND_NAME", "LAST_NAME")]
    full_name = " ".join(value for value in name_parts if value) or None
    return CrmContact(
        id=contact_id,
        full_name=full_name,
        phones=_multi_value_field(payload.get("PHONE")),
        emails=_multi_value_field(payload.get("EMAIL")),
        kind=kind,
    )


def _multi_value_field(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    values: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_value = _string(item.get("VALUE"))
        if raw_value is not None:
            values.append(raw_value)
    return tuple(dict.fromkeys(values))


def _crm_activity(payload: dict[str, JsonValue]) -> CrmActivity | None:
    activity_id = _string(payload.get("ID"))
    owner_id = _string(payload.get("OWNER_ID"))
    owner_type = _string(payload.get("OWNER_TYPE_ID"))
    if activity_id is None or owner_id is None or owner_type is None:
        return None
    provider_id = (_string(payload.get("PROVIDER_ID")) or "").upper()
    provider_type = (_string(payload.get("PROVIDER_TYPE_ID")) or "").upper()
    type_id = _string(payload.get("TYPE_ID"))
    is_call = type_id == "2" or "CALL" in provider_id or "CALL" in provider_type
    history_kind = "call" if is_call else _history_kind(provider_id, provider_type, type_id)
    start_at = _first_datetime(payload, "START_TIME")
    end_at = _first_datetime(payload, "END_TIME")
    duration_seconds = _duration_seconds(payload.get("DURATION"))
    if duration_seconds is None and start_at is not None and end_at is not None:
        duration_seconds = max(int((end_at - start_at).total_seconds()), 0)
    return CrmActivity(
        id=activity_id,
        owner_type=owner_type,
        owner_id=owner_id,
        history_kind=history_kind,
        subject=_string(payload.get("SUBJECT")),
        observed_at=_first_datetime(payload, "LAST_UPDATED", "CREATED", "START_TIME"),
        start_at=start_at,
        end_at=end_at,
        duration_seconds=duration_seconds,
        direction=_string(payload.get("DIRECTION")),
        outcome=_string(payload.get("RESULT_STATUS")) or _string(payload.get("COMPLETED")),
        is_call=is_call,
        raw_payload=payload,
    )


def _history_kind(provider_id: str, provider_type: str, type_id: str | None) -> str:
    if provider_id == "IMOPENLINES_SESSION":
        return "openlines_session"
    if provider_type:
        return provider_type.lower()
    if type_id is not None:
        return f"activity_type_{type_id}"
    return "activity"


def _duration_seconds(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(value, 0)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
