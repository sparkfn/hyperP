"""Behavioural guards for connector skip sites.

Each test feeds real source rows to real connector code and asserts the buffered
``ConnectorSkip`` rows (reason + source_record_id + locator). A skipped row must
never add to or remove from the emitted record set, so every test also pins the
emitted records.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from src.connectors.base import SourceConnector
from src.connectors.dumps.connectors import get_dump_connector


def _skip_rows(connector: SourceConnector) -> list[tuple[str, str | None, str]]:
    return [(skip.reason, skip.source_record_id, skip.locator) for skip in connector.drain_skips()]


_PHPPOS_DUMP = """
CREATE TABLE `phppos_people` (
  `person_id` int NOT NULL,
  `first_name` varchar(255),
  `last_name` varchar(255),
  `full_name` varchar(255),
  `phone_number` varchar(255),
  `email` varchar(255),
  `create_date` datetime,
  `last_modified` datetime
);
CREATE TABLE `phppos_customers` (
  `id` int NOT NULL,
  `person_id` int,
  `deleted` int,
  `account_number` varchar(255),
  `company_name` varchar(255),
  `custom_field_1_value` varchar(255)
);
INSERT INTO `phppos_people` VALUES
(7,'Ada','Lovelace','Ada Lovelace','6599990000','ada@example.test',
'2026-05-01 01:00:00','2026-05-06 02:00:00');
INSERT INTO `phppos_customers` VALUES
(11,7,0,'ACC-11','Ada Co','S1234567A');
INSERT INTO `phppos_customers` VALUES
(12,8,1,'ACC-12','Deleted Co','S1234568B');
INSERT INTO `phppos_customers` VALUES
(13,99,0,'ACC-13','Orphan Co','S1234569C');
"""


def test_eko_dump_connector_ledgers_deleted_customer_and_missing_person_row(
    tmp_path: Path,
) -> None:
    dump_path = tmp_path / "eko.sql"
    dump_path.write_text(_PHPPOS_DUMP, encoding="utf-8")

    connector = get_dump_connector("eko_phppos", dump_path)
    records = list(connector.fetch_records())

    assert [record["source_record_id"] for record in records] == ["eko_phppos-customer-7"]
    assert _skip_rows(connector) == [
        ("deleted_customer", "eko_phppos-customer-8", "phppos_customers#id=12"),
        ("missing_person_row", None, "phppos_customers#id=13,person_id=99"),
    ]


def test_speedzone_dump_connector_ledgers_deleted_customer_and_missing_person_row(
    tmp_path: Path,
) -> None:
    dump_path = tmp_path / "speedzone.sql"
    dump_path.write_text(_PHPPOS_DUMP, encoding="utf-8")

    connector = get_dump_connector("speedzone_phppos", dump_path)
    records = list(connector.fetch_records())

    assert [record["source_record_id"] for record in records] == ["speedzone_phppos-customer-7"]
    assert _skip_rows(connector) == [
        ("deleted_customer", "speedzone_phppos-customer-8", "phppos_customers#id=12"),
        ("missing_person_row", None, "phppos_customers#id=13,person_id=99"),
    ]


def test_fundbox_sales_dump_connector_ledgers_order_status_excluded(tmp_path: Path) -> None:
    dump_path = tmp_path / "fundbox.sql"
    dump_path.write_text(
        "\n".join(
            [
                (
                    "INSERT INTO `orders` "
                    "(`id`,`order_no`,`user_id`,`merchant_id`,`status`,"
                    "`created_at`,`updated_at`) "
                    "VALUES (1,'INV-1',20,5,'cancelled',"
                    "'2026-05-01 00:00:00','2026-05-01 00:00:00');"
                ),
                (
                    "INSERT INTO `orders` "
                    "(`id`,`order_no`,`user_id`,`merchant_id`,`status`,"
                    "`created_at`,`updated_at`) "
                    "VALUES (2,'INV-2',21,5,'completed',"
                    "'2026-05-01 00:00:00','2026-05-01 00:00:00');"
                ),
            ]
        ),
        encoding="utf-8",
    )

    connector = get_dump_connector("fundbox:sales", dump_path)
    records = list(connector.fetch_records())

    assert [record["source_record_id"] for record in records] == ["fundbox-order-2"]
    assert _skip_rows(connector) == [
        ("order_status_excluded", "fundbox-order-1", "orders#id=1"),
    ]


def test_whatsapp_dump_connector_ledgers_session_and_empty_content_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump_path = tmp_path / "whatsapp.sql"
    dump_path.write_text(
        """
COPY public.orgs (id, name) FROM stdin;
org-1	EkoLife SG
\\.
COPY public.sessions (id, org_id, status, whatsapp_user_id) FROM stdin;
session-closed	org-1	closed	6500000001@c.us
session-unmapped	org-404	ready	6500000002@c.us
session-no-user	org-1	ready	\\N
session-ready	org-1	ready	6500000000@c.us
\\.
COPY public.chats (id, name, whatsapp_user_id) FROM stdin;
chat-1	Ada Chat	6500000000@c.us
\\.
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.connectors.dumps.connectors.chat_batch_size", lambda: 20)
    monkeypatch.setattr("src.connectors.dumps.connectors.chat_batch_max_chars", lambda: 1_000_000)

    connector = get_dump_connector("whatsapp_chat", dump_path)
    records = list(connector.fetch_records())

    assert records == []
    assert _skip_rows(connector) == [
        ("session_status_excluded", None, "sessions#id=session-closed"),
        ("org_unmapped", None, "sessions#id=session-unmapped"),
        ("missing_whatsapp_user_id", None, "sessions#id=session-no-user"),
        ("empty_content", None, "chats#id=chat-1"),
    ]


_ONEDIVER_DUMP = """
CREATE TABLE `profiles` (
  `id` int NOT NULL,
  `user_id` int,
  `email` varchar(200),
  `first_name` varchar(50),
  `last_name` varchar(50),
  `modified` datetime,
  `created` datetime,
  `is_deleted` int
);
CREATE TABLE `profile_emergencies` (
  `id` int NOT NULL,
  `profile_id` int,
  `contact_first_name` varchar(100),
  `contact_last_name` varchar(100),
  `contact_number` varchar(50),
  `relation` varchar(100),
  `kin2_fname` varchar(100),
  `kin2_lname` varchar(100),
  `kin2_contact` varchar(50),
  `kin2_relation` varchar(100)
);
INSERT INTO `profiles` (`id`, `user_id`, `email`, `first_name`, `last_name`, `modified`, `created`, `is_deleted`) VALUES
(5, 2, 'ada@example.test', 'Ada', 'Lovelace', '2026-05-06 02:00:00', '2026-05-01 01:00:00', 0);
INSERT INTO `profiles` (`id`, `user_id`, `email`, `first_name`, `last_name`, `modified`, `created`, `is_deleted`) VALUES
(9, 2, 'ghost@example.test', 'Ghost', 'Person', '2026-05-06 02:00:00', '2026-05-01 01:00:00', 1);
INSERT INTO `profiles` (`id`, `user_id`, `email`, `first_name`, `last_name`, `modified`, `created`, `is_deleted`) VALUES
(NULL, 2, 'noid@example.test', 'No', 'Id', '2026-05-06 02:00:00', '2026-05-01 01:00:00', 0);
INSERT INTO `profile_emergencies` (`id`, `profile_id`, `contact_first_name`, `contact_last_name`, `contact_number`, `relation`, `kin2_fname`, `kin2_lname`, `kin2_contact`, `kin2_relation`) VALUES
(10, 9, 'Orphan', 'Contact', '6598880000', 'Friend', NULL, NULL, NULL, NULL);
INSERT INTO `profile_emergencies` (`id`, `profile_id`, `contact_first_name`, `contact_last_name`, `contact_number`, `relation`, `kin2_fname`, `kin2_lname`, `kin2_contact`, `kin2_relation`) VALUES
(11, 5, 'Kin', 'One', '6598770000', 'Friend', 'Kin', 'Two', '6598660000', 'Friend');
"""


def test_onediver_dump_connector_ledgers_deleted_missing_and_orphan_rows(tmp_path: Path) -> None:
    dump_path = tmp_path / "onediver.sql"
    dump_path.write_text(_ONEDIVER_DUMP, encoding="utf-8")

    connector = get_dump_connector("onediver", dump_path)
    records = list(connector.fetch_records())

    assert [record["source_record_id"] for record in records] == [
        "onediver-profile-5",
        "onediver-emergency-11-kin1",
        "onediver-emergency-11-kin2",
    ]
    assert _skip_rows(connector) == [
        ("deleted_profile", "onediver-profile-9", "profiles#id=9"),
        ("missing_id", None, "profiles#missing_id"),
        ("orphan_or_deleted_parent", None, "profile_emergencies#id=10,profile_id=9"),
    ]


def test_sgbankruptcy_dump_connector_ledgers_missing_id(tmp_path: Path) -> None:
    dump_path = tmp_path / "bankruptcy.sql"
    dump_path.write_text(
        """
COPY public.bankruptcy_cases (id, case_number, identification_number, person_name) FROM stdin;
1	1561/2025	S9350236A	Ada Lovelace
\\N	1562/2026	S9350237B	No Identifier
\\.
""".strip(),
        encoding="utf-8",
    )

    connector = get_dump_connector("sgbankruptcy", dump_path)
    records = list(connector.fetch_records())

    assert [record["source_record_id"] for record in records] == ["bankruptcy_case:1"]
    assert _skip_rows(connector) == [
        ("missing_id", None, "cases#missing_id"),
    ]
