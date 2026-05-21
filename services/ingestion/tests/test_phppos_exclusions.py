from __future__ import annotations

from sqlalchemy import create_engine, insert
from src.connectors.eko.connector import EkoConnector
from src.connectors.eko.schema import employees as eko_employees
from src.connectors.eko.schema import metadata as eko_metadata
from src.connectors.eko.schema import people as eko_people
from src.connectors.speedzone.connector import SpeedZoneConnector
from src.connectors.speedzone.schema import employees as speedzone_employees
from src.connectors.speedzone.schema import metadata as speedzone_metadata
from src.connectors.speedzone.schema import people as speedzone_people


def test_eko_people_only_skips_employee_person_ids() -> None:
    engine = create_engine("sqlite:///:memory:")
    eko_metadata.create_all(engine)
    connector = EkoConnector()

    with engine.begin() as conn:
        conn.execute(
            insert(eko_people),
            [
                {"person_id": 1, "full_name": "Customer", "phone_number": "88889999"},
                {"person_id": 2, "full_name": "Employee", "phone_number": "68505434"},
            ],
        )
        conn.execute(insert(eko_employees), [{"person_id": 2, "username": "staff"}])
        excluded = connector._fetch_employee_person_ids(conn, {"phppos_employees"})
        records = list(connector._build_records_people_only(conn, 100, excluded))

    assert [record["source_record_id"] for record in records] == ["eko_phppos-person-1"]


def test_speedzone_people_only_skips_employee_person_ids() -> None:
    engine = create_engine("sqlite:///:memory:")
    speedzone_metadata.create_all(engine)
    connector = SpeedZoneConnector()

    with engine.begin() as conn:
        conn.execute(
            insert(speedzone_people),
            [
                {"person_id": 1, "full_name": "Customer", "phone_number": "88889999"},
                {"person_id": 2, "full_name": "Employee", "phone_number": "68505434"},
            ],
        )
        conn.execute(insert(speedzone_employees), [{"person_id": 2, "username": "staff"}])
        excluded = connector._fetch_employee_person_ids(conn, {"phppos_employees"})
        records = list(connector._build_records_people_only(conn, 100, excluded))

    assert [record["source_record_id"] for record in records] == ["speedzone_phppos-person-1"]
