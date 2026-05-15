from __future__ import annotations

from sqlalchemy import create_engine, insert
from src.connectors.fundbox.base import FundboxConnectorBase
from src.connectors.fundbox.schema import merchant_staff, metadata, model_has_roles, roles


class _Connector(FundboxConnectorBase):
    def get_source_key(self) -> str:
        return "fundbox_consumer_backend"

    def build_records(self, conn: object) -> object:
        _ = conn
        return iter(())


def test_fetch_excluded_user_ids_includes_merchant_admin_and_staff() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    connector = _Connector()

    with engine.begin() as conn:
        conn.execute(
            insert(roles),
            [
                {"id": 1, "name": "consumer", "guard_name": "api"},
                {"id": 2, "name": "merchant", "guard_name": "api"},
                {"id": 3, "name": "admin", "guard_name": "api"},
            ],
        )
        conn.execute(
            insert(model_has_roles),
            [
                {"role_id": 1, "model_type": "User", "model_id": 10},
                {"role_id": 2, "model_type": "User", "model_id": 11},
                {"role_id": 3, "model_type": "User", "model_id": 12},
            ],
        )
        conn.execute(
            insert(merchant_staff),
            [{"id": 1, "user_id": 13, "merchant_id": 99, "name": "Staff"}],
        )

        excluded = connector._fetch_excluded_user_ids(conn, [10, 11, 12, 13, 14])

    assert excluded == {11, 12, 13}
