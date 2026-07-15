"""SourceConnector adapters for source-shaped PHPPOS API pages."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast

from sqlalchemy.engine import RowMapping

from src.connectors.base import SourceConnector
from src.connectors.eko.connector import EkoConnector
from src.connectors.phppos_api.models import CustomerRow, SaleRow
from src.connectors.phppos_sales_common import _build_envelope
from src.connectors.speedzone.connector import SpeedZoneConnector
from src.models import JsonValue


class ApiClient(Protocol):
    def iter_customers(self) -> Iterator[CustomerRow]: ...
    def iter_sales(self) -> Iterator[SaleRow]: ...
    def close(self) -> None: ...


class ApiRow:
    """Attribute and mapping facade matching SQLAlchemy Row's mapper surface."""

    def __init__(self, values: Mapping[str, object]) -> None:
        self._mapping = dict(values)

    def __getattr__(self, name: str) -> object:
        try:
            return self._mapping[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _CustomerApiConnector(SourceConnector):
    source_key: str

    def __init__(self, client: ApiClient) -> None:
        self._client = client

    def get_source_key(self) -> str:
        return self.source_key

    def close(self) -> None:
        self._client.close()

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        try:
            for row in self._client.iter_customers():
                values = row.model_dump()
                for field in _OPTIONAL_CUSTOMER_FIELDS:
                    values.setdefault(field, None)
                api_row = ApiRow(values)
                if self.source_key == "eko_phppos":
                    yield EkoConnector._build_one(api_row)
                else:
                    yield SpeedZoneConnector._build_envelope_with_customer(api_row)
        finally:
            self._client.close()


class EkoApiConnector(_CustomerApiConnector):
    source_key = "eko_phppos"


class SpeedZoneApiConnector(_CustomerApiConnector):
    source_key = "speedzone_phppos"


class _SalesApiConnector(SourceConnector):
    source_key: str

    def __init__(self, client: ApiClient) -> None:
        self._client = client

    def get_source_key(self) -> str:
        return f"{self.source_key}:sales"

    def close(self) -> None:
        self._client.close()

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        try:
            for row in self._client.iter_sales():
                yield self._build_record(row)
        finally:
            self._client.close()

    def _build_record(self, row: SaleRow) -> dict[str, JsonValue]:
        values = row.model_dump()
        _coerce_decimal_fields(values, {"subtotal", "total", "tax", "profit"})
        line_values = values.pop("lines")
        assert isinstance(line_values, list)
        lines = [line for line in line_values if isinstance(line, dict)]
        for line in lines:
            _coerce_decimal_fields(
                line,
                {"item_unit_price", "quantity_purchased", "discount", "cost_price"},
            )
        items: dict[int, Mapping[str, object]] = {}
        categories: dict[int, str] = {}
        for line in lines:
            item_id = _optional_int(line.get("item_id"))
            if item_id is not None:
                items[item_id] = {
                    "item_id": item_id,
                    "name": line.get("item_name"),
                    "item_number": line.get("item_number"),
                    "product_id": line.get("product_id"),
                    "category": line.get("category_id"),
                }
            category_id = _optional_int(line.get("category_id"))
            category_name = line.get("category_name")
            if category_id is not None and isinstance(category_name, str):
                categories[category_id] = category_name
        customer = {
            "custom_field_1_value": values.get("customer_nric"),
            "custom_field_8_value": values.get("customer_custom_field_8"),
            "custom_field_10_value": values.get("customer_custom_field_10"),
        }
        person = {
            "email": values.get("customer_email"),
            "phone_number": values.get("customer_phone"),
        }
        return _build_envelope(
            sale=cast(RowMapping, values),
            line_rows=cast(list[RowMapping], lines),
            items_by_id=cast(dict[int, RowMapping], items),
            sales_cols=set(values),
            items_cols={key for line in lines for key in line},
            item_cols={key for item in items.values() for key in item},
            source_system_key=self.source_key,
            categories=categories,
            customer_row=cast(RowMapping, customer),
            people_row=cast(RowMapping, person),
            extract_bike_plate=self.source_key == "speedzone_phppos",
        )


class EkoSalesApiConnector(_SalesApiConnector):
    source_key = "eko_phppos"


class SpeedZoneSalesApiConnector(_SalesApiConnector):
    source_key = "speedzone_phppos"


_OPTIONAL_CUSTOMER_FIELDS = frozenset(
    {
        "custom_field_1_value",
        "custom_field_2_value",
        "custom_field_9_value",
        "email",
        "phone_number",
        "phone_code",
        "country",
        "full_name",
        "last_modified",
        "create_date",
    }
)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_decimal_fields(values: dict[str, object], fields: set[str]) -> None:
    for field in fields:
        value = values.get(field)
        if not isinstance(value, str):
            continue
        try:
            values[field] = Decimal(value)
        except InvalidOperation:
            continue
