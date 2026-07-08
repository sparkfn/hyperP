"""Edge ``x``/``X`` junk-marker stripping for structured identifiers.

Some source systems pad masked or placeholder identifiers with runs of ``x`` /
``X`` at the start or end (e.g. ``xxx+6589251818``, ``xxxS7012164F``). These
markers carry no meaning for phone numbers and government IDs, so they are
removed before normalization.

Emails are deliberately **excluded** — ``x`` is frequently a legitimate part of
an email local part (names like ``xabby@``, handles like ``xxangel86xx@``, Apple
relay addresses), so stripping it there would corrupt real addresses.
"""

from __future__ import annotations

import re

_LEADING_X = re.compile(r"^[xX]+")
_TRAILING_X = re.compile(r"[xX]+$")


def strip_leading_x_markers(value: str) -> str:
    """Remove a leading run of ASCII ``x``/``X`` (after trimming whitespace).

    Leading-only: used for NRIC/FIN, whose trailing character may be a valid
    check letter ``X`` that must be preserved (e.g. ``F1234567X``).
    """
    return _LEADING_X.sub("", value.strip())


def strip_edge_x_markers(value: str) -> str:
    """Remove leading and trailing runs of ASCII ``x``/``X`` (after trimming).

    Both-ends: safe for phone numbers, which are digits/``+`` only and never
    legitimately begin or end with ``x``.
    """
    return _TRAILING_X.sub("", _LEADING_X.sub("", value.strip()))


def str_or_none(value: object) -> str | None:
    """Return ``value`` iff it is a non-blank string; otherwise ``None``.

    Shared by ingestion helpers that lift arbitrary ``JsonValue``/``object``
    inputs to optional string fields (e.g. product SKUs, LTA tags, serials).
    Use instead of ad-hoc ``isinstance(...) and str(...).strip()`` checks at
    every call site.
    """
    return value if isinstance(value, str) and value.strip() else None
