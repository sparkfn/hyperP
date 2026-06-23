from __future__ import annotations

import json
from collections.abc import Sequence

from pytest import MonkeyPatch
from src.llm import ChatMessage
from src.models import RawAddress
from src.normalizers import address as address_normalizer
from src.normalizers.address import normalize_raw_addresses


class _FakeLlmService:
    async def chat_json(self, messages: Sequence[ChatMessage]) -> str:
        _ = messages
        return json.dumps(
            {
                "addresses": [
                    {
                        "input_index": 0,
                        "unit_number": "#05-123",
                        "street_number": "10",
                        "street_name": "orchard road",
                        "building_name": "lucky plaza",
                        "city": "singapore",
                        "state_province": None,
                        "postal_code": "238863",
                        "country_code": "SG",
                        "normalized_full": (
                            "#05-123, 10 orchard road, lucky plaza, singapore 238863, sg"
                        ),
                    },
                    {
                        "input_index": 1,
                        "unit_number": None,
                        "street_number": "20",
                        "street_name": "second street",
                        "building_name": None,
                        "city": "singapore",
                        "state_province": None,
                        "postal_code": "654321",
                        "country_code": "SG",
                        "normalized_full": "20 second street, singapore 654321, sg",
                    },
                ]
            }
        )


class _DuplicateLlmService:
    async def chat_json(self, messages: Sequence[ChatMessage]) -> str:
        _ = messages
        return json.dumps(
            {
                "addresses": [
                    {
                        "input_index": 0,
                        "unit_number": "#04-242",
                        "street_number": "163A",
                        "street_name": "rivervale crescent",
                        "building_name": None,
                        "city": "singapore",
                        "state_province": None,
                        "postal_code": "541163",
                        "country_code": "SG",
                        "normalized_full": (
                            "#04-242, 163a rivervale crescent, singapore 541163, sg"
                        ),
                    },
                    {
                        "input_index": 0,
                        "unit_number": "#04-242",
                        "street_number": "",
                        "street_name": "rivervale crescent",
                        "building_name": "163A",
                        "city": "singapore",
                        "state_province": None,
                        "postal_code": "541163",
                        "country_code": "SG",
                        "normalized_full": (
                            "#04-242, 163a rivervale crescent, singapore 541163, sg"
                        ),
                    },
                ]
            }
        )


class _CaptureLlmService:
    def __init__(self) -> None:
        self.prompt: str | None = None

    async def chat_json(self, messages: Sequence[ChatMessage]) -> str:
        self.prompt = messages[-1].content
        return json.dumps({"addresses": []})


def test_normalize_raw_addresses_uses_llm_shape(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(address_normalizer, "get_address_llm_service", lambda: _FakeLlmService())

    results = normalize_raw_addresses(
        [
            RawAddress(raw="Lucky Plaza #05-123, 10 Orchard Road Singapore 238863"),
            RawAddress(raw="20 Second Street Singapore 654321"),
        ]
    )

    assert len(results) == 2
    first, first_flag = results[0]
    assert first_flag == "valid"
    assert first.unit_number == "#05-123"
    assert first.street_name == "orchard road"
    assert first.building_name == "lucky plaza"
    assert first.postal_code == "238863"
    second, _second_flag = results[1]
    assert second.street_number == "20"
    assert second.postal_code == "654321"


def test_normalize_raw_addresses_falls_back_when_llm_fails(monkeypatch: MonkeyPatch) -> None:
    def _raise() -> _FakeLlmService:
        raise RuntimeError("no llm")

    monkeypatch.setattr(address_normalizer, "get_address_llm_service", _raise)

    results = normalize_raw_addresses(
        [RawAddress(raw="#05-123 10 Example Street Singapore 123456")]
    )

    assert len(results) == 1
    address, flag = results[0]
    assert flag == "valid"
    assert address.unit_number == "05-123"
    assert address.street_number == "10"
    assert address.postal_code == "123456"


def test_normalize_raw_addresses_deduplicates_equivalent_llm_outputs(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        address_normalizer, "get_address_llm_service", lambda: _DuplicateLlmService()
    )

    results = normalize_raw_addresses(
        [RawAddress(raw="#04-242, 163A Rivervale Crescent Singapore 541163")]
    )

    assert len(results) == 1
    address, flag = results[0]
    assert flag == "valid"
    assert address.street_number == "163A"
    assert address.postal_code == "541163"


def test_normalize_raw_addresses_prefers_structured_fields_for_llm_prompt(
    monkeypatch: MonkeyPatch,
) -> None:
    service = _CaptureLlmService()
    monkeypatch.setattr(address_normalizer, "get_address_llm_service", lambda: service)

    results = normalize_raw_addresses(
        [
            RawAddress(
                raw="163A RIVERVALE CRESCENT, #04-242, RIVERVALE CRESCENT, "
                "163A 04 242, SINGAPORE, SINGAPORE, 541163, SINGAPORE",
                unit_number="#04-242",
                street_number="163A",
                street_name="RIVERVALE CRESCENT",
                city="SINGAPORE",
                postal_code="541163",
                country_code="SINGAPORE",
            )
        ]
    )

    assert len(results) == 1
    assert service.prompt is not None
    assert "163A, RIVERVALE CRESCENT" in service.prompt
    assert "04 242" not in service.prompt
