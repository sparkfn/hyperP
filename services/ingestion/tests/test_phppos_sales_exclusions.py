from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, insert
from sqlalchemy.engine import Connection
from src.connectors.phppos_sales_common import fetch_phppos_sales


def test_fetch_phppos_sales_filters_excluded_customer_ids_in_sales_query() -> None:
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
                excluded_customer_ids={10},
            )
        )
    event.remove(engine, "before_cursor_execute", capture_sales_select)

    sales_selects = [
        statement for statement in statements if "ORDER BY phppos_sales.sale_id" in statement
    ]
    assert sales_selects
    assert "WHERE" in sales_selects[0]
    assert "customer_id" in sales_selects[0]
