"""Tests for NRIC/FIN checksum validation according to the official modulo-11 algorithm."""

from __future__ import annotations

from src.normalizers.nric import nric_checksum_valid


def test_nric_checksum_valid_authoritative_fixtures() -> None:
    # Authoritative fixtures from plan §3
    assert nric_checksum_valid("S1234567D") is True
    assert nric_checksum_valid("T1234567J") is True
    assert nric_checksum_valid("F1234567N") is True
    assert nric_checksum_valid("G1234567X") is True


def test_nric_checksum_valid_existing_and_extra_fixtures() -> None:
    # Existing fixtures from codebase and extra valid samples
    assert nric_checksum_valid("S7012164F") is True
    assert nric_checksum_valid("S8110609F") is True
    assert nric_checksum_valid("S8929303J") is True
    assert nric_checksum_valid("S8708366G") is True
    assert nric_checksum_valid("T0123456G") is True


def test_nric_checksum_invalid_check_letter() -> None:
    assert nric_checksum_valid("S7012164A") is False
    assert nric_checksum_valid("F1234567X") is False
    assert nric_checksum_valid("T0123456J") is False


def test_nric_checksum_invalid_shape_or_prefix() -> None:
    assert nric_checksum_valid("S123456") is False
    assert nric_checksum_valid("M1234567K") is False
    assert nric_checksum_valid("") is False
    assert nric_checksum_valid("invalid-nric") is False


def test_nric_checksum_placeholder_seven_identical_digits_fails() -> None:
    # S0000000J passes pure math (0 % 11 -> J), but fails placeholder check
    assert nric_checksum_valid("S0000000J") is False
    assert nric_checksum_valid("S1111111A") is False
