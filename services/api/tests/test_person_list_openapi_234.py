from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from src.app import build_app

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STATIC_OPENAPI = _REPO_ROOT / "docs" / "profile-unifier-openapi-3.1.yaml"


def test_person_list_openapi_exposes_typed_crm_deal_controls() -> None:
    schema = TestClient(build_app()).get("/app/v2/openapi.json").json()
    operation = schema["paths"]["/persons"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert parameters["crm_deal_count_min"]["schema"]["anyOf"][0]["minimum"] == 0
    assert parameters["crm_deal_count_max"]["schema"]["anyOf"][0]["minimum"] == 0
    sort_schema = parameters["sort_by"]["schema"]["anyOf"][0]
    assert "crm_deal_count" in sort_schema["enum"]


def test_operational_search_openapi_keeps_only_search_parameters() -> None:
    schema = TestClient(build_app()).get("/app/v2/openapi.json").json()
    operation = schema["paths"]["/persons/search"]["get"]

    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "identifier_type",
        "value",
        "q",
        "status",
        "cursor",
        "limit",
    }



def test_static_openapi_preserves_the_person_endpoint_matrix() -> None:
    schema = yaml.safe_load(_STATIC_OPENAPI.read_text(encoding="utf-8"))
    list_operation = schema["paths"]["/v1/persons"]["get"]
    machine_operation = schema["paths"]["/oauth2/v1/persons"]["get"]
    search_operation = schema["paths"]["/v1/persons/search"]["get"]

    assert list_operation["operationId"] == "listPersons"
    assert machine_operation["operationId"] == "listPersonsMachine"
    assert search_operation["operationId"] == "searchPersons"
    assert list_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ListedPersonListResponseEnvelope"
    }
    assert machine_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ListedPersonListResponseEnvelope"
    }
    assert search_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PersonSearchResponseEnvelope"
    }

    list_parameters = {
        parameter["name"]: parameter
        for parameter in list_operation["parameters"]
        if "name" in parameter
    }
    machine_parameters = {
        parameter["name"]: parameter
        for parameter in machine_operation["parameters"]
        if "name" in parameter
    }
    assert list_parameters == machine_parameters
    assert list_parameters["crm_deal_count_max"]["schema"]["minimum"] == 0
    assert "crm_deal_count" in list_parameters["sort_by"]["schema"]["enum"]

    assert {parameter["name"] for parameter in search_operation["parameters"]} == {
        "identifier_type",
        "value",
        "q",
        "status",
        "cursor",
        "limit",
    }
    assert schema["components"]["schemas"]["PersonSearchResponseEnvelope"]["properties"][
        "data"
    ]["items"] == {"$ref": "#/components/schemas/Person"}
