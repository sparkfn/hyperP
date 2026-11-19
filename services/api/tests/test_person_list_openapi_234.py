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
    machine_search_operation = schema["paths"]["/oauth2/v1/persons/search"]["get"]

    assert list_operation["operationId"] == "listPersons"
    assert machine_operation["operationId"] == "listPersonsMachine"
    assert search_operation["operationId"] == "searchPersons"
    assert machine_search_operation["operationId"] == "searchPersonsMachine"
    assert list_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ListedPersonListResponseEnvelope"
    }
    assert machine_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ListedPersonListResponseEnvelope"
    }
    assert search_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PersonSearchResponseEnvelope"
    }
    assert machine_search_operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/PersonSearchResponseEnvelope"}

    list_parameter_keys = [
        (parameter["in"], parameter["name"])
        for parameter in list_operation["parameters"]
        if "name" in parameter
    ]
    machine_parameter_keys = [
        (parameter["in"], parameter["name"])
        for parameter in machine_operation["parameters"]
        if "name" in parameter
    ]
    assert len(list_parameter_keys) == len(set(list_parameter_keys))
    assert len(machine_parameter_keys) == len(set(machine_parameter_keys))

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
    machine_search_parameter_keys = [
        (parameter["in"], parameter["name"])
        for parameter in machine_search_operation["parameters"]
        if "name" in parameter
    ]
    assert len(machine_search_parameter_keys) == len(set(machine_search_parameter_keys))
    assert {parameter["name"] for parameter in machine_search_operation["parameters"]} == {
        "identifier_type",
        "value",
        "q",
        "status",
        "cursor",
        "limit",
    }
    assert schema["components"]["schemas"]["PersonSearchResponseEnvelope"]["properties"]["data"][
        "items"
    ] == {"$ref": "#/components/schemas/Person"}

    profiles_list = schema["paths"]["/v1/profiles"]["get"]
    profiles_detail = schema["paths"]["/v1/profiles/{profile_id}"]["get"]
    assert profiles_list["operationId"] == "list_profiles"
    assert profiles_detail["operationId"] == "get_profile"
    assert "/oauth2/v1/profiles" not in schema["paths"]
    profile_identifier = schema["components"]["schemas"]["PersonIdentifier"]["properties"]
    assert "score" in profile_identifier
    assert "golden_rank" in profile_identifier
    for schema_name in ("SourceRecord", "SourceRecordView"):
        lifecycle = schema["components"]["schemas"][schema_name]["properties"]["lifecycle_status"]
        assert "parked" in lifecycle["enum"]


def test_static_openapi_profile_detail_composition_keeps_its_own_members() -> None:
    schema = yaml.safe_load(_STATIC_OPENAPI.read_text(encoding="utf-8"))
    profile_detail = schema["components"]["schemas"]["ProfileDetail"]

    effective_properties: dict[str, object] = {}
    for member in profile_detail["allOf"]:
        if "$ref" in member:
            base_name = member["$ref"].rsplit("/", 1)[-1]
            base = schema["components"]["schemas"][base_name]
            # A closed allOf base rejects the detail-only members below.
            assert base.get("additionalProperties") is not False
            effective_properties.update(base.get("properties", {}))
        else:
            assert member.get("additionalProperties") is not False
            effective_properties.update(member.get("properties", {}))
    assert {"source_records", "identifiers"} <= set(effective_properties)

    status_parameter = next(
        parameter
        for parameter in schema["paths"]["/v1/profiles"]["get"]["parameters"]
        if parameter.get("name") == "status"
    )
    assert status_parameter["schema"]["enum"] == ["open", "promoted", "retired", "all"]
    assert status_parameter["schema"]["default"] == "open"
    assert "all" in status_parameter["description"]


def test_static_openapi_documents_machine_exact_identifier_search() -> None:
    schema = yaml.safe_load(_STATIC_OPENAPI.read_text(encoding="utf-8"))
    operation = schema["paths"]["/oauth2/v1/persons/search"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    description = operation["description"]
    identifier_parameter = parameters["identifier_type"]

    assert identifier_parameter["schema"] == {"type": "string"}
    for identifier_type in (
        "phone",
        "email",
        "nric",
        "external_customer_id",
        "membership_id",
        "crm_contact_id",
        "loyalty_id",
        "custom",
    ):
        assert f"`{identifier_type}`" in identifier_parameter["description"]
        assert f"`{identifier_type}`" in description
    assert "nonblank" in description
    assert "together" in description
    assert "normalized" in description
    assert "updated_at" in description
    assert "429" in description


def test_static_openapi_person_search_envelope_allows_display_items() -> None:
    schema = yaml.safe_load(_STATIC_OPENAPI.read_text(encoding="utf-8"))
    envelope = schema["components"]["schemas"]["PersonSearchResponseEnvelope"]

    display_items = envelope["properties"]["display_items"]
    assert display_items["type"] == ["array", "null"]
    assert display_items["items"] == {"$ref": "#/components/schemas/PopoverDisplayItem"}
    assert envelope["additionalProperties"] is False


def test_static_openapi_human_search_accepts_arbitrary_identifier_keys() -> None:
    schema = yaml.safe_load(_STATIC_OPENAPI.read_text(encoding="utf-8"))
    operation = schema["paths"]["/v1/persons/search"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    identifier_schema = parameters["identifier_type"]["schema"]
    assert identifier_schema["type"] == "string"
    assert "$ref" not in identifier_schema
    assert "enum" not in identifier_schema
    assert "partner_account_id" in parameters["identifier_type"]["description"]
