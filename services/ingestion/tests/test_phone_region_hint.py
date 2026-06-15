"""phone_region_hint() — region derivation from POS country/phone_code columns."""

from __future__ import annotations

from src.connectors.fundbox.builders import IdentifierBag, phone_region_hint


def test_phone_code_takes_precedence() -> None:
    assert phone_region_hint("60", "Singapore") == "MY"


def test_phone_code_singapore() -> None:
    assert phone_region_hint("65", "SG") == "SG"


def test_falls_back_to_country_text_when_no_phone_code() -> None:
    assert phone_region_hint(None, "Malaysia") == "MY"


def test_country_text_is_case_and_whitespace_insensitive() -> None:
    assert phone_region_hint(None, " SIngapore ") == "SG"


def test_unrecognized_country_returns_none() -> None:
    assert phone_region_hint(None, "Atlantis") is None


def test_non_numeric_phone_code_falls_back_to_country() -> None:
    assert phone_region_hint("unknown", "Malaysia") == "MY"


def test_both_absent_returns_none() -> None:
    assert phone_region_hint(None, None) is None


def test_identifier_bag_carries_region_hint() -> None:
    bag = IdentifierBag()
    bag.add("phone", "123456789", region_hint="MY")
    bag.add("email", "ada@example.com")
    assert bag.items[0] == {
        "type": "phone",
        "value": "123456789",
        "is_verified": False,
        "region_hint": "MY",
    }
    assert "region_hint" not in bag.items[1]
