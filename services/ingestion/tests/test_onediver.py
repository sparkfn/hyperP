"""Tests for the OneDiver dump connectors."""

from __future__ import annotations

from pathlib import Path

from src.connectors.dumps.connectors import get_dump_connector

_SCHEMA = """
CREATE TABLE `profiles` (
  `id` int NOT NULL,
  `user_id` int,
  `ssi_master_id` varchar(20),
  `email` varchar(200),
  `first_name` varchar(50),
  `last_name` varchar(50),
  `gender` char(1),
  `birthday` date,
  `passport_full_name` varchar(100),
  `passport_number` varchar(20),
  `ic_number` varchar(50),
  `lk_nationality_code` char(2),
  `race` varchar(50),
  `address` varchar(1000),
  `address2` varchar(500),
  `city` varchar(100),
  `state` varchar(100),
  `lk_country_code` char(2),
  `zip_code` varchar(20),
  `contact_number` varchar(20),
  `lk_contact_country_code` varchar(5),
  `secondary_contact_number` varchar(20),
  `lk_secondary_contact_country_code` varchar(5),
  `Alternative_email` varchar(200),
  `membership_id` varchar(50),
  `dive_level` varchar(50),
  `dives` varchar(20),
  `last_dive` datetime,
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
  `lk_contact_country_code` varchar(5),
  `relation` varchar(100),
  `kin2_fname` varchar(100),
  `kin2_lname` varchar(100),
  `kin2_contact` varchar(50),
  `kin2_relation` varchar(100),
  `modified` datetime,
  `created` datetime
);
CREATE TABLE `users` (
  `id` int NOT NULL,
  `account_id` int,
  `username` varchar(50)
);
CREATE TABLE `accounts` (
  `id` int NOT NULL,
  `name` varchar(200)
);
CREATE TABLE `sales_orders` (
  `id` int NOT NULL,
  `order_id` varchar(20),
  `order_date` datetime,
  `accepted_date` datetime,
  `created` datetime,
  `billing_contact_name` varchar(100),
  `billing_contact_email` varchar(350),
  `billing_contact_number` varchar(50),
  `billing_country_code` varchar(20),
  `total` varchar(50),
  `status_code` varchar(50),
  `currency` varchar(20),
  `modified` datetime
);
"""


def _write_dump(tmp_path: Path, *inserts: str) -> Path:
    dump_path = tmp_path / "onediver.sql"
    dump_path.write_text(_SCHEMA + "\n" + "\n".join(inserts), encoding="utf-8")
    return dump_path


_PROFILE_INSERT = """\
INSERT INTO `profiles` (`id`, `user_id`, `ssi_master_id`, `email`, `first_name`, `last_name`, \
`gender`, `birthday`, `passport_full_name`, `passport_number`, `ic_number`, `lk_nationality_code`, \
`race`, `address`, `address2`, `city`, `state`, `lk_country_code`, `zip_code`, `contact_number`, \
`lk_contact_country_code`, `secondary_contact_number`, `lk_secondary_contact_country_code`, \
`Alternative_email`, `membership_id`, `dive_level`, `dives`, `last_dive`, `modified`, `created`, \
`is_deleted`) VALUES
(5, 2, 'SSI-001', 'ada@example.test', 'Ada', 'Lovelace', 'F', '1990-01-31', 'Ada Lovelace', \
'E6975636L', 'S1234567A', 'SG', 'Chinese', 'One Street', 'Block 2', 'Singapore', 'SG', '123456', \
'6599990000', '65', '6500000000', '65', 'ada.alt@example.test', 'M-100', 'Open Water', '42', NULL, \
'2026-05-06 02:00:00', '2026-05-01 01:00:00', 0);
"""

_USERS_INSERT = (
    "INSERT INTO `users` (`id`, `account_id`, `username`) "
    "VALUES (2, 1, 'ada.lovelace');"
)
_ACCOUNTS_INSERT = "INSERT INTO `accounts` (`id`, `name`) VALUES (1, 'Scubahub');"


def test_onediver_identity_envelope(tmp_path: Path) -> None:
    dump_path = _write_dump(tmp_path, _PROFILE_INSERT, _USERS_INSERT, _ACCOUNTS_INSERT)

    connector = get_dump_connector("onediver", dump_path)
    records = [r for r in connector.fetch_records() if r["record_type"] == "identity"]

    assert len(records) == 1
    rec = records[0]
    assert rec["source_record_id"] == "onediver-profile-5"
    id_pairs = {(i["type"], i["value"]) for i in rec["identifiers"]}
    assert ("email", "ada@example.test") in id_pairs
    assert ("email", "ada.alt@example.test") in id_pairs
    assert ("nric", "S1234567A") in id_pairs
    # passport is intentionally NOT emitted as an identifier (no downstream
    # normalizer/fanout-cap/govt-ID gate, and an un-hashed govt-ID Identifier
    # node would be a sensitive-data exposure with no match value); the raw
    # passport number lives only in raw_payload.profile.
    assert ("passport", "E6975636L") not in id_pairs
    assert ("phone", "6599990000") in id_pairs
    assert ("phone", "6500000000") in id_pairs
    assert rec["attributes"]["full_name"] == "Ada Lovelace"
    assert rec["attributes"]["passport_full_name"] == "Ada Lovelace"
    assert rec["attributes"]["nationality"] == "SG"
    assert rec["attributes"]["shop_name"] == "Scubahub"
    assert rec["attributes"]["username"] == "ada.lovelace"
    assert rec["addresses"][0]["city"] == "Singapore"
    assert rec["addresses"][0]["postal_code"] == "123456"


def test_onediver_skips_deleted_profiles(tmp_path: Path) -> None:
    deleted = """\
INSERT INTO `profiles` (`id`, `user_id`, `email`, `first_name`, `last_name`, `is_deleted`) VALUES
(9, 0, 'gone@example.test', 'Ghost', 'Person', 1);
"""
    dump_path = _write_dump(tmp_path, deleted)

    connector = get_dump_connector("onediver", dump_path)
    records = list(connector.fetch_records())

    assert records == []


def test_onediver_relationship_envelopes(tmp_path: Path) -> None:
    emergency = """\
INSERT INTO `profile_emergencies` (`id`, `profile_id`, `contact_first_name`, \
`contact_last_name`, `contact_number`, `lk_contact_country_code`, `relation`, \
`kin2_fname`, `kin2_lname`, `kin2_contact`, `kin2_relation`, `modified`, `created`) VALUES
(10, 5, 'Charles', 'Babbage', '6598880000', '65', 'Father', 'Annie', 'Lovelace', '6598770000', \
'Mother', '2026-05-06 02:00:00', '2026-05-01 01:00:00');
"""
    dump_path = _write_dump(tmp_path, _PROFILE_INSERT, _USERS_INSERT, _ACCOUNTS_INSERT, emergency)

    records = list(get_dump_connector("onediver", dump_path).fetch_records())
    relationships = [r for r in records if r["record_type"] == "relationship"]
    assert len(relationships) == 2
    by_slot = {r["raw_payload"]["kin_slot"]: r for r in relationships}

    kin1 = by_slot["kin1"]
    assert kin1["source_record_id"] == "onediver-emergency-10-kin1"
    assert kin1["attributes"]["full_name"] == "Charles Babbage"
    assert kin1["attributes"]["relationship_to_referrer"] == "Father"
    assert kin1["raw_payload"]["linked_to_source_record_id"] == "onediver-profile-5"
    assert any(i["type"] == "phone" and i["value"] == "6598880000" for i in kin1["identifiers"])

    kin2 = by_slot["kin2"]
    assert kin2["source_record_id"] == "onediver-emergency-10-kin2"
    assert kin2["attributes"]["full_name"] == "Annie Lovelace"


def test_onediver_sales_envelope_links_by_email(tmp_path: Path) -> None:
    sales = """\
INSERT INTO `sales_orders` (`id`, `order_id`, `order_date`, `accepted_date`, `created`, \
`billing_contact_name`, `billing_contact_email`, `billing_contact_number`, `billing_country_code`, \
`total`, `status_code`, `currency`, `modified`) VALUES
(1, 'SO-1', '2026-05-02 03:00:00', NULL, '2026-05-02 03:05:00', 'Ada Lovelace', \
'ada@example.test', '6599990000', '65', '250.00', 'ACCEPTED', 'SGD', '2026-05-02 03:05:00'),
(2, 'SO-2', '2026-05-03 03:00:00', NULL, '2026-05-03 03:05:00', 'Walk In', \
'walkin@example.test', '6588880000', '65', '50.00', 'ACCEPTED', 'SGD', '2026-05-03 03:05:00');
"""
    dump_path = _write_dump(tmp_path, _PROFILE_INSERT, sales)

    records = list(get_dump_connector("onediver:sales", dump_path).fetch_records())
    assert len(records) == 2
    by_id = {r["source_record_id"]: r for r in records}

    linked = by_id["onediver-salesorder-1"]
    assert linked["record_type"] == "sales"
    # Sales records carry no identifiers and no attributes — the person link is
    # carried solely via raw_payload.customer_link (matches every sibling sales
    # connector). Order facts live only in raw_payload.order.
    assert linked["identifiers"] == []
    assert linked["attributes"] == {}
    assert linked["raw_payload"]["order"]["total_amount"] == 250.0
    assert linked["raw_payload"]["order"]["order_no"] == "SO-1"
    assert linked["raw_payload"]["order"]["currency"] == "SGD"
    assert (
        linked["raw_payload"]["customer_link"]["identity_source_record_id"]
        == "onediver-profile-5"
    )
    assert linked["raw_payload"]["customer_link"]["source_system_key"] == "onediver"

    unlinked = by_id["onediver-salesorder-2"]
    assert "customer_link" not in unlinked["raw_payload"]


def test_onediver_sales_skips_deleted_profile_email(tmp_path: Path) -> None:
    # A sales order billed to a *deleted* profile's email must NOT link: the
    # identity connector never emits a source record for deleted profiles, so
    # the sales connector's email_to_id index must skip them too (else the
    # customer_link would dangle on a never-emitted source_record_id).
    deleted = """\
INSERT INTO `profiles` (`id`, `user_id`, `email`, `first_name`, `last_name`, `is_deleted`) VALUES
(5, 2, 'ada@example.test', 'Ada', 'Lovelace', 1);
"""
    sales = """\
INSERT INTO `sales_orders` (`id`, `order_id`, `order_date`, `accepted_date`, `created`, \
`billing_contact_name`, `billing_contact_email`, `billing_contact_number`, `billing_country_code`, \
`total`, `status_code`, `currency`, `modified`) VALUES
(1, 'SO-1', '2026-05-02 03:00:00', NULL, '2026-05-02 03:05:00', 'Ada Lovelace', \
'ada@example.test', '6599990000', '65', '250.00', 'ACCEPTED', 'SGD', '2026-05-02 03:05:00');
"""
    dump_path = _write_dump(tmp_path, deleted, sales)

    records = list(get_dump_connector("onediver:sales", dump_path).fetch_records())
    assert len(records) == 1
    assert "customer_link" not in records[0]["raw_payload"]


def test_onediver_zero_date_falls_back_to_created(tmp_path: Path) -> None:
    # MySQL zero-date sentinel '0000-00-00 00:00:00' must normalize to None in
    # to_iso, so the connector's `to_iso(modified) or to_iso(created)` falls
    # back to the created timestamp instead of writing the garbage string.
    profile = """\
INSERT INTO `profiles` (`id`, `user_id`, `email`, `first_name`, `last_name`, \
`modified`, `created`, `is_deleted`) VALUES
(5, 2, 'ada@example.test', 'Ada', 'Lovelace', '0000-00-00 00:00:00', \
'2026-05-01 01:00:00', 0);
"""
    dump_path = _write_dump(tmp_path, profile)

    records = [
        r for r in get_dump_connector("onediver", dump_path).fetch_records()
        if r["record_type"] == "identity"
    ]
    assert len(records) == 1
    assert records[0]["observed_at"] == "2026-05-01T01:00:00Z"


def test_onediver_sales_connector_source_key(tmp_path: Path) -> None:
    dump_path = _write_dump(tmp_path)
    assert get_dump_connector("onediver:sales", dump_path).get_source_key() == "onediver:sales"


def test_onediver_relationship_zero_date_falls_back_to_created(tmp_path: Path) -> None:
    # Regression guard: the relationship envelope's observed_at must use the
    # zero-date-safe to_iso_first form (same as identity), not to_iso(a or b) —
    # a zero-date `modified` on a profile_emergencies row falls back to `created`
    # instead of yielding None (which would crash model_validate, observed_at
    # being a non-optional str).
    profile = """\
INSERT INTO `profiles` (`id`, `user_id`, `email`, `first_name`, `last_name`, `is_deleted`) VALUES
(5, 2, 'ada@example.test', 'Ada', 'Lovelace', 0);
"""
    emergency = """\
INSERT INTO `profile_emergencies` (`id`, `profile_id`, `contact_first_name`, \
`contact_last_name`, `contact_number`, `lk_contact_country_code`, `relation`, \
`kin2_fname`, `kin2_lname`, `kin2_contact`, `kin2_relation`, `modified`, `created`) VALUES
(10, 5, 'Charles', 'Babbage', '6598880000', '65', 'Father', '', '', '', '', \
'0000-00-00 00:00:00', '2026-05-01 01:00:00');
"""
    dump_path = _write_dump(tmp_path, profile, emergency)

    relationships = [
        r for r in get_dump_connector("onediver", dump_path).fetch_records()
        if r["record_type"] == "relationship"
    ]
    assert len(relationships) == 1
    assert relationships[0]["observed_at"] == "2026-05-01T01:00:00Z"


def test_onediver_sales_skips_null_pk_profile(tmp_path: Path) -> None:
    # A profile with a NULL id is skipped by the identity loop AND the email_to_id
    # index, so a sales order billed to its email must not produce a dangling
    # 'onediver-profile-None' customer_link.
    null_pk = """\
INSERT INTO `profiles` (`id`, `user_id`, `email`, `first_name`, `last_name`, `is_deleted`) VALUES
(NULL, 2, 'nullpk@example.test', 'Null', 'Pk', 0);
"""
    sales = """\
INSERT INTO `sales_orders` (`id`, `order_id`, `order_date`, `accepted_date`, `created`, \
`billing_contact_name`, `billing_contact_email`, `billing_contact_number`, `billing_country_code`, \
`total`, `status_code`, `currency`, `modified`) VALUES
(1, 'SO-1', '2026-05-02 03:00:00', NULL, '2026-05-02 03:05:00', 'Null Pk', \
'nullpk@example.test', '6599990000', '65', '250.00', 'ACCEPTED', 'SGD', '2026-05-02 03:05:00');
"""
    dump_path = _write_dump(tmp_path, null_pk, sales)

    records = list(get_dump_connector("onediver:sales", dump_path).fetch_records())
    assert len(records) == 1
    assert "customer_link" not in records[0]["raw_payload"]


def test_onediver_identity_both_timestamps_zero(tmp_path: Path) -> None:
    # A profile whose every timestamp column is the MySQL zero-date sentinel has
    # no valid observed_at. The connector must emit it with observed_at=None
    # (not crash) — the SourceRecordEnvelope model accepts None and the pipeline
    # stores a null graph property (the API falls back to ingested_at).
    profile = """\
INSERT INTO `profiles` (`id`, `user_id`, `email`, `first_name`, `last_name`, \
`modified`, `created`, `is_deleted`) VALUES
(5, 2, 'ada@example.test', 'Ada', 'Lovelace', '0000-00-00 00:00:00', \
'0000-00-00 00:00:00', 0);
"""
    dump_path = _write_dump(tmp_path, profile)

    records = [
        r for r in get_dump_connector("onediver", dump_path).fetch_records()
        if r["record_type"] == "identity"
    ]
    assert len(records) == 1
    assert records[0]["observed_at"] is None


def test_source_record_envelope_accepts_none_observed_at() -> None:
    # Regression guard: the model must accept observed_at=None (a source row with
    # no valid timestamp) without raising — otherwise one such row crashes the
    # whole ingestion run via model_validate.
    from src.models import RecordType, SourceRecordEnvelope

    env = SourceRecordEnvelope(
        source_system="onediver",
        source_record_id="onediver-profile-1",
        record_type=RecordType.IDENTITY,
        observed_at=None,
        record_hash="sha256:abc",
    )
    assert env.observed_at is None