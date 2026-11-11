from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, insert
from sqlalchemy.engine import Connection, Engine
from src.connectors.phppos_sales_common import fetch_phppos_sales
from src.ingest_skips import ConnectorSkip


def _build_engine() -> tuple[Engine, Table]:
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    sales = Table(
        "phppos_sales",
        metadata,
        Column("sale_id", Integer, primary_key=True),
        Column("customer_id", Integer),
    )
    Table("phppos_sales_items", metadata, Column("sale_id", Integer), Column("item_id", Integer))
    Table(
        "phppos_items",
        metadata,
        Column("item_id", Integer, primary_key=True),
        Column("name", String(255)),
    )
    metadata.create_all(engine)
    return engine, sales


def test_fetch_phppos_sales_does_not_filter_customer_id_in_sql() -> None:
    """Employee exclusion is a Python backstop, so the sales query stays unfiltered."""
    engine, sales = _build_engine()
    statements: list[str] = []

    def capture_sales_select(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        _ = conn, cursor, parameters, context, executemany
        if "FROM phppos_sales" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_sales_select)
    with engine.begin() as conn:
        conn.execute(
            insert(sales),
            [
                {"sale_id": 1, "customer_id": 10},
                {"sale_id": 2, "customer_id": 20},
            ],
        )
        list(
            fetch_phppos_sales(
                engine=engine,
                conn=conn,
                source_system_key="eko_phppos",
                chunk_size=100,
                excluded_person_ids={10},
            )
        )
    event.remove(engine, "before_cursor_execute", capture_sales_select)

    sales_selects = [
        statement for statement in statements if "ORDER BY phppos_sales.sale_id" in statement
    ]
    assert sales_selects
    assert "WHERE" not in sales_selects[0]


def test_fetch_phppos_sales_ledgers_employee_sale_and_keeps_other_sales() -> None:
    engine, sales = _build_engine()
    skips: list[ConnectorSkip] = []
    with engine.begin() as conn:
        conn.execute(
            insert(sales),
            [
                {"sale_id": 1, "customer_id": 10},
                {"sale_id": 2, "customer_id": 20},
            ],
        )
        records = list(
            fetch_phppos_sales(
                engine=engine,
                conn=conn,
                source_system_key="eko_phppos",
                chunk_size=100,
                excluded_person_ids={10},
                on_skip=skips.append,
            )
        )
    assert [record["source_record_id"] for record in records] == ["eko_phppos-sale-2"]
    assert [(skip.reason, skip.source_record_id, skip.locator) for skip in skips] == [
        ("employee_sale", "eko_phppos-sale-1", "phppos_sales#sale_id=1")
    ]


def test_fetch_phppos_sales_ledgers_null_customer_sale_when_employees_exist() -> None:
    engine, sales = _build_engine()
    skips: list[ConnectorSkip] = []
    with engine.begin() as conn:
        conn.execute(
            insert(sales),
            [
                {"sale_id": 1, "customer_id": None},
                {"sale_id": 2, "customer_id": 20},
            ],
        )
        records = list(
            fetch_phppos_sales(
                engine=engine,
                conn=conn,
                source_system_key="eko_phppos",
                chunk_size=100,
                excluded_person_ids={10},
                on_skip=skips.append,
            )
        )
    assert [record["source_record_id"] for record in records] == ["eko_phppos-sale-2"]
    assert [(skip.reason, skip.source_record_id, skip.locator) for skip in skips] == [
        ("missing_customer_id", "eko_phppos-sale-1", "phppos_sales#sale_id=1")
    ]


def test_fetch_phppos_sales_emits_null_customer_sale_when_no_employees_exist() -> None:
    engine, sales = _build_engine()
    skips: list[ConnectorSkip] = []
    with engine.begin() as conn:
        conn.execute(
            insert(sales),
            [
                {"sale_id": 1, "customer_id": None},
                {"sale_id": 2, "customer_id": 20},
            ],
        )
        records = list(
            fetch_phppos_sales(
                engine=engine,
                conn=conn,
                source_system_key="eko_phppos",
                chunk_size=100,
                excluded_person_ids=set(),
                on_skip=skips.append,
            )
        )
    assert [record["source_record_id"] for record in records] == [
        "eko_phppos-sale-1",
        "eko_phppos-sale-2",
    ]
    assert skips == []
