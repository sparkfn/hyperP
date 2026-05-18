"""Tests for direct dump-backed source connectors."""

from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch
from src.connectors.dumps.connectors import get_dump_connector
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
msg-1	6599990000@c.us	6599990000@c.us	6500000000@c.us	\\N	Hi, I am Ada	2026-05-06 10:00:00	f
\\.
COPY public.contacts (jid, phone_number, name) FROM stdin;
6599990000@c.us	+6599990000	Ada Customer
\\.
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.connectors.dumps.connectors.run_extraction_batch", _sample_extraction)
    monkeypatch.setattr(
        "src.connectors.whatsapp.connector.extraction_method_label", lambda: "llm:test"
    )

    connector = get_dump_connector("whatsapp_chat", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    assert records[0]["source_record_id"] == "whatsapp-chat-6599990000@c.us-person-1"
    assert records[0]["record_type"] == "conversation"
    assert records[0]["observed_at"] == "2026-05-06T10:00:00Z"
    assert records[0]["attributes"] == {"full_name": "Ada Lovelace"}
    assert records[0]["identifiers"] == []


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
(100,5,'Ada Lovelace','Hello Ada','LLM fallback','2026-05-06 09:30:00');
INSERT INTO `sent_message_logs` VALUES (200,5,300,'2026-05-06 09:45:00');
INSERT INTO `templates` VALUES (300,'Template body');
INSERT INTO `agents` VALUES (400,'agent-1','Agent Smith',1);
INSERT INTO `agent_chat` VALUES (5,400);
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.connectors.dumps.connectors.run_extraction_batch", _sample_extraction)
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


def test_eko_dump_connector_yields_system_envelope(tmp_path: Path) -> None:
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
  `custom_field_4_value` varchar(255),
  `custom_field_5_value` varchar(255),
  `custom_field_9_value` varchar(255)
);
INSERT INTO `phppos_people` VALUES
(7,'Ada','Lovelace','Ada Lovelace','6599990000','ada@example.test','One','Two',
'Singapore','SG','123456','SG','notes','2026-05-01 01:00:00','2026-05-06 02:00:00',
'Ms','65');
INSERT INTO `phppos_customers` VALUES
(11,7,0,'ACC-11','Ada Co','S1234567A','0','EXT-11','-536457600');
""".strip(),
        encoding="utf-8",
    )

    connector = get_dump_connector("eko_phppos", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    assert records[0]["source_record_id"] == "eko_phppos-customer-11"
    assert records[0]["attributes"] == {
        "full_name": "Ada Lovelace",
        "address": "One, Two, Singapore, SG, 123456, SG",
    }
    identifiers = {item["type"]: item["value"] for item in records[0]["identifiers"]}
    assert identifiers["nric"] == "S1234567A"
    assert identifiers["email"] == "ada@example.test"
    assert identifiers["phone"] == "6599990000"
    assert identifiers["external_customer_id"] == "EXT-11"


def test_fundbox_dump_keeps_device_ids_out_of_identifiers() -> None:
    connector = get_dump_connector(
        "fundbox_consumer_backend",
        Path(".dumps") / "limited-100" / "fundbox_2026-05-06.sql",
    )
    record = next(connector.fetch_records())

    identifier_types = {item["type"] for item in record["identifiers"]}
    assert "device_id" not in identifier_types
    assert record["raw_payload"]["device_ids"]


def test_real_non_chat_dumps_yield_first_records() -> None:
    cases = [
        ("fundbox_consumer_backend", "fundbox_2026-05-06.sql"),
        ("fundbox_consumer_backend:contacts", "fundbox_2026-05-06.sql"),
        ("fundbox_consumer_backend:legacy", "fundbox_2026-05-06.sql"),
        ("fundbox_consumer_backend:merged", "fundbox_2026-05-06.sql"),
        ("fundbox_consumer_backend:sales", "fundbox_2026-05-06.sql"),
        ("eko_phppos", "eko_phppos_2026-05-06.sql"),
        ("eko_phppos:sales", "eko_phppos_2026-05-06.sql"),
        ("speedzone_phppos", "speedzone_phppos_2026-05-06.sql"),
        ("speedzone_phppos:sales", "speedzone_phppos_2026-05-06.sql"),
    ]
    for source_key, dump_file in cases:
        connector = get_dump_connector(source_key, Path(".dumps") / dump_file)
        record = next(connector.fetch_records(), None)
        assert record is not None, source_key
        assert record["source_record_id"]


def test_sggov_sources_are_registered_for_dump_mode(tmp_path: Path) -> None:
    bankruptcy_dump = tmp_path / "bankruptcy.sql"
    bankruptcy_dump.write_text("", encoding="utf-8")
    rental_dump = tmp_path / "rental.sql"
    rental_dump.write_text("", encoding="utf-8")

    bankruptcy = get_dump_connector("sgbankruptcy", bankruptcy_dump)
    rental_flats = get_dump_connector("sgrentalflats", rental_dump)

    assert isinstance(bankruptcy, SGGovernmentBankruptcyConnector)
    assert isinstance(rental_flats, SGGovernmentRentalFlatsConnector)
