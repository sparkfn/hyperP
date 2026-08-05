"""Bounded taxonomy projection for CRM-WON discovery reports."""

from __future__ import annotations

import hashlib
import re

_TAXONOMY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_CATEGORY_ID = re.compile(r"^[0-9]{1,12}$")
SUPPORTED_CURRENCIES = frozenset({"SGD", "USD", "MYR"})
ISO_4217_CURRENCIES = frozenset(
    """AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND
    BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC
    CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD
    GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS
    KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT
    MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK
    PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD
    SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI
    UYU UYW UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF
    XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW ZWG""".split()
)
KNOWN_SOURCES = frozenset({"bitrix_chat"})
KNOWN_RECORD_TYPES = frozenset(
    {
        "bankruptcy",
        "call",
        "conversation",
        "crm_deal",
        "crm_history",
        "identity",
        "relationship",
        "rental_flat",
        "sales",
    }
)


def taxonomy(value: object) -> str:
    """Return a bounded generic taxonomy value or a safe report band."""
    if isinstance(value, str) and _TAXONOMY.fullmatch(value):
        return value
    return "invalid_or_unknown"


def source_taxonomy(value: object) -> str:
    """Expose only source systems declared by this connector contract."""
    return value if isinstance(value, str) and value in KNOWN_SOURCES else "invalid_or_unknown"


def record_type_taxonomy(value: object) -> str:
    """Expose only record types in the repository's closed provenance contract."""
    return value if isinstance(value, str) and value in KNOWN_RECORD_TYPES else "invalid_or_unknown"


def source_identity_token(value: object, index: int) -> str:
    """Preserve distinct internal source identities without emitting their values."""
    if not isinstance(value, str) or not value:
        return f"missing-source-{index}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def scoped_entity(value: object, entity_keys: tuple[str, ...]) -> str:
    """Expose only entities explicitly selected for the discovery run."""
    if not isinstance(value, str):
        return "invalid_or_unknown"
    return value if value in entity_keys else "outside_requested_scope"


def stage_taxonomy_for_entity(
    value: object, entity_key: str, stage_catalog: dict[str, frozenset[str]]
) -> str:
    """Expose a stage only when supplied by the validated entity policy."""
    if not isinstance(value, str):
        return "invalid_or_unknown"
    return value if value in stage_catalog.get(entity_key, frozenset()) else "unmapped_or_unknown"


def category_status(value: object) -> str:
    """Report category shape without emitting the source value."""
    if value is None or value == "":
        return "missing"
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if isinstance(text, str) and _CATEGORY_ID.fullmatch(text):
        return "present_valid_shape"
    return "invalid_or_unknown"


def currency_taxonomy(value: object) -> str:
    """Expose supported currencies and safely band other ISO-shaped values."""
    if isinstance(value, str) and value in SUPPORTED_CURRENCIES:
        return value
    if isinstance(value, str) and value in ISO_4217_CURRENCIES:
        return "valid_but_unsupported"
    return "invalid_or_unknown"


def currency_status(value: object) -> str:
    """Distinguish missing/malformed currency from valid unsupported currency."""
    if value is None or value == "":
        return "missing"
    projected = currency_taxonomy(value)
    if projected in SUPPORTED_CURRENCIES:
        return "present_supported"
    if projected == "valid_but_unsupported":
        return "present_valid_but_unsupported"
    return "invalid"
