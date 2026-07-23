"""Direct dump-backed source connectors."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import cast

from sqlalchemy.engine import RowMapping

from src.connectors.base import SourceConnector
from src.connectors.bitrix.connector import (
    BITRIX_SOURCE_KEY,
    CATEGORY_TO_ENTITY,
    BitrixChatConnector,
    _AgentMember,
)
from src.connectors.bitrix.connector import (
    _ChatBundle as BitrixChatBundle,
)
from src.connectors.chat_helpers import (
    ExtractionResult,
    chat_batch_max_chars,
    chat_batch_size,
    iter_char_batches,
    run_extraction_batch,
)
from src.connectors.dumps.reader import DumpRow, iter_dump_rows
from src.connectors.eko.connector import EkoConnector
from src.connectors.fundbox.builders import (
    IdentifierBag,
    _norm_race,
    addresses_from_rows,
    build_envelope,
    format_address,
    serialize_row,
    to_iso,
)
from src.connectors.fundbox.sales import (
    FundboxSalesConnector,
    _CustomerContact,
    _variant_to_product,
)
from src.connectors.fundbox.users import FundboxConnector
from src.connectors.onediver.connector import (
    OneDiverDumpConnector,
    OneDiverSalesDumpConnector,
)
from src.connectors.phppos_sales_common import (
    phppos_customer_bike_plate,
    phppos_customer_nric,
    phppos_resolve_category_name,
)
from src.connectors.sggov.bankruptcy import SGGovernmentBankruptcyConnector
from src.connectors.sggov.rental_flats import SGGovernmentRentalFlatsConnector
from src.connectors.speedzone.connector import SpeedZoneConnector
from src.connectors.whatsapp.connector import (
    ORG_TO_ENTITY,
    _first_str,
    _format_messages,
    _latest_message_timestamp,
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
_DUMP_BATCH_SIZE = 1000

PHPPOS_SALES_TABLES: TableSpec = {
    "phppos_sales": None,
    "phppos_sales_items": None,
    "phppos_items": None,
    # category_id -> name for vehicle classification (resolves phppos_items.category
    # from an integer FK to the human-readable category name).
    "phppos_categories": None,
    # Customer bike plate (custom_field_8_value/custom_field_10_value for speedzone)
    # + NRIC (custom_field_1_value for all phppos) used by the matching heuristic's
    # NRIC anti-match (Task 6).
    "phppos_customers": None,
    # Customer contact channels (email + phone_number) — the live connector
    # joins phppos_people to phppos_customers for these; the dump mirrors that
    # so the Vehicle heuristic (Task 6) can read sale-level email/phone from
    # raw_payload without re-joining.
    "phppos_people": None,
}


def get_dump_connector(source_key: str, dump_path: str | Path) -> SourceConnector:
    """Create a source connector that reads directly from ``dump_path``."""
    path = Path(dump_path)
    factories: dict[str, Callable[[Path], SourceConnector]] = {
        "whatsapp_chat": WhatsAppDumpConnector,
        "bitrix_chat": BitrixDumpConnector,
        "eko_phppos": EkoDumpConnector,
        "speedzone_phppos": SpeedZoneDumpConnector,
        "onediver": OneDiverDumpConnector,
        "onediver:sales": OneDiverSalesDumpConnector,
        "fundbox": FundboxDumpConnector,
        "fundbox:contacts": FundboxContactsDumpConnector,
        "fundbox:legacy": FundboxLegacyDumpConnector,
        "fundbox:merged": FundboxMergedUsersDumpConnector,
        "fundbox:sales": FundboxSalesDumpConnector,
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
        return "fundbox"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        users = _table_rows(self._dump_path, "users", FUNDBOX_TABLES)
        for user_batch in _dump_row_batches(users):
            user_ids = {_row_int(row, "id") for row in user_batch}
            profiles = _single_by_int(
                _rows_for_int_keys(
                    self._dump_path, "basic_profiles", FUNDBOX_TABLES, "user_id", user_ids
                ),
                "user_id",
            )
            plus_profiles = _single_by_int(
                _rows_for_int_keys(
                    self._dump_path,
                    "basic_plus_profiles",
                    FUNDBOX_TABLES,
                    "user_id",
                    user_ids,
                ),
                "user_id",
            )
            addresses = _group_by_int(
                _rows_for_int_keys(
                    self._dump_path, "addresses", FUNDBOX_TABLES, "user_id", user_ids
                ),
                "user_id",
            )
            socials = _group_by_int(
                _rows_for_int_keys(
                    self._dump_path, "social_accounts", FUNDBOX_TABLES, "user_id", user_ids
                ),
                "user_id",
            )
            devices = _group_by_int(
                _rows_for_int_keys(
                    self._dump_path, "device_ids", FUNDBOX_TABLES, "user_id", user_ids
                ),
                "user_id",
            )
            last_logins = _single_by_int(
                _rows_for_int_keys(
                    self._dump_path, "last_logins", FUNDBOX_TABLES, "user_id", user_ids
                ),
                "user_id",
            )
            for user in sorted(user_batch, key=lambda row: _row_int(row, "id")):
                user_id = _row_int(user, "id")
                row = _join_fundbox_user(user, profiles.get(user_id), plus_profiles.get(user_id))
                last_login = last_logins.get(user_id)
                last_login_value = (
                    str(last_login.last_logged_in)
                    if last_login and last_login.last_logged_in
                    else None
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
        return "fundbox:contacts"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        rows = _table_rows(self._dump_path, "contacts", FUNDBOX_TABLES)
        for batch in _dump_row_batches(rows):
            for row in sorted(batch, key=lambda item: _row_int(item, "id")):
                yield _build_fundbox_contact(row)


class FundboxLegacyDumpConnector(SourceConnector):
    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "fundbox:legacy"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        profiles = _table_rows(self._dump_path, "log_legacy_profiles", FUNDBOX_TABLES)
        for profile_batch in _dump_row_batches(profiles):
            user_ids = {_row_int(row, "user_id") for row in profile_batch}
            addresses = _group_by_int(
                _rows_for_int_keys(
                    self._dump_path,
                    "log_legacy_profile_addresses",
                    FUNDBOX_TABLES,
                    "user_id",
                    user_ids,
                ),
                "user_id",
            )
            for row in sorted(profile_batch, key=lambda item: _row_int(item, "id")):
                yield _build_fundbox_legacy(
                    row,
                    addresses.get(_row_int(row, "user_id"), []),
                )


class FundboxMergedUsersDumpConnector(SourceConnector):
    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "fundbox:merged"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        rows = _table_rows(self._dump_path, "merged_users", FUNDBOX_TABLES)
        for batch in _dump_row_batches(rows):
            for row in sorted(batch, key=lambda item: _row_int(item, "id")):
                yield _build_fundbox_merged(row)


class FundboxSalesDumpConnector(SourceConnector):
    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "fundbox:sales"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        builder = FundboxSalesConnector()
        eligible_orders = (
            row
            for row in _table_rows(self._dump_path, "orders", FUNDBOX_TABLES)
            if str(row.status or "") in FUNDBOX_ORDER_STATUSES
        )
        for order_batch in _dump_row_batches(eligible_orders):
            order_ids = {_row_int(row, "id") for row in order_batch}
            user_ids = {_row_int(row, "user_id") for row in order_batch}
            merchant_ids = {_row_int(row, "merchant_id") for row in order_batch}
            line_rows = _group_by_int(
                _rows_for_int_keys(
                    self._dump_path, "order_items", FUNDBOX_TABLES, "order_id", order_ids
                ),
                "order_id",
            )
            merchant_product_ids = {
                _row_int(line, "merchant_product_id")
                for lines in line_rows.values()
                for line in lines
            }
            merchants = {
                _row_int(row, "id"): str(row.name or row.official_name or "")
                for row in _rows_for_int_keys(
                    self._dump_path, "merchants", FUNDBOX_TABLES, "id", merchant_ids
                )
            }
            product_info = _fundbox_product_info_for_ids(
                self._dump_path,
                merchant_product_ids,
            )
            customer_contacts = _fundbox_customer_contacts_for_ids(
                self._dump_path,
                user_ids,
            )
            for row in sorted(order_batch, key=lambda item: _row_int(item, "id")):
                user_id = _row_int(row, "user_id")
                yield builder._build_one(
                    cast(RowMapping, row),
                    cast(list[RowMapping], line_rows.get(_row_int(row, "id"), [])),
                    merchants,
                    product_info,
                    customer_contacts.get(user_id),
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
        org_name_by_id = {
            str(row.id): str(row.name or "")
            for row in _table_rows(self._dump_path, "orgs", WHATSAPP_TABLES)
        }
        sessions = (
            row
            for row in _table_rows(self._dump_path, "sessions", WHATSAPP_TABLES)
            if str(row.status or "") == "ready"
            and str(row.org_id or "") in org_name_by_id
            and row.whatsapp_user_id
        )
        for session_batch in _dump_row_batches(sessions):
            sessions_by_user: dict[str, list[DumpRow]] = {}
            for session in session_batch:
                whatsapp_uid = str(session.whatsapp_user_id or "")
                sessions_by_user.setdefault(whatsapp_uid, []).append(session)
            chats = (
                row
                for row in _table_rows(self._dump_path, "chats", WHATSAPP_TABLES)
                if str(row.whatsapp_user_id or "") in sessions_by_user
            )
            for chat_batch in _dump_row_batches(chats):
                yield from self._process_chat_batch(
                    chat_batch,
                    sessions_by_user,
                    org_name_by_id,
                )

    def _process_chat_batch(
        self,
        chat_batch: list[DumpRow],
        sessions_by_user: Mapping[str, list[DumpRow]],
        org_name_by_id: Mapping[str, str],
    ) -> Iterator[dict[str, JsonValue]]:
        chat_ids = {str(row.id or "") for row in chat_batch}
        message_rows = [
            row
            for row in _table_rows(self._dump_path, "messages", WHATSAPP_TABLES)
            if str(row.chat_id or "") in chat_ids
        ]
        messages_by_chat = _index_rows(message_rows, "chat_id")
        jids_by_user: dict[str, set[str]] = {}
        for chat in chat_batch:
            whatsapp_uid = str(chat.whatsapp_user_id or "")
            chat_id = str(chat.id or "")
            messages = [row.as_object_dict() for row in messages_by_chat.get(chat_id, [])]
            jids_by_user.setdefault(whatsapp_uid, set()).update(
                _participant_jids(chat_id, messages)
            )
        contact_rows = [
            row
            for row in _table_rows(self._dump_path, "contacts", WHATSAPP_TABLES)
            if _contact_is_relevant(row, jids_by_user)
        ]
        contacts_by_jid = _index_contacts(contact_rows)
        bundles: list[WhatsAppChatBundle] = []
        for chat in chat_batch:
            whatsapp_uid = str(chat.whatsapp_user_id or "")
            chat_id = str(chat.id or "")
            messages = [row.as_object_dict() for row in messages_by_chat.get(chat_id, [])]
            if not messages:
                continue
            chat_name = str(chat.name or "")
            participants = _whatsapp_participants(
                chat_id,
                whatsapp_uid,
                messages,
                contacts_by_jid,
            )
            for session in sessions_by_user.get(whatsapp_uid, []):
                org_name = org_name_by_id.get(str(session.org_id or ""), "")
                tenant = ORG_TO_ENTITY.get(org_name)
                if tenant is None:
                    continue
                bundles.append(
                    WhatsAppChatBundle(
                        chat_id=chat_id,
                        chat_name=chat_name,
                        session_id=str(session.id),
                        whatsapp_user_id=whatsapp_uid,
                        tenant=tenant,
                        msg_text=_format_messages(messages, participants, chat_name),
                        observed_at=_latest_message_timestamp(messages),
                        participants=participants,
                        message_endpoints=_message_endpoints(messages),
                        session_phone=_phone_from_jid(whatsapp_uid),
                    )
                )
        yield from _extract_whatsapp_dump_bundles(bundles)


class BitrixDumpConnector(SourceConnector):
    """Yields Bitrix conversation envelopes from a MySQL/MariaDB SQL dump."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path
        self._builder = BitrixChatConnector()

    def get_source_key(self) -> str:
        return BITRIX_SOURCE_KEY

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        categories = {
            _row_int(row, "id"): str(row.name or "")
            for row in _table_rows(self._dump_path, "categories", BITRIX_TABLES)
        }
        chats = _table_rows(self._dump_path, "chats", BITRIX_TABLES)
        for chat_batch in _dump_row_batches(chats):
            yield from self._process_chat_batch(chat_batch, categories)

    def _process_chat_batch(
        self,
        chat_batch: list[DumpRow],
        categories: Mapping[int, str],
    ) -> Iterator[dict[str, JsonValue]]:
        chat_ids = {_row_int(row, "id") for row in chat_batch}
        deal_ids = {_row_int(row, "deal_id") for row in chat_batch}
        deals = {
            _row_int(row, "id"): _bitrix_deal(row)
            for row in _rows_for_int_keys(self._dump_path, "deals", BITRIX_TABLES, "id", deal_ids)
        }
        personalize_by_chat = _index_rows(
            _rows_for_int_keys(
                self._dump_path,
                "personalize_message_logs",
                BITRIX_TABLES,
                "chat_id",
                chat_ids,
            ),
            "chat_id",
        )
        sent_rows = _rows_for_int_keys(
            self._dump_path,
            "sent_message_logs",
            BITRIX_TABLES,
            "chat_id",
            chat_ids,
        )
        sent_by_chat = _index_rows(sent_rows, "chat_id")
        template_ids = {_row_int(row, "template_id") for row in sent_rows}
        templates = {
            _row_int(row, "id"): row
            for row in _rows_for_int_keys(
                self._dump_path, "templates", BITRIX_TABLES, "id", template_ids
            )
        }
        agent_chat_rows = _rows_for_int_keys(
            self._dump_path,
            "agent_chat",
            BITRIX_TABLES,
            "chat_id",
            chat_ids,
        )
        agent_chat_by_chat = _index_rows(agent_chat_rows, "chat_id")
        agent_ids = {_row_int(row, "agent_id") for row in agent_chat_rows}
        agents = {
            _row_int(row, "id"): row
            for row in _rows_for_int_keys(self._dump_path, "agents", BITRIX_TABLES, "id", agent_ids)
        }
        bundles: list[BitrixChatBundle] = []
        for chat in sorted(chat_batch, key=lambda row: _row_int(row, "id")):
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
            chat_batch_max_chars(),
            chat_batch_size(),
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
        customers = (
            row
            for row in _table_rows(self._dump_path, "phppos_customers", PHPPOS_TABLES)
            if _int_value(row.deleted) == 0
        )
        for customer_batch in _dump_row_batches(customers):
            person_ids = {_row_int(row, "person_id") for row in customer_batch}
            people = {
                _row_int(row, "person_id"): row
                for row in _rows_for_int_keys(
                    self._dump_path,
                    "phppos_people",
                    PHPPOS_TABLES,
                    "person_id",
                    person_ids,
                )
            }
            for customer in sorted(customer_batch, key=lambda row: _row_int(row, "id")):
                person = people.get(_row_int(customer, "person_id"))
                if person is not None:
                    yield EkoConnector._build_one(_join_eko_row(person, customer))


class SpeedZoneDumpConnector(SourceConnector):
    """Yields SpeedZone POS identity envelopes from a MySQL/MariaDB SQL dump."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "speedzone_phppos"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        customers = (
            row
            for row in _table_rows(
                self._dump_path,
                "phppos_customers",
                SPEEDZONE_PHPPOS_TABLES,
            )
            if _int_value(row.deleted) == 0
        )
        for customer_batch in _dump_row_batches(customers):
            person_ids = {_row_int(row, "person_id") for row in customer_batch}
            people = {
                _row_int(row, "person_id"): row
                for row in _rows_for_int_keys(
                    self._dump_path,
                    "phppos_people",
                    SPEEDZONE_PHPPOS_TABLES,
                    "person_id",
                    person_ids,
                )
            }
            for customer in sorted(customer_batch, key=lambda row: _row_int(row, "id")):
                person = people.get(_row_int(customer, "person_id"))
                if person is not None:
                    yield SpeedZoneConnector._build_envelope_with_customer(
                        _join_speedzone_row(person, customer)
                    )


def _run_batches(
    texts: list[str],
    max_chars: int,
    max_count: int,
    extractor: Callable[[list[str]], list[ExtractionResult | None]],
) -> Iterator[tuple[int, ExtractionResult | None]]:
    for start, end in iter_char_batches(texts, max_chars, max_count):
        for offset, result in enumerate(extractor(texts[start:end])):
            yield start + offset, result


def _table_rows(
    dump_path: Path,
    table_name: str,
    table_spec: TableSpec,
) -> Iterator[DumpRow]:
    return iter_dump_rows(dump_path, table_name, table_spec[table_name])


def _dump_row_batches(rows: Iterable[DumpRow]) -> Iterator[list[DumpRow]]:
    batch: list[DumpRow] = []
    for row in rows:
        batch.append(row)
        if len(batch) == _DUMP_BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def _rows_for_int_keys(
    dump_path: Path,
    table_name: str,
    table_spec: TableSpec,
    key: str,
    values: set[int],
) -> list[DumpRow]:
    if not values:
        return []
    return [
        row
        for row in _table_rows(dump_path, table_name, table_spec)
        if _row_int(row, key) in values
    ]


def _contact_is_relevant(row: DumpRow, jids_by_user: Mapping[str, set[str]]) -> bool:
    whatsapp_user_id = str(row._mapping.get("whatsapp_user_id") or "")
    candidate_jids = {str(row._mapping.get(key) or "") for key in ("jid", "lid_id", "cus_id")}
    candidate_jids.discard("")
    if whatsapp_user_id:
        return bool(candidate_jids & jids_by_user.get(whatsapp_user_id, set()))
    return any(candidate_jids & jids for jids in jids_by_user.values())


def _extract_whatsapp_dump_bundles(
    bundles: list[WhatsAppChatBundle],
) -> Iterator[dict[str, JsonValue]]:
    for bundle_index, extraction in _run_batches(
        [bundle.msg_text for bundle in bundles],
        chat_batch_max_chars(),
        chat_batch_size(),
        run_extraction_batch,
    ):
        if extraction is None:
            continue
        envelope = build_whatsapp_envelope(
            bundle=bundles[bundle_index],
            extraction=extraction,
        )
        if envelope is not None:
            yield envelope


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
    events: list[tuple[str, int, str]] = []
    for row in personalize_rows:
        ts = str(row.created_at or "")
        row_id = _int_value(row.id)
        client_name = str(row.client_name or "").strip()
        if client_name:
            events.append((ts, row_id, f"[{ts}] Client: {client_name}"))
        body = str(row.message_sent or row.llm_message or "").strip()
        if body:
            events.append((ts, row_id, f"[{ts}] Sent: {body}"))
    for row in sent_rows:
        template = templates.get(_int_value(row.template_id))
        if template is None:
            continue
        ts = str(row.created_at or "")
        body = str(template.content or "").strip()
        if body:
            events.append((ts, _int_value(row.id), f"[{ts}] Template: {body}"))
    lines.extend(line for _ts, _row_id, line in sorted(events))
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
            "race": profile.race if profile else None,
            "profile_email": profile.email if profile else None,
            "profile_mobile": profile.mobile_number if profile else None,
            "whatsapp_phone": profile.get("whatsapp_phone") if profile else None,
            "facebook_id": plus_profile.get("facebook_id") if plus_profile else None,
        }
    )


def _fundbox_product_info_for_ids(
    dump_path: Path,
    merchant_product_ids: set[int],
) -> dict[int, dict[str, JsonValue]]:
    merchant_products = _rows_for_int_keys(
        dump_path,
        "merchant_products",
        FUNDBOX_TABLES,
        "id",
        merchant_product_ids,
    )
    variant_ids = {_row_int(row, "product_variant_id") for row in merchant_products}
    variants = _single_by_int(
        _rows_for_int_keys(
            dump_path,
            "product_variants",
            FUNDBOX_TABLES,
            "id",
            variant_ids,
        ),
        "id",
    )
    product_ids = {_row_int(row, "product_id") for row in variants.values()}
    products = _single_by_int(
        _rows_for_int_keys(
            dump_path,
            "products",
            FUNDBOX_TABLES,
            "id",
            product_ids,
        ),
        "id",
    )
    result: dict[int, dict[str, JsonValue]] = {}
    for merchant_product in merchant_products:
        variant = variants.get(_row_int(merchant_product, "product_variant_id"))
        if variant is None:
            continue
        product = products.get(_row_int(variant, "product_id"))
        result[_row_int(merchant_product, "id")] = _variant_to_product(
            cast(RowMapping, variant),
            cast(RowMapping | None, product),
        )
    return result


def _fundbox_customer_contacts_for_ids(
    dump_path: Path,
    user_ids: set[int],
) -> dict[int, _CustomerContact]:
    """Build ``user_id -> {customer_emails, customer_phones, customer_nric}``
    from the dump's ``users`` + ``basic_profiles`` rows.

    Mirrors ``FundboxSalesConnector._fetch_customer_contacts`` so the dump
    path emits the same sale-level contact channels the live path does.
    Emails/phones are deduped non-empty values across the two tables;
    ``customer_nric`` comes from the first ``basic_profiles`` row per user
    (lowest ``id``, matching the live connector's deterministic pick).
    """
    user_rows = _single_by_int(
        _rows_for_int_keys(dump_path, "users", FUNDBOX_TABLES, "id", user_ids),
        "id",
    )
    # ``basic_profiles`` is 1-to-many on ``user_id``; keep the first by id.
    profile_by_user: dict[int, DumpRow] = {}
    profile_rows = _rows_for_int_keys(
        dump_path,
        "basic_profiles",
        FUNDBOX_TABLES,
        "user_id",
        user_ids,
    )
    for row in sorted(profile_rows, key=lambda item: _row_int(item, "id")):
        uid = _row_int(row, "user_id")
        if uid not in profile_by_user:
            profile_by_user[uid] = row

    contacts: dict[int, _CustomerContact] = {}
    for uid, u in user_rows.items():
        p = profile_by_user.get(uid)
        emails: list[str] = []
        phones: list[str] = []
        for src in (u, p):
            if src is None:
                continue
            email = src._mapping.get("email")
            if isinstance(email, str) and email and email not in emails:
                emails.append(email)
            mobile = src._mapping.get("mobile_number")
            if isinstance(mobile, str) and mobile and mobile not in phones:
                phones.append(mobile)
        nric_value = p._mapping.get("nric") if p is not None else None
        nric: str | None = nric_value if isinstance(nric_value, str) and nric_value else None
        contacts[uid] = {
            "customer_emails": emails,
            "customer_phones": phones,
            "customer_nric": nric,
        }
    return contacts


def _build_fundbox_contact(row: DumpRow) -> dict[str, JsonValue]:
    ids = IdentifierBag()
    ids.add("phone", row.mobile_number)
    return build_envelope(
        source_record_id=f"fundbox-contact-{row.id}",
        observed_at=to_iso(row.updated_at or row.created_at),
        identifiers=ids.items,
        record_type="relationship",
        attributes={
            "full_name": row.full_name,
            "relationship_to_referrer": row.relationship,
        },
        raw_payload={
            "contact": serialize_row(row),
            "linked_to_source_record_id": f"fundbox-user-{row.user_id}",
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
    address_rows = addresses_from_rows(user_addresses)
    return build_envelope(
        source_record_id=f"fundbox-legacy-{row.id}",
        observed_at=to_iso(row.updated_at or row.created_at),
        identifiers=ids.items,
        attributes={
            "full_name": row.full_name,
            "dob": to_iso(row.date_of_birth),
            "gender": row.gender,
            "nationality": row.nationality,
            "race_ethnicity": _norm_race(row.race),
            "address": format_address(user_addresses[0]) if user_addresses else None,
        },
        raw_payload={
            "legacy_profile": serialize_row(row),
            "addresses": [serialize_row(address) for address in user_addresses],
        },
        addresses=address_rows,
    )


def _build_fundbox_merged(row: DumpRow) -> dict[str, JsonValue]:
    ids = IdentifierBag()
    ids.add("nric", row.nric, verified=True)
    ids.add("email", row.email)
    ids.add("phone", row.mobile_number)
    return build_envelope(
        source_record_id=f"fundbox-merged-{row.id}",
        observed_at=to_iso(row.updated_at or row.created_at),
        identifiers=ids.items,
        attributes={},
        raw_payload={
            "merged_user": serialize_row(row),
            "merge_hint": {
                "merged_into_source_record_id": (
                    f"fundbox-user-{row.new_user_id}" if row.new_user_id else None
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
    extract_bike_plate = source_system_key == "speedzone_phppos"
    sales = _table_rows(dump_path, "phppos_sales", PHPPOS_SALES_TABLES)
    for sale_batch in _dump_row_batches(sales):
        sale_ids = {_row_int(row, "sale_id") for row in sale_batch}
        customer_ids = {_row_int(row, "customer_id") for row in sale_batch}
        lines_by_sale = _group_by_int(
            _rows_for_int_keys(
                dump_path,
                "phppos_sales_items",
                PHPPOS_SALES_TABLES,
                "sale_id",
                sale_ids,
            ),
            "sale_id",
        )
        item_ids = {
            _row_int(line, "item_id") for line_rows in lines_by_sale.values() for line in line_rows
        }
        items_by_id = {
            _row_int(row, "item_id"): row
            for row in _rows_for_int_keys(
                dump_path,
                "phppos_items",
                PHPPOS_SALES_TABLES,
                "item_id",
                item_ids,
            )
        }
        category_ids = {_row_int(row, "category") for row in items_by_id.values()}
        categories = {
            _row_int(row, "id"): str(row.get("name") or "")
            for row in _rows_for_int_keys(
                dump_path,
                "phppos_categories",
                PHPPOS_SALES_TABLES,
                "id",
                category_ids,
            )
        }
        customers_by_person_id = {
            _row_int(row, "person_id"): row
            for row in _rows_for_int_keys(
                dump_path,
                "phppos_customers",
                PHPPOS_SALES_TABLES,
                "person_id",
                customer_ids,
            )
        }
        people_by_id = {
            _row_int(row, "person_id"): row
            for row in _rows_for_int_keys(
                dump_path,
                "phppos_people",
                PHPPOS_SALES_TABLES,
                "person_id",
                customer_ids,
            )
        }
        for sale in sorted(sale_batch, key=lambda row: _row_int(row, "sale_id")):
            sale_id = _row_int(sale, "sale_id")
            customer_id = _row_int(sale, "customer_id")
            yield _build_phppos_sales_envelope(
                sale,
                lines_by_sale.get(sale_id, []),
                items_by_id,
                source_system_key,
                categories,
                customers_by_person_id.get(customer_id),
                extract_bike_plate,
                people_by_id.get(customer_id),
            )


def _build_phppos_sales_envelope(
    sale: DumpRow,
    line_rows: list[DumpRow],
    items_by_id: Mapping[int, DumpRow],
    source_system_key: str,
    categories: Mapping[int, str] | None = None,
    customer_row: DumpRow | None = None,
    extract_bike_plate: bool = False,
    people_row: DumpRow | None = None,
) -> dict[str, JsonValue]:
    source_order_id = str(sale.sale_id)
    resolved_categories: Mapping[int, str] = categories if categories is not None else {}
    # Per-sale customer fields applied to every line. NRIC feeds the matching
    # heuristic's anti-match (Task 6); ``lta_tag`` carries the bike plate for
    # SpeedZone only.
    line_nric = phppos_customer_nric(customer_row)
    line_lta_tag = phppos_customer_bike_plate(customer_row) if extract_bike_plate else None
    # Sale-level customer contact channels — the vehicle matching heuristic
    # (Task 6) needs the customer's email/phone to find the active Person that
    # shares the Vehicle identity. ``phppos_people`` carries them (the customer
    # row in ``phppos_customers`` only carries loyalty / custom-field data).
    customer_emails: list[str] = []
    customer_phones: list[str] = []
    if people_row is not None:
        email = people_row.get("email")
        if isinstance(email, str) and email.strip():
            customer_emails.append(email.strip())
        phone = people_row.get("phone_number")
        if isinstance(phone, str) and phone.strip():
            customer_phones.append(phone.strip())
    line_items: list[JsonValue] = []
    for line in line_rows:
        item = items_by_id.get(_row_int(line, "item_id"))
        product = (
            _phppos_product_payload(item, source_system_key, resolved_categories) if item else None
        )
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
                "metadata": {
                    "serialnumber": line.serialnumber,
                    # Customer-level fields (same for every line of this sale).
                    "nric": line_nric,
                    "lta_tag": line_lta_tag,
                },
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
                "loyalty": {
                    "points_used": _int_or_none(sale.get("points_used")),
                    "points_gained": _int_or_none(sale.get("points_gained")),
                    "did_redeem_discount": _int_or_none(sale.get("did_redeem_discount")),
                    "is_purchase_points": _int_or_none(sale.get("is_purchase_points")),
                },
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
            # Sale-level customer contact for the vehicle matching heuristic
            # (Task 6). ``customer_nric`` mirrors the per-line ``metadata.nric``
            # at the sale level so the heuristic can read it in one place.
            "customer_nric": line_nric,
            "customer_emails": cast(list[JsonValue], customer_emails),
            "customer_phones": cast(list[JsonValue], customer_phones),
        },
    )


def _phppos_product_payload(
    item: DumpRow, source_system_key: str, categories: Mapping[int, str]
) -> dict[str, JsonValue]:
    raw_category = item.get("category")
    # ``phppos_items.category`` is an int FK into ``phppos_categories``; resolve
    # to the name so ``vehicle_extraction`` can classify the line.
    category = phppos_resolve_category_name(raw_category, categories)
    return {
        "source_product_id": f"{source_system_key}-item-{item.item_id}",
        "sku": item.get("item_number"),
        "name": item.get("name"),
        "display_name": item.get("name"),
        "category": category,
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
            "points": customer.points,
            "disable_loyalty": customer.disable_loyalty,
            "current_spend_for_points": customer.current_spend_for_points,
            "current_sales_for_discount": customer.current_sales_for_discount,
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
            "points": customer.points,
            "disable_loyalty": customer.disable_loyalty,
            "current_spend_for_points": customer.current_spend_for_points,
            "current_sales_for_discount": customer.current_sales_for_discount,
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


def _int_or_none(value: JsonValue) -> int | None:
    """Coerce a dump column to int, preserving None (for nullable loyalty fields)."""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        try:
            return int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
