from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from src.config import Settings
from src.connectors.sggov.rental_flats import SGGovernmentRentalFlatsConnector
from src.connectors.sggov.rental_flats_api import (
    SGGovernmentRentalFlatsApiClient,
    SGGovernmentRentalFlatsApiConnector,
)
from src.main import create_sgrentalflats_api_client, get_connector, run_ingestion


def _page(
    *,
    offset: int,
    next_offset: int | None,
    town_map_zone: str | None = "CCK",
    street_name: str = "Teck Whye Cres",
) -> dict[str, object]:
    return {
        "items": [
            {
                "id": offset + 1,
                "block_no": "165A",
                "street_name": street_name,
                "postal_code": "681165",
                "flat_type": "1-room & 2-room",
                "first_seen_at": "2026-05-08T08:24:42.132480Z",
                "last_seen_at": "2026-05-08T09:47:25.177970Z",
                "is_active": offset == 0,
                "town": {
                    "id": 9,
                    "name": "Choa Chu Kang Town",
                    "map_id": "choa_chu_kang",
                    "map_zone": town_map_zone,
                },
            }
        ],
        "total": 2,
        "limit": 1,
        "offset": offset,
        "next_offset": next_offset,
    }


def _copy_encode(value: str) -> str:
    return (
        value.replace("\\", r"\\")
        .replace("\b", r"\b")
        .replace("\f", r"\f")
        .replace("\v", r"\v")
        .replace("\n", r"\n")
        .replace("\t", r"\t")
        .replace("\r", r"\r")
    )


def _write_dump(
    path: Path,
    *,
    town_map_zone: str | None = "CCK",
    street_name: str = "Teck Whye Cres",
) -> None:
    dump_map_zone = town_map_zone if town_map_zone is not None else r"\N"
    path.write_text(
        "COPY public.flats "
        "(id, town_id, block_no, street_name, postal_code, flat_type, "
        "first_seen_at, last_seen_at, is_active) FROM stdin;\n"
        f"1\t9\t165A\t{_copy_encode(street_name)}\t681165\t1-room & 2-room\t"
        "2026-05-08 08:24:42.13248+00\t2026-05-08 09:47:25.17797+00\tt\n"
        "\\.\n"
        "COPY public.towns (id, name, map_id, map_zone) FROM stdin;\n"
        f"9\tChoa Chu Kang Town\tchoa_chu_kang\t{dump_map_zone}\n"
        "\\.\n",
        encoding="utf-8",
    )


def test_api_connector_fetches_all_pages_and_maps_dump_equivalent_envelopes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(request.url.params["offset"])
        next_offset = 1 if offset == 0 else None
        return httpx.Response(200, json=_page(offset=offset, next_offset=next_offset))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = SGGovernmentRentalFlatsApiClient(
        base_url="https://rentals.test",
        api_key="secret",
        page_size=1,
        http=http,
    )

    records = list(SGGovernmentRentalFlatsApiConnector(client).fetch_records())

    assert [record["source_record_id"] for record in records] == [
        "rental_flat:1",
        "rental_flat:2",
    ]
    assert records[0]["observed_at"] == "2026-05-08T09:47:25.17797+00:00"
    assert records[0]["attributes"] == {
        "country_code": "SG",
        "postal_code": "681165",
        "block_no": "165A",
        "street_name": "Teck Whye Cres",
        "flat_type": "1-room & 2-room",
        "town_id": "9",
        "town_name": "Choa Chu Kang Town",
        "town_map_id": "choa_chu_kang",
        "town_map_zone": "CCK",
        "is_active": True,
    }
    second_attributes = records[1]["attributes"]
    assert isinstance(second_attributes, dict)
    assert second_attributes["is_active"] is False
    assert [request.headers["Authorization"] for request in requests] == [
        "Bearer secret",
        "Bearer secret",
    ]
    assert [request.url.params["offset"] for request in requests] == ["0", "1"]
    assert http.is_closed


def test_api_and_dump_modes_produce_identical_envelope_hashes(tmp_path: Path) -> None:
    dump = tmp_path / "rental.sql"
    _write_dump(dump)
    dump_record = list(SGGovernmentRentalFlatsConnector(dump).fetch_records())[0]
    api_page = _page(offset=0, next_offset=None)
    api_page["total"] = 1
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=api_page))
    )
    api_record = list(
        SGGovernmentRentalFlatsApiConnector(
            SGGovernmentRentalFlatsApiClient(
                base_url="https://rentals.test",
                api_key="secret",
                page_size=1,
                http=http,
            )
        ).fetch_records()
    )[0]

    assert api_record == dump_record
    assert api_record["record_hash"] == dump_record["record_hash"]
    assert dump_record["record_hash"] == "sha256:582b3d59af1acc6f"


def test_api_and_dump_modes_preserve_null_town_zone(tmp_path: Path) -> None:
    dump = tmp_path / "rental-null-zone.sql"
    _write_dump(dump, town_map_zone=None)
    dump_record = list(SGGovernmentRentalFlatsConnector(dump).fetch_records())[0]
    api_page = _page(offset=0, next_offset=None, town_map_zone=None)
    api_page["total"] = 1
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=api_page))
    )
    api_record = list(
        SGGovernmentRentalFlatsApiConnector(
            SGGovernmentRentalFlatsApiClient(
                base_url="https://rentals.test",
                api_key="secret",
                page_size=1,
                http=http,
            )
        ).fetch_records()
    )[0]

    assert api_record == dump_record
    raw_payload = api_record["raw_payload"]
    assert isinstance(raw_payload, dict)
    town = raw_payload["town"]
    assert isinstance(town, dict)
    assert town["map_zone"] is None


def test_api_and_dump_modes_preserve_legacy_copy_escape_decoding(tmp_path: Path) -> None:
    street_name = "Controls\b\f\v " + r"Literal\n\t\r Street"
    dump = tmp_path / "rental-escaped.sql"
    _write_dump(dump, street_name=street_name)
    dump_record = list(SGGovernmentRentalFlatsConnector(dump).fetch_records())[0]
    api_page = _page(offset=0, next_offset=None, street_name=street_name)
    api_page["total"] = 1
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=api_page))
    )
    api_record = list(
        SGGovernmentRentalFlatsApiConnector(
            SGGovernmentRentalFlatsApiClient(
                base_url="https://rentals.test",
                api_key="secret",
                page_size=1,
                http=http,
            )
        ).fetch_records()
    )[0]

    assert api_record == dump_record


def test_api_mode_dispatches_rental_flats_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.setattr("src.main.create_sgrentalflats_api_client", lambda: sentinel)

    connector = get_connector("sgrentalflats", mode="api")

    assert isinstance(connector, SGGovernmentRentalFlatsApiConnector)


@pytest.mark.parametrize(
    ("page", "message"),
    [
        (_page(offset=0, next_offset=None), "terminated before total"),
        (_page(offset=0, next_offset=2), "next offset"),
        (
            {
                "items": [],
                "total": 2,
                "limit": 1,
                "offset": 0,
                "next_offset": 1,
            },
            "empty page",
        ),
    ],
)
def test_api_client_rejects_incomplete_pagination(
    page: dict[str, object],
    message: str,
) -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=page))
    )
    client = SGGovernmentRentalFlatsApiClient(
        base_url="https://rentals.test",
        api_key="secret",
        page_size=1,
        http=http,
    )

    with pytest.raises(ValueError, match=message):
        list(client.iter_flats())

    client.close()


def test_api_connector_close_releases_client_before_iteration() -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    connector = SGGovernmentRentalFlatsApiConnector(
        SGGovernmentRentalFlatsApiClient(
            base_url="https://rentals.test",
            api_key="secret",
            page_size=1,
            http=http,
        )
    )

    connector.close()

    assert http.is_closed


def test_run_ingestion_does_not_build_connector_when_run_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The IngestRun is created before the connector is built (see run_ingestion),
    # so when run creation fails no connector — and therefore no HTTP client —
    # has been allocated. Asserting get_connector is never called verifies there
    # is nothing to leak on that path.
    class GraphClient:
        def verify_connectivity(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr("src.main.get_settings", lambda: object())
    monkeypatch.setattr("src.main.Neo4jClient", lambda _settings: GraphClient())
    monkeypatch.setattr("src.main.IngestPipeline", lambda _client, **_kwargs: object())

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("get_connector must not run when IngestRun creation fails")

    monkeypatch.setattr("src.main.get_connector", boom)

    def fail_create_run(*_args: object, **kwargs: object) -> str:
        assert kwargs["control_instance_id"] == "legacy-default"
        raise RuntimeError("run creation failed")

    monkeypatch.setattr("src.main._create_ingest_run", fail_create_run)

    with pytest.raises(RuntimeError, match="run creation failed"):
        run_ingestion("sgrentalflats", mode="api", initialize_graph=False)


def test_create_api_client_validates_settings_before_allocating_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        neo4j_password="secret",
        sgrentalflats_api_key=SecretStr(""),
    )
    allocations = 0

    def allocate_http(*_args: object, **_kwargs: object) -> httpx.Client:
        nonlocal allocations
        allocations += 1
        raise AssertionError("HTTP client must not be allocated")

    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    monkeypatch.setattr("src.main.httpx.Client", allocate_http)

    with pytest.raises(ValueError, match="API key"):
        create_sgrentalflats_api_client()

    assert allocations == 0
