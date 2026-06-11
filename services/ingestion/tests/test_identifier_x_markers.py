"""Junk x/X edge-marker cleaning for phone + NRIC (emails intentionally untouched)."""

from __future__ import annotations

from src.models import QualityFlag
from src.normalizers.clean import strip_edge_x_markers, strip_leading_x_markers
from src.normalizers.email import normalize_email
from src.normalizers.nric import normalize_nric
from src.normalizers.phone import normalize_phone


def test_strip_leading_x_markers() -> None:
    assert strip_leading_x_markers("xxxS7012164F") == "S7012164F"
    assert strip_leading_x_markers("XXS123") == "S123"
    assert strip_leading_x_markers("  xx S123 ") == " S123"  # only outer whitespace trimmed
    assert strip_leading_x_markers("S7012164F") == "S7012164F"  # no markers, unchanged
    assert strip_leading_x_markers("F1234567X") == "F1234567X"  # trailing X preserved
    assert strip_leading_x_markers("xxxx") == ""  # all markers


def test_strip_edge_x_markers_both_ends() -> None:
    assert strip_edge_x_markers("xxx+6589251818") == "+6589251818"
    assert strip_edge_x_markers("+6589237903xxx") == "+6589237903"
    assert strip_edge_x_markers("xx+65123xx") == "+65123"
    assert strip_edge_x_markers("xxxxxx") == ""
    assert strip_edge_x_markers("9123x4567") == "9123x4567"  # internal x untouched


def test_normalize_phone_recovers_x_padded_numbers() -> None:
    assert normalize_phone("xxx+6589251818") == ("+6589251818", QualityFlag.VALID)
    assert normalize_phone("xxxxxx+6591276203") == ("+6591276203", QualityFlag.VALID)
    # all-x collapses to empty -> invalid
    assert normalize_phone("xxxx") == (None, QualityFlag.INVALID_FORMAT)
    # a clean number is unaffected
    assert normalize_phone("+6589251818") == ("+6589251818", QualityFlag.VALID)


def test_normalize_nric_strips_leading_x_and_uppercases() -> None:
    assert normalize_nric("xxxS7012164F") == ("S7012164F", QualityFlag.VALID)
    assert normalize_nric("xxxxS8110609F") == ("S8110609F", QualityFlag.VALID)
    assert normalize_nric("s8929303j") == ("S8929303J", QualityFlag.VALID)
    assert normalize_nric("S8708366g") == ("S8708366G", QualityFlag.VALID)
    # trailing X is a valid check letter and must survive
    assert normalize_nric("F1234567X") == ("F1234567X", QualityFlag.VALID)
    assert normalize_nric("  S7012164F  ") == ("S7012164F", QualityFlag.VALID)
    assert normalize_nric("xxxx") == (None, QualityFlag.INVALID_FORMAT)


def test_email_x_prefix_is_not_stripped() -> None:
    # Emails legitimately contain x — normalization must leave it alone.
    assert normalize_email("xabby@gmail.com")[0] == "xabby@gmail.com"
    assert normalize_email("xxangel86xx@live.com")[0] == "xxangel86xx@live.com"
