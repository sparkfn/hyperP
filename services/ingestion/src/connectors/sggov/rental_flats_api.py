"""API extraction for the SG rental-flats address inventory."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import httpx

from src.connectors.base import SourceConnector
from src.connectors.sggov.rental_flats import build_rental_flat_envelope
from src.connectors.sggov.rental_flats_api_models import RentalFlatPage, RentalFlatRow
from src.models import JsonValue


def _postgres_dump_datetime(value: datetime) -> str:
    rendered = value.isoformat(sep=" ")
    if not rendered.endswith("+00:00"):
        return rendered
    timestamp = rendered[:-6]
    if "." in timestamp:
        timestamp = timestamp.rstrip("0").rstrip(".")
    return f"{timestamp}+00"


def _legacy_dump_text(value: str) -> str:
    """Reproduce the current COPY parser's decoded representation."""
    encoded = (
        value.replace("\\", r"\\")
        .replace("\b", r"\b")
        .replace("\f", r"\f")
        .replace("\v", r"\v")
        .replace("\n", r"\n")
        .replace("\t", r"\t")
        .replace("\r", r"\r")
    )
    return (
        encoded.replace(r"\n", "\n")
        .replace(r"\t", "\t")
        .replace(r"\r", "\r")
        .replace(r"\\", "\\")
    )


class SGGovernmentRentalFlatsApiClient:
    @staticmethod
    def validate_config(*, base_url: str, api_key: str, page_size: int) -> None:
        if not base_url:
            raise ValueError("SG rental flats API base URL is required")
        if not api_key:
            raise ValueError("SG rental flats API key is required")
        if not 1 <= page_size <= 1000:
            raise ValueError("SG rental flats API page size must be between 1 and 1000")

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        page_size: int,
        http: httpx.Client | None = None,
    ) -> None:
        self.validate_config(base_url=base_url, api_key=api_key, page_size=page_size)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._page_size = page_size
        self._http = http or httpx.Client(timeout=30.0)

    def iter_flats(self) -> Iterator[RentalFlatRow]:
        offset = 0
        expected_total: int | None = None
        while True:
            response = self._http.get(
                f"{self._base_url}/integrations/hyperp/rental-flats",
                params={"limit": self._page_size, "offset": offset},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
            page = RentalFlatPage.model_validate(response.json())
            if page.offset != offset:
                raise ValueError(
                    f"SG rental flats API returned offset {page.offset}, expected {offset}"
                )
            if page.limit != self._page_size:
                raise ValueError(
                    f"SG rental flats API returned limit {page.limit}, "
                    f"expected {self._page_size}"
                )
            if len(page.items) > page.limit:
                raise ValueError("SG rental flats API returned more items than the page limit")
            if expected_total is None:
                expected_total = page.total
            elif page.total != expected_total:
                raise ValueError("SG rental flats API total changed during pagination")
            consumed = offset + len(page.items)
            if consumed > page.total:
                raise ValueError("SG rental flats API page exceeds the reported total")
            if not page.items and consumed < page.total:
                raise ValueError("SG rental flats API returned an empty page before total")
            if consumed < page.total:
                if page.next_offset is None:
                    raise ValueError("SG rental flats API terminated before total")
                if page.next_offset != consumed:
                    raise ValueError(
                        f"SG rental flats API next offset {page.next_offset}, expected {consumed}"
                    )
            elif page.next_offset is not None:
                raise ValueError("SG rental flats API returned a next offset after total")
            yield from page.items
            if page.next_offset is None:
                return
            offset = page.next_offset

    def close(self) -> None:
        self._http.close()


class SGGovernmentRentalFlatsApiConnector(SourceConnector):
    def __init__(self, client: SGGovernmentRentalFlatsApiClient) -> None:
        self._client = client

    def get_source_key(self) -> str:
        return "sgrentalflats"

    def close(self) -> None:
        self._client.close()

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        try:
            for flat in self._client.iter_flats():
                block_no = _legacy_dump_text(flat.block_no)
                street_name = _legacy_dump_text(flat.street_name)
                postal_code = _legacy_dump_text(flat.postal_code)
                flat_type = _legacy_dump_text(flat.flat_type)
                town_name = _legacy_dump_text(flat.town.name)
                town_map_id = _legacy_dump_text(flat.town.map_id)
                town_map_zone = (
                    _legacy_dump_text(flat.town.map_zone)
                    if flat.town.map_zone is not None
                    else None
                )
                first_seen_at = _postgres_dump_datetime(flat.first_seen_at)
                last_seen_at = _postgres_dump_datetime(flat.last_seen_at)
                raw_payload: dict[str, JsonValue] = {
                    "flat": {
                        "id": str(flat.id),
                        "town_id": str(flat.town.id),
                        "block_no": block_no,
                        "street_name": street_name,
                        "postal_code": postal_code,
                        "flat_type": flat_type,
                        "first_seen_at": first_seen_at,
                        "last_seen_at": last_seen_at,
                        "is_active": "t" if flat.is_active else "f",
                    },
                    "town": {
                        "id": str(flat.town.id),
                        "name": town_name,
                        "map_id": town_map_id,
                        "map_zone": town_map_zone,
                    },
                }
                yield build_rental_flat_envelope(
                    flat_id=str(flat.id),
                    town_id=str(flat.town.id),
                    block_no=block_no,
                    street_name=street_name,
                    postal_code=postal_code,
                    flat_type=flat_type,
                    town_name=town_name,
                    town_map_id=town_map_id,
                    town_map_zone=town_map_zone or "",
                    is_active=flat.is_active,
                    observed_at=last_seen_at,
                    raw_payload=raw_payload,
                )
        finally:
            self._client.close()
