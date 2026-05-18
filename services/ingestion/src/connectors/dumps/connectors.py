"""Direct dump-backed source connectors."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from src.connectors.base import SourceConnector
from src.connectors.bitrix.connector import (
    BITRIX_SOURCE_KEY,
    CATEGORY_TO_ENTITY,
    BitrixChatConnector,
    _AgentMember,
)
from src.connectors.bitrix.connector import (
    LLM_BATCH_SIZE as BITRIX_LLM_BATCH_SIZE,
)
from src.connectors.bitrix.connector import (
    _ChatBundle as BitrixChatBundle,
)
from src.connectors.chat_helpers import ExtractionResult, run_extraction_batch
from src.connectors.dumps.reader import DumpRow, load_dump_tables
from src.connectors.eko.connector import EkoConnector
from src.connectors.fundbox.builders import (
    IdentifierBag,
    build_envelope,
    format_address,
    serialize_row,
    to_iso,
)
from src.connectors.fundbox.sales import FundboxSalesConnector, _variant_to_product
from src.connectors.fundbox.users import FundboxConnector
from src.connectors.sggov.bankruptcy import SGGovernmentBankruptcyConnector
from src.connectors.sggov.rental_flats import SGGovernmentRentalFlatsConnector
from src.connectors.speedzone.connector import SpeedZoneConnector
from src.connectors.whatsapp.connector import (
    LLM_BATCH_SIZE as WHATSAPP_LLM_BATCH_SIZE,
)
from src.connectors.whatsapp.connector import (
    ORG_TO_ENTITY,
    _first_str,
    _format_messages,
    _message_endpoints,
    _Participant,
    _participant_jids,
    _phone_from_jid,
)
from src.connectors.whatsapp.connector import (
    _build_envelope as build_whatsapp_envelope,
)
from src.connectors.whatsapp.connector import (
    _ChatBundle as WhatsAppChatBundle,
)
from src.models import JsonValue

TableSpec = Mapping[str, Sequence[str] | None]


class DumpTableReader(Protocol):
    def rows(self, table_name: str) -> list[DumpRow]: ...


WHATSAPP_TABLES: TableSpec = {
    "orgs": None,
    "sessions": None,
    "chats": None,
    "messages": None,
    "contacts": None,
}

BITRIX_TABLES: TableSpec = {
    "categories": None,
    "deals": None,
    "chats": None,
    "personalize_message_logs": None,
    "sent_message_logs": None,
    "templates": None,
    "agents": None,
    "agent_chat": None,
}

PHPPOS_TABLES: TableSpec = {
    "phppos_people": None,
    "phppos_customers": None,
}

SPEEDZONE_PHPPOS_TABLES: TableSpec = PHPPOS_TABLES

FUNDBOX_TABLES: TableSpec = {
    "users": None,
    "basic_profiles": None,
    "basic_plus_profiles": None,
    "addresses": None,
    "social_accounts": None,
    "device_ids": None,
    "last_logins": None,
    "contacts": None,
    "log_legacy_profiles": None,
    "log_legacy_profile_addresses": None,
    "merged_users": None,
    "orders": None,
    "order_items": None,
    "merchant_products": None,
    "product_variants": None,
    "products": None,
    "merchants": None,
}

FUNDBOX_ORDER_STATUSES = {"acknowledged", "to release", "completed"}

PHPPOS_SALES_TABLES: TableSpec = {
    "phppos_sales": None,
    "phppos_sales_items": None,
    "phppos_items": None,
}


def get_dump_connector(source_key: str, dump_path: str | Path) -> SourceConnector:
    """Create a source connector that reads directly from ``dump_path``."""
    path = Path(dump_path)
    factories: dict[str, Callable[[Path], SourceConnector]] = {
        "whatsapp_chat": WhatsAppDumpConnector,
        "bitrix_chat": BitrixDumpConnector,
        "eko_phppos": EkoDumpConnector,
        "speedzone_phppos": SpeedZoneDumpConnector,
        "fundbox_consumer_backend": FundboxDumpConnector,
        "fundbox_consumer_backend:contacts": FundboxContactsDumpConnector,
        "fundbox_consumer_backend:legacy": FundboxLegacyDumpConnector,
        "fundbox_consumer_backend:merged": FundboxMergedUsersDumpConnector,
        "fundbox_consumer_backend:sales": FundboxSalesDumpConnector,
        "eko_phppos:sales": EkoSalesDumpConnector,
        "speedzone_phppos:sales": SpeedZoneSalesDumpConnector,
        "sgbankruptcy": SGGovernmentBankruptcyConnector,
        "sgrentalflats": SGGovernmentRentalFlatsConnector,
    }
    factory = factories.get(source_key)
    if factory is None:
        raise ValueError(f"Dump ingestion is not supported for source_key: {source_key}")
    return factory(path)


class FundboxDumpConnector(SourceConnector):
    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "fundbox_consumer_backend"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, FUNDBOX_TABLES)
        profiles = _single_by_int(tables.rows("basic_profiles"), "user_id")
        plus_profiles = _single_by_int(tables.rows("basic_plus_profiles"), "user_id")
        addresses = _group_by_int(tables.rows("addresses"), "user_id")
        socials = _group_by_int(tables.rows("social_accounts"), "user_id")
        devices = _group_by_int(tables.rows("device_ids"), "user_id")
        last_logins = _single_by_int(tables.rows("last_logins"), "user_id")
        for user in sorted(tables.rows("users"), key=lambda row: _row_int(row, "id")):
            user_id = _row_int(user, "id")
            row = _join_fundbox_user(user, profiles.get(user_id), plus_profiles.get(user_id))
            last_login = last_logins.get(user_id)
            last_login_value = (
                str(last_login.last_logged_in) if last_login and last_login.last_logged_in else None
            )
            yield FundboxConnector._build_one(
                row,
                addresses.get(user_id, []),
                socials.get(user_id, []),
                devices.get(user_id, []),
                last_login_value,
            )


class FundboxContactsDumpConnector(SourceConnector):
    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "fundbox_consumer_backend:contacts"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, FUNDBOX_TABLES)
        for row in sorted(tables.rows("contacts"), key=lambda item: _row_int(item, "id")):
            yield _build_fundbox_contact(row)


class FundboxLegacyDumpConnector(SourceConnector):
    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "fundbox_consumer_backend:legacy"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, FUNDBOX_TABLES)
        addresses = _group_by_int(tables.rows("log_legacy_profile_addresses"), "user_id")
        profiles = sorted(tables.rows("log_legacy_profiles"), key=lambda item: _row_int(item, "id"))
        for row in profiles:
            yield _build_fundbox_legacy(row, addresses.get(_row_int(row, "user_id"), []))


class FundboxMergedUsersDumpConnector(SourceConnector):
    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "fundbox_consumer_backend:merged"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, FUNDBOX_TABLES)
        for row in sorted(tables.rows("merged_users"), key=lambda item: _row_int(item, "id")):
            yield _build_fundbox_merged(row)


class FundboxSalesDumpConnector(SourceConnector):
    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "fundbox_consumer_backend:sales"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, FUNDBOX_TABLES)
        merchants = {
            _row_int(row, "id"): str(row.name or row.official_name or "")
            for row in tables.rows("merchants")
        }
        line_rows = _group_by_int(tables.rows("order_items"), "order_id")
        product_info = _fundbox_product_info(tables)
        builder = FundboxSalesConnector()
        for row in sorted(tables.rows("orders"), key=lambda item: _row_int(item, "id")):
            if str(row.status or "") not in FUNDBOX_ORDER_STATUSES:
                continue
            yield builder._build_one(
                row,
                line_rows.get(_row_int(row, "id"), []),
                merchants,
                product_info,
            )


class EkoSalesDumpConnector(SourceConnector):
    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "eko_phppos:sales"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        yield from _fetch_phppos_dump_sales(self._dump_path, "eko_phppos")


class SpeedZoneSalesDumpConnector(SourceConnector):
    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "speedzone_phppos:sales"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        yield from _fetch_phppos_dump_sales(self._dump_path, "speedzone_phppos")


class WhatsAppDumpConnector(SourceConnector):
    """Yields WhatsApp conversation envelopes from a PostgreSQL SQL dump."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "whatsapp_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, WHATSAPP_TABLES)
        org_name_by_id = {str(row.id): str(row.name or "") for row in tables.rows("orgs")}
        sessions = [
            row
            for row in tables.rows("sessions")
            if str(row.status or "") == "ready"
            and str(row.org_id or "") in org_name_by_id
            and row.whatsapp_user_id
        ]
        chats_by_user = _index_rows(tables.rows("chats"), "whatsapp_user_id")
        messages_by_chat = _index_rows(tables.rows("messages"), "chat_id")
        contacts_by_jid = _index_contacts(tables.rows("contacts"))
        bundles: list[WhatsAppChatBundle] = []

        for session in sessions:
            org_name = org_name_by_id.get(str(session.org_id), "")
            tenant = ORG_TO_ENTITY.get(org_name)
            if tenant is None:
                continue
            whatsapp_uid = str(session.whatsapp_user_id or "")
            for chat in chats_by_user.get(whatsapp_uid, []):
                chat_id = str(chat.id or "")
                msgs: list[dict[str, object]] = [
                    row.as_object_dict() for row in messages_by_chat.get(chat_id, [])
                ]
                if not msgs:
                    continue
                chat_name = str(chat.name or "")
                participants = _whatsapp_participants(chat_id, whatsapp_uid, msgs, contacts_by_jid)
                bundles.append(
                    WhatsAppChatBundle(
                        chat_id=chat_id,
                        chat_name=chat_name,
                        session_id=str(session.id),
                        whatsapp_user_id=whatsapp_uid,
                        tenant=tenant,
                        msg_text=_format_messages(msgs, participants, chat_name),
                        observed_at=to_iso(msgs[-1].get("timestamp")) or "",
                        participants=participants,
                        message_endpoints=_message_endpoints(msgs),
                        session_phone=_phone_from_jid(whatsapp_uid),
                    )
                )

        for bundle, extraction in _run_batches(
            [bundle.msg_text for bundle in bundles],
            WHATSAPP_LLM_BATCH_SIZE,
            run_extraction_batch,
        ):
            if extraction is not None:
                envelope = build_whatsapp_envelope(bundle=bundles[bundle], extraction=extraction)
                if envelope is not None:
                    yield envelope


class BitrixDumpConnector(SourceConnector):
    """Yields Bitrix conversation envelopes from a MySQL/MariaDB SQL dump."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path
        self._builder = BitrixChatConnector()

    def get_source_key(self) -> str:
        return BITRIX_SOURCE_KEY

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, BITRIX_TABLES)
        categories = {_row_int(row, "id"): str(row.name or "") for row in tables.rows("categories")}
        deals = {_row_int(row, "id"): _bitrix_deal(row) for row in tables.rows("deals")}
        personalize_by_chat = _index_rows(tables.rows("personalize_message_logs"), "chat_id")
        sent_by_chat = _index_rows(tables.rows("sent_message_logs"), "chat_id")
        templates = {_row_int(row, "id"): row for row in tables.rows("templates")}
        agents = {_row_int(row, "id"): row for row in tables.rows("agents")}
        agent_chat_by_chat = _index_rows(tables.rows("agent_chat"), "chat_id")
        bundles: list[BitrixChatBundle] = []

        for chat in sorted(tables.rows("chats"), key=lambda row: _row_int(row, "id")):
            deal_id = _row_int(chat, "deal_id")
            deal = deals.get(deal_id)
            if deal is None:
                continue
            category_name = categories.get(_int_value(deal.get("category_id")), "")
            entity = CATEGORY_TO_ENTITY.get(category_name)
            if entity is None:
                continue
            chat_id = _row_int(chat, "id")
            bundles.append(
                BitrixChatBundle(
                    chat_id=chat_id,
                    deal_id=deal_id,
                    bitrix_chat_id=str(chat.bitrix_chat_id or ""),
                    last_message_at=None,
                    created_at=None,
                    category_name=category_name,
                    entity=entity,
                    conv_text=_bitrix_conversation(
                        deal,
                        personalize_by_chat.get(chat_id, []),
                        sent_by_chat.get(chat_id, []),
                        templates,
                    ),
                    deal=_object_mapping(deal),
                    agents=_bitrix_agents(agent_chat_by_chat.get(chat_id, []), agents),
                )
            )

        for bundle_index, extraction in _run_batches(
            [bundle.conv_text for bundle in bundles],
            BITRIX_LLM_BATCH_SIZE,
            run_extraction_batch,
        ):
            if extraction is not None:
                envelope = self._builder._build_envelope(
                    bundle=bundles[bundle_index],
                    extraction=extraction,
                )
                if envelope is not None:
                    yield envelope


class EkoDumpConnector(SourceConnector):
    """Yields Eko POS identity envelopes from a MySQL/MariaDB SQL dump."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "eko_phppos"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, PHPPOS_TABLES)
        people = {_row_int(row, "person_id"): row for row in tables.rows("phppos_people")}
        customers = sorted(tables.rows("phppos_customers"), key=lambda row: _row_int(row, "id"))
        for customer in customers:
            if _int_value(customer.deleted) != 0:
                continue
            person = people.get(_row_int(customer, "person_id"))
            if person is None:
                continue
            yield EkoConnector._build_one(_join_eko_row(person, customer))


class SpeedZoneDumpConnector(SourceConnector):
    """Yields SpeedZone POS identity envelopes from a MySQL/MariaDB SQL dump."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "speedzone_phppos"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, SPEEDZONE_PHPPOS_TABLES)
        people = {_row_int(row, "person_id"): row for row in tables.rows("phppos_people")}
        customers = sorted(tables.rows("phppos_customers"), key=lambda row: _row_int(row, "id"))
        for customer in customers:
            if _int_value(customer.deleted) != 0:
                continue
            person = people.get(_row_int(customer, "person_id"))
            if person is None:
                continue
            yield SpeedZoneConnector._build_envelope_with_customer(
                _join_speedzone_row(person, customer)
            )


def _run_batches(
    texts: list[str],
    batch_size: int,
    extractor: Callable[[list[str]], list[ExtractionResult | None]],
) -> Iterator[tuple[int, ExtractionResult | None]]:
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        for offset, result in enumerate(extractor(batch)):
            yield start + offset, result


def _row_int(row: DumpRow, key: str) -> int:
    return _int_value(row._mapping.get(key))


def _single_by_int(rows: list[DumpRow], key: str) -> dict[int, DumpRow]:
    return {_row_int(row, key): row for row in rows}


def _group_by_int(rows: list[DumpRow], key: str) -> dict[int, list[DumpRow]]:
    indexed: dict[int, list[DumpRow]] = {}
    for row in rows:
        indexed.setdefault(_row_int(row, key), []).append(row)
    return indexed


def _index_rows(rows: list[DumpRow], key: str) -> dict[str | int, list[DumpRow]]:
    indexed: dict[str | int, list[DumpRow]] = {}
    for row in rows:
        value = getattr(row, key)
        if isinstance(value, int):
            index_key: str | int = value
        else:
            index_key = str(value or "")
        indexed.setdefault(index_key, []).append(row)
    return indexed


def _index_contacts(rows: list[DumpRow]) -> dict[tuple[str, str], DumpRow]:
    indexed: dict[tuple[str, str], DumpRow] = {}
    for row in rows:
        whatsapp_user_id = str(row._mapping.get("whatsapp_user_id") or "")
        for key in ("jid", "lid_id", "cus_id"):
            jid = str(row._mapping.get(key) or "")
            if jid:
                indexed[(whatsapp_user_id, jid)] = row
    return indexed


def _whatsapp_participants(
    chat_id: str,
    whatsapp_uid: str,
    msgs: list[dict[str, object]],
    contacts_by_jid: Mapping[tuple[str, str], DumpRow],
) -> list[_Participant]:
    participants: list[_Participant] = []
    for jid in sorted(_participant_jids(chat_id, msgs)):
        contact = contacts_by_jid.get((whatsapp_uid, jid))
        if contact is None:
            participants.append(
                _Participant(
                    jid=jid,
                    phone=_phone_from_jid(jid),
                    name=None,
                    role="chat" if jid == chat_id else "participant",
                )
            )
            continue
        participants.append(
            _Participant(
                jid=jid,
                phone=_first_str(contact.phone_number, contact.number, _phone_from_jid(jid)),
                name=_first_str(contact.name, contact.pushname, contact.short_name),
                role="chat" if jid == chat_id else "participant",
            )
        )
    return participants


def _bitrix_deal(row: DumpRow) -> dict[str, JsonValue]:
    return {
        "id": row.id,
        "bitrix_deal_id": row.bitrix_deal_id,
        "title": row.title,
        "stage_id": row.stage_id,
        "opened": bool(row.opened),
        "closed": bool(row.closed),
        "category_id": row.category_id,
    }


def _bitrix_conversation(
    deal: Mapping[str, JsonValue],
    personalize_rows: list[DumpRow],
    sent_rows: list[DumpRow],
    templates: Mapping[int, DumpRow],
) -> str:
    lines: list[str] = []
    title = str(deal.get("title") or "")
    if title:
        lines.append(f"[Deal] {title}")
    for row in personalize_rows:
        ts = str(row.created_at or "")
        client_name = str(row.client_name or "").strip()
        if client_name:
            lines.append(f"[{ts}] Client: {client_name}")
        body = str(row.message_sent or row.llm_message or "").strip()
        if body:
            lines.append(f"[{ts}] Sent: {body}")
    for row in sent_rows:
        template = templates.get(_int_value(row.template_id))
        if template is None:
            continue
        body = str(template.content or "").strip()
        if body:
            lines.append(f"[{row.created_at or ''}] Template: {body}")
    return "\n".join(lines)


def _bitrix_agents(
    agent_chat_rows: list[DumpRow], agents: Mapping[int, DumpRow]
) -> list[_AgentMember]:
    members: list[_AgentMember] = []
    for link in agent_chat_rows:
        agent = agents.get(_int_value(link.agent_id))
        if agent is None:
            continue
        members.append(
            _AgentMember(
                bitrix_agent_id=str(agent.bitrix_agent_id or ""),
                name=str(agent.name or ""),
                active=bool(agent.active),
            )
        )
    return members


def _join_fundbox_user(
    user: DumpRow, profile: DumpRow | None, plus_profile: DumpRow | None
) -> DumpRow:
    return DumpRow(
        {
            **user.as_dict(),
            "user_id": user.id,
            "user_email": user.email,
            "user_mobile": user.mobile_number,
            "user_created_at": user.created_at,
            "user_updated_at": user.updated_at,
            "nric": profile.nric if profile else None,
            "full_name": profile.full_name if profile else None,
            "date_of_birth": profile.date_of_birth if profile else None,
            "gender": profile.gender if profile else None,
            "nationality": profile.nationality if profile else None,
            "profile_email": profile.email if profile else None,
            "profile_mobile": profile.mobile_number if profile else None,
            "whatsapp_phone": profile.get("whatsapp_phone") if profile else None,
            "facebook_id": plus_profile.get("facebook_id") if plus_profile else None,
        }
    )


def _fundbox_product_info(tables: DumpTableReader) -> dict[int, dict[str, JsonValue]]:
    products = _single_by_int(tables.rows("products"), "id")
    variants = _single_by_int(tables.rows("product_variants"), "id")
    result: dict[int, dict[str, JsonValue]] = {}
    for merchant_product in tables.rows("merchant_products"):
        variant = variants.get(_row_int(merchant_product, "product_variant_id"))
        if variant is None:
            continue
        product = products.get(_row_int(variant, "product_id"))
        result[_row_int(merchant_product, "id")] = _variant_to_product(variant, product)
    return result


def _build_fundbox_contact(row: DumpRow) -> dict[str, JsonValue]:
    ids = IdentifierBag()
    ids.add("phone", row.mobile_number)
    return build_envelope(
        source_record_id=f"fundbox_consumer_backend-contact-{row.id}",
        observed_at=to_iso(row.updated_at or row.created_at),
        identifiers=ids.items,
        attributes={
            "full_name": row.full_name,
            "relationship_to_referrer": row.relationship,
        },
        raw_payload={
            "contact": serialize_row(row),
            "linked_to_source_record_id": f"fundbox_consumer_backend-user-{row.user_id}",
            "link_type": row.relationship,
        },
    )


def _build_fundbox_legacy(row: DumpRow, user_addresses: list[DumpRow]) -> dict[str, JsonValue]:
    ids = IdentifierBag()
    ids.add("nric", row.nric, verified=True)
    ids.add("email", row.email)
    ids.add("phone", row.mobile_number)
    ids.add("phone", row.whatsapp_phone)
    ids.add("social:facebook", row.facebook_id)
    return build_envelope(
        source_record_id=f"fundbox_consumer_backend-legacy-{row.id}",
        observed_at=to_iso(row.updated_at or row.created_at),
        identifiers=ids.items,
        attributes={
            "full_name": row.full_name,
            "dob": to_iso(row.date_of_birth),
            "gender": row.gender,
            "nationality": row.nationality,
            "address": format_address(user_addresses[0]) if user_addresses else None,
        },
        raw_payload={
            "legacy_profile": serialize_row(row),
            "addresses": [serialize_row(address) for address in user_addresses],
        },
    )


def _build_fundbox_merged(row: DumpRow) -> dict[str, JsonValue]:
    ids = IdentifierBag()
    ids.add("nric", row.nric, verified=True)
    ids.add("email", row.email)
    ids.add("phone", row.mobile_number)
    return build_envelope(
        source_record_id=f"fundbox_consumer_backend-merged-{row.id}",
        observed_at=to_iso(row.updated_at or row.created_at),
        identifiers=ids.items,
        attributes={},
        raw_payload={
            "merged_user": serialize_row(row),
            "merge_hint": {
                "merged_into_source_record_id": (
                    f"fundbox_consumer_backend-user-{row.new_user_id}" if row.new_user_id else None
                ),
                "surviving_identifiers": {
                    "nric": row.new_nric,
                    "email": row.new_email,
                    "phone": row.new_mobile_number,
                },
            },
        },
    )


def _fetch_phppos_dump_sales(
    dump_path: Path, source_system_key: str
) -> Iterator[dict[str, JsonValue]]:
    tables = load_dump_tables(dump_path, PHPPOS_SALES_TABLES)
    items_by_id = {_row_int(row, "item_id"): row for row in tables.rows("phppos_items")}
    lines_by_sale = _group_by_int(tables.rows("phppos_sales_items"), "sale_id")
    for sale in sorted(tables.rows("phppos_sales"), key=lambda row: _row_int(row, "sale_id")):
        sale_id = _row_int(sale, "sale_id")
        yield _build_phppos_sales_envelope(
            sale,
            lines_by_sale.get(sale_id, []),
            items_by_id,
            source_system_key,
        )


def _build_phppos_sales_envelope(
    sale: DumpRow,
    line_rows: list[DumpRow],
    items_by_id: Mapping[int, DumpRow],
    source_system_key: str,
) -> dict[str, JsonValue]:
    source_order_id = str(sale.sale_id)
    line_items: list[JsonValue] = []
    for line in line_rows:
        item = items_by_id.get(_row_int(line, "item_id"))
        product = _phppos_product_payload(item, source_system_key) if item else None
        quantity = _float_value(line.quantity_purchased)
        unit_price = _float_value(line.item_unit_price)
        discount = _float_value(line.discount_percent)
        line_total = quantity * unit_price * (1 - discount / 100)
        line_items.append(
            {
                "source_line_id": f"{source_system_key}-sale-{sale.sale_id}-line-{line.line}",
                "source_line_item_id": f"{source_system_key}-sale-{sale.sale_id}-line-{line.line}",
                "quantity": quantity,
                "unit_price": unit_price,
                "discount_amount": None,
                "line_total": line_total,
                "serial_number": line.serialnumber,
                "metadata": {"serialnumber": line.serialnumber},
                "raw": serialize_row(line),
                "product": product,
            }
        )
    return build_envelope(
        source_record_id=f"{source_system_key}-sale-{sale.sale_id}",
        observed_at=to_iso(sale.sale_time or sale.invoice_date),
        identifiers=[],
        attributes={},
        record_type="sales",
        raw_payload={
            "order": {
                "source_order_id": source_order_id,
                "order_no": sale.get("invoice_number"),
                "ordered_at": to_iso(sale.get("sale_time")),
                "status": sale.get("sale_status") or sale.get("suspended"),
                "currency": "SGD",
                "total_amount": sum(_json_float(item, "line_total") for item in line_items),
                "raw": serialize_row(sale),
            },
            "line_items": line_items,
            "customer_link": {
                "identity_source_record_id": (
                    f"{source_system_key}-customer-{sale.customer_id}"
                    if sale.customer_id is not None
                    else None
                ),
                "source_system_key": source_system_key,
            },
        },
    )


def _phppos_product_payload(item: DumpRow, source_system_key: str) -> dict[str, JsonValue]:
    return {
        "source_product_id": f"{source_system_key}-item-{item.item_id}",
        "sku": item.get("item_number"),
        "name": item.get("name"),
        "display_name": item.get("name"),
        "category": item.get("category"),
        "subcategory": item.get("subcategory"),
        "manufacturer": None,
        "is_active": True,
        "attributes": {
            "size": item.get("size"),
            "cost_price": _float_or_none(item.get("cost_price")),
            "unit_price": _float_or_none(item.get("unit_price")),
            "description": item.get("description"),
        },
    }


def _join_eko_row(person: DumpRow, customer: DumpRow) -> DumpRow:
    return DumpRow(
        {
            **person.as_dict(),
            "customer_id": customer.id,
            "account_number": customer.account_number,
            "company_name": customer.company_name,
            "custom_field_1_value": customer.custom_field_1_value,
            "custom_field_2_value": customer.custom_field_2_value,
            "custom_field_3_value": customer.custom_field_3_value,
            "custom_field_4_value": customer.custom_field_4_value,
            "custom_field_5_value": customer.custom_field_5_value,
            "custom_field_6_value": customer.custom_field_6_value,
            "custom_field_7_value": customer.custom_field_7_value,
            "custom_field_8_value": customer.custom_field_8_value,
            "custom_field_9_value": customer.custom_field_9_value,
            "custom_field_10_value": customer.custom_field_10_value,
        }
    )


def _join_speedzone_row(person: DumpRow, customer: DumpRow) -> DumpRow:
    return DumpRow(
        {
            **person.as_dict(),
            "customer_id": customer.id,
            "account_number": customer.account_number,
            "company_name": customer.company_name,
            "custom_field_1_value": customer.custom_field_1_value,
            "custom_field_2_value": customer.custom_field_2_value,
            "custom_field_3_value": customer.custom_field_3_value,
            "custom_field_4_value": customer.custom_field_4_value,
            "custom_field_5_value": customer.custom_field_5_value,
            "custom_field_6_value": customer.custom_field_6_value,
            "custom_field_7_value": customer.custom_field_7_value,
            "custom_field_8_value": customer.custom_field_8_value,
            "custom_field_9_value": customer.custom_field_9_value,
            "custom_field_10_value": customer.custom_field_10_value,
        }
    )


def _json_float(item: JsonValue, key: str) -> float:
    if isinstance(item, dict):
        return _float_value(item.get(key))
    return 0.0


def _float_or_none(value: JsonValue) -> float | None:
    if value is None:
        return None
    return _float_value(value)


def _float_value(value: JsonValue) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _object_mapping(values: Mapping[str, JsonValue]) -> dict[str, object]:
    return dict(values)


def _int_value(value: JsonValue) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return 0
