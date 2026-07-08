"""Tests for direct dump-backed source connectors."""

from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch, mark, skip
from src.connectors.dumps.connectors import (
    FundboxSalesDumpConnector,
    PHPPOS_SALES_TABLES,
    _build_fundbox_contact,
    _build_fundbox_legacy,
    _fetch_phppos_dump_sales,
    get_dump_connector,
)
from src.connectors.dumps.reader import DumpRow
from src.connectors.onediver.connector import ONEDIVER_SALES_TABLES
from src.connectors.sggov.bankruptcy import SGGovernmentBankruptcyConnector
from src.connectors.sggov.rental_flats import SGGovernmentRentalFlatsConnector


def _sample_extraction(texts: list[str]) -> list[dict[str, object]]:
    return [
        {
            "persons": [{"name": "Ada Lovelace"}],
            "transactions": [],
            "summary": text,
            "confidence": 0.9,
        }
        for text in texts
    ]


def test_whatsapp_dump_connector_yields_conversation_envelope(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    dump_path = tmp_path / "whatsapp.sql"
    dump_path.write_text(
        """
COPY public.orgs (id, name) FROM stdin;
org-1	EkoLife SG
\\.
COPY public.sessions (id, org_id, status, whatsapp_user_id) FROM stdin;
session-1	org-1	ready	6500000000@c.us
\\.
COPY public.chats (id, name, whatsapp_user_id) FROM stdin;
6599990000@c.us	Ada Chat	6500000000@c.us
\\.
COPY public.messages (id, chat_id, from_id, to_id, author_id, body, timestamp, from_me) FROM stdin;
msg-2	6599990000@c.us	6599990000@c.us	6500000000@c.us	\\N	Second message	2026-05-06 10:05:00	f
msg-1	6599990000@c.us	6599990000@c.us	6500000000@c.us	\\N	Hi, I am Ada	2026-05-06 10:00:00	f
\\.
COPY public.contacts (jid, phone_number, name) FROM stdin;
6599990000@c.us	+6599990000	Ada Customer
\\.
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.connectors.dumps.connectors.run_extraction_batch", _sample_extraction)
    monkeypatch.setattr("src.connectors.dumps.connectors.chat_batch_size", lambda: 20)
    monkeypatch.setattr("src.connectors.dumps.connectors.chat_batch_max_chars", lambda: 1_000_000)
    monkeypatch.setattr(
        "src.connectors.whatsapp.connector.extraction_method_label", lambda: "llm:test"
    )

    connector = get_dump_connector("whatsapp_chat", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    assert records[0]["source_record_id"] == "whatsapp-chat-6599990000@c.us-person-1"
    assert records[0]["record_type"] == "conversation"
    assert records[0]["observed_at"] == "2026-05-06T10:05:00Z"
    assert records[0]["attributes"] == {"full_name": "Ada Lovelace"}
    assert records[0]["identifiers"] == []
    assert records[0]["raw_payload"]["messages_text"].splitlines() == [
        "[2026-05-06 10:00:00] Ada Chat (+6599990000): Hi, I am Ada",
        "[2026-05-06 10:05:00] Ada Chat (+6599990000): Second message",
    ]


def test_bitrix_dump_connector_yields_conversation_envelope(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    dump_path = tmp_path / "bitrix.sql"
    dump_path.write_text(
        """
CREATE TABLE `categories` (
  `id` int,
  `name` varchar(255)
);
CREATE TABLE `deals` (
  `id` int,
  `bitrix_deal_id` varchar(255),
  `title` varchar(255),
  `stage_id` varchar(255),
  `opened` int,
  `closed` int,
  `category_id` int
);
CREATE TABLE `chats` (
  `id` int,
  `deal_id` int,
  `bitrix_chat_id` varchar(255),
  `last_message_at` datetime,
  `created_at` datetime
);
CREATE TABLE `personalize_message_logs` (
  `id` int,
  `chat_id` int,
  `client_name` varchar(255),
  `message_sent` text,
  `llm_message` text,
  `created_at` datetime
);
CREATE TABLE `sent_message_logs` (
  `id` int,
  `chat_id` int,
  `template_id` int,
  `created_at` datetime
);
CREATE TABLE `templates` (
  `id` int,
  `content` text
);
CREATE TABLE `agents` (
  `id` int,
  `bitrix_agent_id` varchar(255),
  `name` varchar(255),
  `active` int
);
CREATE TABLE `agent_chat` (
  `chat_id` int,
  `agent_id` int
);
INSERT INTO `categories` VALUES (1,'EkoSG');
INSERT INTO `deals` VALUES (10,'B10','Deal for Ada','NEW',1,0,1);
INSERT INTO `chats` VALUES (5,10,'chat-5','2026-05-06 10:00:00','2026-05-06 09:00:00');
INSERT INTO `personalize_message_logs` VALUES
(100,5,'Ada Lovelace','Hello Ada','LLM fallback','2026-05-06 09:50:00');
INSERT INTO `sent_message_logs` VALUES (200,5,300,'2026-05-06 09:45:00');
INSERT INTO `templates` VALUES (300,'Template body');
INSERT INTO `agents` VALUES (400,'agent-1','Agent Smith',1);
INSERT INTO `agent_chat` VALUES (5,400);
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.connectors.dumps.connectors.run_extraction_batch", _sample_extraction)
    monkeypatch.setattr("src.connectors.dumps.connectors.chat_batch_size", lambda: 20)
    monkeypatch.setattr("src.connectors.dumps.connectors.chat_batch_max_chars", lambda: 1_000_000)
    monkeypatch.setattr(
        "src.connectors.bitrix.connector.extraction_method_label", lambda: "llm:test"
    )

    connector = get_dump_connector("bitrix_chat", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    assert records[0]["source_record_id"] == "bitrix-chat-5-person-1"
    assert records[0]["record_type"] == "conversation"
    assert records[0]["attributes"] == {"full_name": "Ada Lovelace", "deal_title": "Deal for Ada"}
    assert records[0]["conversation_ref"] == {
        "platform": "bitrix",
        "chat_id": "5",
        "deal_id": "10",
        "bitrix_chat_id": "chat-5",
        "tenant": "eko",
    }
    assert records[0]["raw_payload"]["conversation_text"].splitlines() == [
        "[Deal] Deal for Ada",
        "[2026-05-06 09:45:00] Template: Template body",
        "[2026-05-06 09:50:00] Client: Ada Lovelace",
        "[2026-05-06 09:50:00] Sent: Hello Ada",
    ]


def test_eko_dump_connector_yields_identity_envelope(tmp_path: Path) -> None:
    dump_path = tmp_path / "eko.sql"
    dump_path.write_text(
        """
CREATE TABLE `phppos_people` (
  `person_id` int NOT NULL,
  `first_name` varchar(255),
  `last_name` varchar(255),
  `full_name` varchar(255),
  `phone_number` varchar(255),
  `email` varchar(255),
  `address_1` varchar(255),
  `address_2` varchar(255),
  `city` varchar(255),
  `state` varchar(255),
  `zip` varchar(255),
  `country` varchar(255),
  `comments` text,
  `create_date` datetime,
  `last_modified` datetime,
  `title` varchar(255),
  `phone_code` varchar(255)
);
CREATE TABLE `phppos_customers` (
  `id` int NOT NULL,
  `person_id` int,
  `deleted` int,
  `account_number` varchar(255),
  `company_name` varchar(255),
  `custom_field_1_value` varchar(255),
  `custom_field_2_value` varchar(255),
  `custom_field_3_value` varchar(255),
  `custom_field_4_value` varchar(255),
  `custom_field_5_value` varchar(255),
  `custom_field_6_value` varchar(255),
  `custom_field_7_value` varchar(255),
  `custom_field_8_value` varchar(255),
  `custom_field_9_value` varchar(255),
  `custom_field_10_value` varchar(255)
);
INSERT INTO `phppos_people` VALUES
(7,'Ada','Lovelace','Ada Lovelace','6599990000','ada@example.test','One','Two',
'Singapore','SG','123456','SG','notes','2026-05-01 01:00:00','2026-05-06 02:00:00',
'Ms','65');
INSERT INTO `phppos_customers` VALUES
(11,7,0,'ACC-11','Ada Co','S1234567A','unused-2','unused-3','2026-12-31','15',
'unused-6','unused-7','East','1990-01-31','Y');
""".strip(),
        encoding="utf-8",
    )

    connector = get_dump_connector("eko_phppos", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    # Keyed on person_id (7), not customers.id (11) — see EkoConnector._build_one.
    assert records[0]["source_record_id"] == "eko_phppos-customer-7"
    assert records[0]["record_type"] == "identity"
    assert records[0]["attributes"] == {
        "full_name": "Ada Lovelace",
        "address": "One, Two, Singapore, SG, 123456, SG",
        "dob": "1990-01-31",
    }
    identifiers = {item["type"]: item["value"] for item in records[0]["identifiers"]}
    phone_values = {item["value"] for item in records[0]["identifiers"] if item["type"] == "phone"}
    identifier_types = {item["type"] for item in records[0]["identifiers"]}
    raw_person = records[0]["raw_payload"]["person"]
    assert identifiers["nric"] == "S1234567A"
    assert identifiers["email"] == "ada@example.test"
    assert phone_values == {"6599990000"}
    assert "external:bitrix" not in identifier_types
    assert "external_customer_id" not in identifier_types
    assert raw_person["custom_field_2_value"] == "unused-2"
    assert raw_person["custom_field_4_value"] == "2026-12-31"
    assert raw_person["custom_field_5_value"] == "15"
    assert raw_person["custom_field_8_value"] == "East"
    assert raw_person["custom_field_10_value"] == "Y"


def test_eko_dump_connector_derives_phone_region_hint(tmp_path: Path) -> None:
    dump_path = tmp_path / "eko_my.sql"
    dump_path.write_text(
        """
CREATE TABLE `phppos_people` (
  `person_id` int NOT NULL,
  `first_name` varchar(255),
  `last_name` varchar(255),
  `full_name` varchar(255),
  `phone_number` varchar(255),
  `email` varchar(255),
  `address_1` varchar(255),
  `address_2` varchar(255),
  `city` varchar(255),
  `state` varchar(255),
  `zip` varchar(255),
  `country` varchar(255),
  `comments` text,
  `create_date` datetime,
  `last_modified` datetime,
  `title` varchar(255),
  `phone_code` varchar(255)
);
CREATE TABLE `phppos_customers` (
  `id` int NOT NULL,
  `person_id` int,
  `deleted` int,
  `account_number` varchar(255),
  `company_name` varchar(255),
  `custom_field_1_value` varchar(255),
  `custom_field_2_value` varchar(255),
  `custom_field_3_value` varchar(255),
  `custom_field_4_value` varchar(255),
  `custom_field_5_value` varchar(255),
  `custom_field_6_value` varchar(255),
  `custom_field_7_value` varchar(255),
  `custom_field_8_value` varchar(255),
  `custom_field_9_value` varchar(255),
  `custom_field_10_value` varchar(255)
);
INSERT INTO `phppos_people` VALUES
(9,'Wei','Tan','Wei Tan','96542555','wei@example.test','One','Two',
'Kuala Lumpur','KL','50000','Malaysia','notes','2026-05-01 01:00:00','2026-05-06 02:00:00',
'Mr','60');
INSERT INTO `phppos_customers` VALUES
(13,9,0,'ACC-13','Wei Co','S1234568A','unused-2','unused-3','2026-12-31','15',
'unused-6','unused-7','KL','1991-02-02','Y');
""".strip(),
        encoding="utf-8",
    )

    connector = get_dump_connector("eko_phppos", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    phone_items = [item for item in records[0]["identifiers"] if item["type"] == "phone"]
    assert phone_items == [
        {"type": "phone", "value": "96542555", "is_verified": False, "region_hint": "MY"}
    ]


def test_speedzone_dump_connector_preserves_custom_field_mapping(tmp_path: Path) -> None:
    dump_path = tmp_path / "speedzone.sql"
    dump_path.write_text(
        """
CREATE TABLE `phppos_people` (
  `person_id` int NOT NULL,
  `first_name` varchar(255),
  `last_name` varchar(255),
  `full_name` varchar(255),
  `phone_number` varchar(255),
  `email` varchar(255),
  `address_1` varchar(255),
  `address_2` varchar(255),
  `city` varchar(255),
  `state` varchar(255),
  `zip` varchar(255),
  `country` varchar(255),
  `comments` text,
  `create_date` datetime,
  `last_modified` datetime,
  `title` varchar(255),
  `phone_code` varchar(255)
);
CREATE TABLE `phppos_customers` (
  `id` int NOT NULL,
  `person_id` int,
  `deleted` int,
  `account_number` varchar(255),
  `company_name` varchar(255),
  `custom_field_1_value` varchar(255),
  `custom_field_2_value` varchar(255),
  `custom_field_3_value` varchar(255),
  `custom_field_4_value` varchar(255),
  `custom_field_5_value` varchar(255),
  `custom_field_6_value` varchar(255),
  `custom_field_7_value` varchar(255),
  `custom_field_8_value` varchar(255),
  `custom_field_9_value` varchar(255),
  `custom_field_10_value` varchar(255)
);
INSERT INTO `phppos_people` VALUES
(8,'Grace','Hopper','Grace Hopper','6588880000','grace@example.test','Three','Four',
'Singapore','SG','654321','SG','notes','2026-05-01 01:00:00','2026-05-06 02:00:00',
'Ms','65');
INSERT INTO `phppos_customers` VALUES
(12,8,0,'ACC-12','Grace Co','S7654321B','BITRIX-12','2026-11-30','9','unused-5',
'unused-6','Vespa Primavera','SBA1234A','1992-02-29','SBB5678B');
""".strip(),
        encoding="utf-8",
    )

    connector = get_dump_connector("speedzone_phppos", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    # Keyed on person_id (8), not customers.id (12).
    assert records[0]["source_record_id"] == "speedzone_phppos-customer-8"
    assert records[0]["attributes"] == {
        "full_name": "Grace Hopper",
        "address": "Three, Four, Singapore, SG, 654321, SG",
        "dob": "1992-02-29",
    }
    assert records[0]["addresses"] == [
        {
            "raw": "Three, Four, Singapore, SG, 654321, SG",
            "street_number": None,
            "street_name": "Three",
            "unit_number": None,
            "building_name": "Four",
            "city": "Singapore",
            "state_province": "SG",
            "postal_code": "654321",
            "country_code": "SG",
        }
    ]
    identifiers = {item["type"]: item["value"] for item in records[0]["identifiers"]}
    raw_person = records[0]["raw_payload"]["person"]
    assert identifiers["nric"] == "S7654321B"
    assert identifiers["email"] == "grace@example.test"
    assert identifiers["phone"] == "6588880000"
    assert identifiers["external:bitrix"] == "BITRIX-12"
    assert raw_person["custom_field_3_value"] == "2026-11-30"
    assert raw_person["custom_field_4_value"] == "9"
    assert raw_person["custom_field_7_value"] == "Vespa Primavera"
    assert raw_person["custom_field_8_value"] == "SBA1234A"
    assert raw_person["custom_field_9_value"] == "1992-02-29"
    assert raw_person["custom_field_10_value"] == "SBB5678B"


def test_speedzone_dump_connector_derives_phone_region_hint(tmp_path: Path) -> None:
    dump_path = tmp_path / "speedzone_my.sql"
    dump_path.write_text(
        """
CREATE TABLE `phppos_people` (
  `person_id` int NOT NULL,
  `first_name` varchar(255),
  `last_name` varchar(255),
  `full_name` varchar(255),
  `phone_number` varchar(255),
  `email` varchar(255),
  `address_1` varchar(255),
  `address_2` varchar(255),
  `city` varchar(255),
  `state` varchar(255),
  `zip` varchar(255),
  `country` varchar(255),
  `comments` text,
  `create_date` datetime,
  `last_modified` datetime,
  `title` varchar(255),
  `phone_code` varchar(255)
);
CREATE TABLE `phppos_customers` (
  `id` int NOT NULL,
  `person_id` int,
  `deleted` int,
  `account_number` varchar(255),
  `company_name` varchar(255),
  `custom_field_1_value` varchar(255),
  `custom_field_2_value` varchar(255),
  `custom_field_3_value` varchar(255),
  `custom_field_4_value` varchar(255),
  `custom_field_5_value` varchar(255),
  `custom_field_6_value` varchar(255),
  `custom_field_7_value` varchar(255),
  `custom_field_8_value` varchar(255),
  `custom_field_9_value` varchar(255),
  `custom_field_10_value` varchar(255)
);
INSERT INTO `phppos_people` VALUES
(10,'Wei','Tan','Wei Tan','96542555','wei@example.test','One','Two',
'Kuala Lumpur','KL','50000','Malaysia','notes','2026-05-01 01:00:00','2026-05-06 02:00:00',
'Mr','60');
INSERT INTO `phppos_customers` VALUES
(14,10,0,'ACC-14','Wei Co','S1234569A','unused-2','unused-3','2026-12-31','15',
'unused-6','unused-7','KL','1991-02-02','Y');
""".strip(),
        encoding="utf-8",
    )

    connector = get_dump_connector("speedzone_phppos", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    phone_items = [item for item in records[0]["identifiers"] if item["type"] == "phone"]
    assert phone_items == [
        {"type": "phone", "value": "96542555", "is_verified": False, "region_hint": "MY"}
    ]


def test_fundbox_contact_dump_is_relationship_record() -> None:
    record = _build_fundbox_contact(
        DumpRow(
            {
                "id": 9,
                "user_id": 7,
                "mobile_number": "6599990000",
                "full_name": "Next Of Kin",
                "relationship": "mother",
                "updated_at": "2026-05-06 10:00:00",
                "created_at": "2026-05-01 10:00:00",
            }
        )
    )

    assert record["source_record_id"] == "fundbox_consumer_backend-contact-9"
    assert record["record_type"] == "relationship"
    assert record["attributes"]["relationship_to_referrer"] == "mother"


def test_fundbox_legacy_dump_preserves_multiple_addresses() -> None:
    record = _build_fundbox_legacy(
        DumpRow(
            {
                "id": 7,
                "nric": "S1234567A",
                "email": "ada@example.test",
                "mobile_number": "6599990000",
                "whatsapp_phone": None,
                "facebook_id": None,
                "updated_at": "2026-05-06 10:00:00",
                "created_at": "2026-05-01 10:00:00",
                "full_name": "Ada Lovelace",
                "date_of_birth": "1992-02-29",
                "gender": "F",
                "nationality": "SG",
            }
        ),
        [
            DumpRow(
                {
                    "address_line_1": "10 Orchard Road",
                    "address_line_2": "Lucky Plaza",
                    "street": "Orchard Road",
                    "building": "Lucky Plaza",
                    "block": "10",
                    "floor": "05",
                    "unit": "123",
                    "city": "Singapore",
                    "state": None,
                    "postal_code": "238863",
                    "country": "SG",
                }
            ),
            DumpRow(
                {
                    "address_line_1": "20 Second Street",
                    "address_line_2": None,
                    "street": "Second Street",
                    "building": None,
                    "block": "20",
                    "floor": "07",
                    "unit": "456",
                    "city": "Singapore",
                    "state": None,
                    "postal_code": "654321",
                    "country": "SG",
                }
            ),
        ],
    )

    assert len(record["addresses"]) == 2
    assert record["addresses"][0]["postal_code"] == "238863"
    assert record["addresses"][0]["unit_number"] == "#05-123"
    assert record["addresses"][1]["postal_code"] == "654321"
    assert record["addresses"][1]["unit_number"] == "#07-456"
    assert record["attributes"]["address"].startswith("10 Orchard Road")


def test_fundbox_sales_dump_resolves_product_from_product_variant_id(tmp_path: Path) -> None:
    dump_path = tmp_path / "fundbox.sql"
    dump_path.write_text(
        "\n".join(
            [
                (
                    "INSERT INTO `orders` "
                    "(`id`,`order_no`,`user_id`,`merchant_id`,`status`,"
                    "`created_at`,`updated_at`,`deleted_at`) "
                    "VALUES (10,'INV-10',123,1,'completed',"
                    "'2026-05-01 00:00:00','2026-05-01 00:00:00',NULL);"
                ),
                (
                    "INSERT INTO `order_items` "
                    "(`id`,`order_id`,`merchant_product_id`,`quantity`,`price`,"
                    "`lta_tag`,`serial_no`,`created_at`,`updated_at`) "
                    "VALUES (77,10,501,1,1599.00,'X891','SN-891',"
                    "'2026-05-01 00:00:00','2026-05-01 00:00:00');"
                ),
                (
                    "INSERT INTO `merchant_products` "
                    "(`id`,`merchant_id`,`product_variant_id`,`price`,`created_at`,"
                    "`updated_at`) "
                    "VALUES (501,1,701,1599.00,'2026-05-01 00:00:00',"
                    "'2026-05-01 00:00:00');"
                ),
                (
                    "INSERT INTO `product_variants` "
                    "(`id`,`product_id`,`sku`,`name`,`image`,`price`,`attributes`,"
                    "`active`,`visible`,`deleted_at`,`created_at`,`updated_at`) "
                    "VALUES (701,801,'SKU-701','Variant Bike','',1599.00,'{}',"
                    "1,1,NULL,'2026-05-01 00:00:00','2026-05-01 00:00:00');"
                ),
                (
                    "INSERT INTO `products` "
                    "(`id`,`product_id`,`name`,`image`,`type`,`sub_type`,`category`,"
                    "`sub_category`,`description`,`make`,`model`,`has_serial_number`,"
                    "`has_lta_tag`,`active`,`visible`,`deleted_at`,`created_at`,"
                    "`updated_at`) "
                    "VALUES (801,'P-801','Parent Bike','','Micro Mobility',NULL,"
                    "'Bicycles',NULL,'','Brand','Model X',1,1,1,1,NULL,"
                    "'2026-05-01 00:00:00','2026-05-01 00:00:00');"
                ),
            ]
        ),
        encoding="utf-8",
    )

    records = list(FundboxSalesDumpConnector(dump_path).fetch_records())

    assert len(records) == 1
    raw_payload = records[0]["raw_payload"]
    assert isinstance(raw_payload, dict)
    line_items = raw_payload["line_items"]
    assert isinstance(line_items, list)
    line = line_items[0]
    assert isinstance(line, dict)
    product = line["product"]
    assert isinstance(product, dict)
    assert product["display_name"] == "Parent Bike"
    assert product["name"] == "Variant Bike"
    assert product["attributes"] == {
        "variant_attributes": "{}",
        "type": "Micro Mobility",
        "sub_type": None,
        "model": "Model X",
    }


def test_phppos_sales_dump_puts_serialnumber_in_metadata(tmp_path: Path) -> None:
    dump_path = tmp_path / "phppos.sql"
    dump_path.write_text(
        "\n".join(
            [
                (
                    "INSERT INTO `phppos_sales` (`sale_id`,`customer_id`,`sale_time`,"
                    "`invoice_date`,`invoice_number`,`sale_status`,`suspended`) "
                    "VALUES (1,55,'2026-05-01 00:00:00','2026-05-01','INV-1',"
                    "'0','0');"
                ),
                (
                    "INSERT INTO `phppos_sales_items` (`sale_id`,`item_id`,`line`,"
                    "`quantity_purchased`,`item_unit_price`,`discount_percent`,"
                    "`serialnumber`) VALUES (1,22,0,1.0,899.0,0.0,'SER-22');"
                ),
                (
                    "INSERT INTO `phppos_items` (`item_id`,`item_number`,`name`,"
                    "`category`,`subcategory`,`size`,`cost_price`,`unit_price`,"
                    "`description`) VALUES (22,'SKU-22','Scooter Model','Scooters',"
                    "'Electric','Large',500.0,899.0,'');"
                ),
            ]
        ),
        encoding="utf-8",
    )

    records = list(_fetch_phppos_dump_sales(dump_path, "eko_phppos"))

    assert len(records) == 1
    raw_payload = records[0]["raw_payload"]
    assert isinstance(raw_payload, dict)
    line_items = raw_payload["line_items"]
    assert isinstance(line_items, list)
    line = line_items[0]
    assert isinstance(line, dict)
    metadata = line["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["serialnumber"] == "SER-22"


def test_fundbox_dump_keeps_device_ids_out_of_identifiers() -> None:
    dump_path = Path(".dumps") / "limited-100" / "fundbox_2026-05-06.sql"
    if not dump_path.exists():
        skip(f"local dump fixture missing: {dump_path}")
    connector = get_dump_connector("fundbox_consumer_backend", dump_path)
    record = next(connector.fetch_records())

    identifier_types = {item["type"] for item in record["identifiers"]}
    assert "device_id" not in identifier_types
    assert record["raw_payload"]["device_ids"]


# Each (source_key, dump_file, expected_record_type) is a separate test item, so a
# missing local fixture skips only that case — connectors whose dump *is* present
# still run and surface regressions (a whole-test skip would mask them).
@mark.parametrize(
    ("source_key", "dump_file", "expected_record_type"),
    [
        ("fundbox_consumer_backend", "fundbox_2026-05-06.sql", "identity"),
        ("fundbox_consumer_backend:contacts", "fundbox_2026-05-06.sql", "relationship"),
        ("fundbox_consumer_backend:legacy", "fundbox_2026-05-06.sql", "identity"),
        ("fundbox_consumer_backend:merged", "fundbox_2026-05-06.sql", "identity"),
        ("fundbox_consumer_backend:sales", "fundbox_2026-05-06.sql", "sales"),
        ("eko_phppos", "eko_phppos_2026-05-06.sql", "identity"),
        ("eko_phppos:sales", "eko_phppos_2026-05-06.sql", "sales"),
        ("speedzone_phppos", "speedzone_phppos_2026-05-06.sql", "identity"),
        ("speedzone_phppos:sales", "speedzone_phppos_2026-05-06.sql", "sales"),
    ],
)
def test_real_non_chat_dumps_yield_first_records(
    source_key: str, dump_file: str, expected_record_type: str
) -> None:
    dump_path = Path(".dumps") / dump_file
    if not dump_path.exists():
        skip(f"local dump fixture missing for {source_key}: {dump_path}")
    connector = get_dump_connector(source_key, dump_path)
    record = next(connector.fetch_records(), None)
    assert record is not None, source_key
    assert record["source_record_id"]
    assert record["record_type"] == expected_record_type, source_key


def test_sggov_sources_are_registered_for_dump_mode(tmp_path: Path) -> None:
    bankruptcy_dump = tmp_path / "bankruptcy.sql"
    bankruptcy_dump.write_text("", encoding="utf-8")
    rental_dump = tmp_path / "rental.sql"
    rental_dump.write_text("", encoding="utf-8")

    bankruptcy = get_dump_connector("sgbankruptcy", bankruptcy_dump)
    rental_flats = get_dump_connector("sgrentalflats", rental_dump)

    assert isinstance(bankruptcy, SGGovernmentBankruptcyConnector)
    assert isinstance(rental_flats, SGGovernmentRentalFlatsConnector)


def test_phppos_sales_tables_include_categories_and_customers() -> None:
    # The vehicle-extraction + NRIC anti-match pipeline reads category names
    # (phppos_categories) and the customer's NRIC / bike plate (phppos_customers).
    assert "phppos_categories" in PHPPOS_SALES_TABLES
    assert "phppos_customers" in PHPPOS_SALES_TABLES


def test_onediver_sales_tables_include_items_and_products() -> None:
    # OneDiver Order ``non_vehicle_lines`` enrichment reads line items + the
    # product catalogue.
    assert "sales_order_items" in ONEDIVER_SALES_TABLES
    assert "products" in ONEDIVER_SALES_TABLES


def _write_phppos_sales_dump_with_customer(dump_path: Path, source_system_key: str) -> None:
    """Write a phppos sales dump with a customer + category for field-emission tests."""
    dump_path.write_text(
        "\n".join(
            [
                (
                    "INSERT INTO `phppos_sales` (`sale_id`,`customer_id`,`sale_time`,"
                    "`invoice_date`,`invoice_number`,`sale_status`,`suspended`) "
                    "VALUES (1,55,'2026-05-01 00:00:00','2026-05-01','INV-1','0','0');"
                ),
                (
                    "INSERT INTO `phppos_sales_items` (`sale_id`,`item_id`,`line`,"
                    "`quantity_purchased`,`item_unit_price`,`discount_percent`,"
                    "`serialnumber`) VALUES (1,22,0,1.0,899.0,0.0,'SER-22');"
                ),
                (
                    "INSERT INTO `phppos_items` (`item_id`,`item_number`,`name`,"
                    "`category`,`subcategory`,`size`,`cost_price`,`unit_price`,"
                    "`description`) VALUES (22,'SKU-22','Scooter Model',7,"
                    "'Electric','Large',500.0,899.0,'');"
                ),
                (
                    "INSERT INTO `phppos_categories` (`id`,`name`) VALUES (7,'Scooters');"
                ),
                (
                    "INSERT INTO `phppos_customers` (`id`,`person_id`,`deleted`,"
                    "`custom_field_1_value`,`custom_field_8_value`,"
                    "`custom_field_10_value`) VALUES (10,55,0,'S9876543Z','SGX1234J',NULL);"
                ),
            ]
        ),
        encoding="utf-8",
    )


def test_eko_sales_dump_emits_nric_and_resolves_category(tmp_path: Path) -> None:
    dump_path = tmp_path / "eko.sql"
    _write_phppos_sales_dump_with_customer(dump_path, "eko_phppos")

    records = list(_fetch_phppos_dump_sales(dump_path, "eko_phppos"))
    assert len(records) == 1
    line = records[0]["raw_payload"]["line_items"][0]
    assert isinstance(line, dict)
    metadata = line["metadata"]
    assert isinstance(metadata, dict)
    # NRIC is emitted for all phppos sources (eko + speedzone).
    assert metadata["nric"] == "S9876543Z"
    # Eko is not SpeedZone — no bike plate under lta_tag.
    assert metadata["lta_tag"] is None
    # Existing serialnumber emission is preserved.
    assert metadata["serialnumber"] == "SER-22"
    product = line["product"]
    assert isinstance(product, dict)
    # Category FK (7) resolved to the name from phppos_categories.
    assert product["category"] == "Scooters"


def test_speedzone_sales_dump_emits_bike_plate_lta_tag_and_nric(tmp_path: Path) -> None:
    dump_path = tmp_path / "speedzone.sql"
    _write_phppos_sales_dump_with_customer(dump_path, "speedzone_phppos")

    records = list(_fetch_phppos_dump_sales(dump_path, "speedzone_phppos"))
    assert len(records) == 1
    line = records[0]["raw_payload"]["line_items"][0]
    assert isinstance(line, dict)
    metadata = line["metadata"]
    assert isinstance(metadata, dict)
    # Bike plate (custom_field_8_value) emitted as lta_tag for SpeedZone only.
    assert metadata["lta_tag"] == "SGX1234J"
    # NRIC emitted for all phppos sources.
    assert metadata["nric"] == "S9876543Z"
    product = line["product"]
    assert isinstance(product, dict)
    assert product["category"] == "Scooters"


def test_fundbox_sales_dump_emits_merchant_and_product_flags(tmp_path: Path) -> None:
    dump_path = tmp_path / "fundbox.sql"
    dump_path.write_text(
        "\n".join(
            [
                (
                    "INSERT INTO `orders` "
                    "(`id`,`order_no`,`user_id`,`merchant_id`,`status`,"
                    "`created_at`,`updated_at`,`deleted_at`) "
                    "VALUES (10,'INV-10',123,1,'completed','2026-05-01 00:00:00',"
                    "'2026-05-01 00:00:00',0);"
                ),
                (
                    "INSERT INTO `order_items` "
                    "(`id`,`order_id`,`merchant_product_id`,`quantity`,`price`,"
                    "`lta_tag`,`serial_no`,`created_at`,`updated_at`) "
                    "VALUES (77,10,501,1,1599.00,'X891','SN-891',"
                    "'2026-05-01 00:00:00','2026-05-01 00:00:00');"
                ),
                (
                    "INSERT INTO `merchant_products` "
                    "(`id`,`merchant_id`,`product_variant_id`,`price`,`created_at`,"
                    "`updated_at`) "
                    "VALUES (501,1,701,1599.00,'2026-05-01 00:00:00',"
                    "'2026-05-01 00:00:00');"
                ),
                (
                    "INSERT INTO `product_variants` "
                    "(`id`,`product_id`,`sku`,`name`,`active`,`attributes`) "
                    "VALUES (701,801,'SKU-701','Variant Bike',1,'{}');"
                ),
                (
                    "INSERT INTO `merchants` (`id`,`name`,`official_name`) "
                    "VALUES (1,'Acme Bikes','Acme Bikes Pte Ltd');"
                ),
                (
                    "INSERT INTO `products` "
                    "(`id`,`product_id`,`name`,`image`,`type`,`sub_type`,`category`,"
                    "`sub_category`,`description`,`make`,`model`,`has_serial_number`,"
                    "`has_lta_tag`,`active`,`visible`,`deleted_at`,`created_at`,"
                    "`updated_at`) "
                    "VALUES (801,'P-801','Parent Bike','','Micro Mobility',NULL,"
                    "'Bicycles',NULL,'','Brand','Model X',1,1,1,1,NULL,"
                    "'2026-05-01 00:00:00','2026-05-01 00:00:00');"
                ),
            ]
        ),
        encoding="utf-8",
    )

    records = list(FundboxSalesDumpConnector(dump_path).fetch_records())
    assert len(records) == 1
    line = records[0]["raw_payload"]["line_items"][0]
    assert isinstance(line, dict)
    metadata = line["metadata"]
    assert isinstance(metadata, dict)
    # Merchant name resolved from the order's merchant_id.
    assert metadata["merchant"] == "Acme Bikes"
    product = line["product"]
    assert isinstance(product, dict)
    assert product["category"] == "Bicycles"
    assert product["manufacturer"] == "Brand"
    # Top-level model (vehicle_extraction reads product.model).
    assert product["model"] == "Model X"
    # Secondary product flags surfaced for future use.
    assert product["has_serial_number"] is True
    assert product["has_lta_tag"] is True


def test_fundbox_sales_dump_emits_customer_contact_from_users_and_basic_profiles(
    tmp_path: Path,
) -> None:
    """The fundbox sales dump must carry sale-level customer_emails /
    customer_phones / customer_nric joined from ``users`` + ``basic_profiles``
    by ``orders.user_id`` — the Vehicle heuristic reads these from
    ``raw_payload`` to find candidates (Task 6 fix)."""
    dump_path = tmp_path / "fundbox.sql"
    dump_path.write_text(
        "\n".join(
            [
                (
                    "INSERT INTO `orders` "
                    "(`id`,`order_no`,`user_id`,`merchant_id`,`status`,"
                    "`created_at`,`updated_at`,`deleted_at`) "
                    "VALUES (10,'INV-10',123,1,'completed',"
                    "'2026-05-01 00:00:00','2026-05-01 00:00:00',NULL);"
                ),
                (
                    "INSERT INTO `users` "
                    "(`id`,`email`,`mobile_number`,`created_at`,`updated_at`) "
                    "VALUES (123,'jane@fundbox.sg','+6591112222',"
                    "'2026-05-01 00:00:00','2026-05-01 00:00:00');"
                ),
                (
                    "INSERT INTO `basic_profiles` "
                    "(`id`,`user_id`,`nric`,`email`,`mobile_number`) "
                    "VALUES (1,123,'S9876543A','jane@fundbox.sg','+6591112222');"
                ),
            ]
        ),
        encoding="utf-8",
    )

    records = list(FundboxSalesDumpConnector(dump_path).fetch_records())
    assert len(records) == 1
    raw_payload = records[0]["raw_payload"]
    assert isinstance(raw_payload, dict)
    # customer_emails / customer_phones deduped across users + basic_profiles.
    assert raw_payload["customer_emails"] == ["jane@fundbox.sg"]
    assert raw_payload["customer_phones"] == ["+6591112222"]
    assert raw_payload["customer_nric"] == "S9876543A"


def test_fundbox_sales_dump_emits_empty_contact_when_user_missing(
    tmp_path: Path,
) -> None:
    """An order whose user_id has no users/basic_profiles row still emits a
    valid envelope with empty contact channels (graceful degradation)."""
    dump_path = tmp_path / "fundbox.sql"
    dump_path.write_text(
        "\n".join(
            [
                (
                    "INSERT INTO `orders` "
                    "(`id`,`order_no`,`user_id`,`merchant_id`,`status`,"
                    "`created_at`,`updated_at`,`deleted_at`) "
                    "VALUES (11,'INV-11',999,1,'completed',"
                    "'2026-05-01 00:00:00','2026-05-01 00:00:00',NULL);"
                ),
            ]
        ),
        encoding="utf-8",
    )

    records = list(FundboxSalesDumpConnector(dump_path).fetch_records())
    assert len(records) == 1
    raw_payload = records[0]["raw_payload"]
    assert isinstance(raw_payload, dict)
    assert raw_payload["customer_emails"] == []
    assert raw_payload["customer_phones"] == []
    assert raw_payload["customer_nric"] is None
