"""SQLAlchemy Core table reflections for the SpeedZone POS DB.

Tables are declared with only the columns the connector actually reads.  The
schema mirrors the standard PHP POS (phppos) layout used across all outlets.

Customer custom-field mapping (from ``phppos_app_config``):

- ``custom_field_1_value`` → NRIC / Passport No.
- ``custom_field_2_value`` → bitrix_user_id
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

people = Table(
    "phppos_people",
    metadata,
    Column("person_id", Integer, primary_key=True),
    Column("first_name", String(255)),
    Column("last_name", String(255)),
    Column("full_name", Text),
    Column("phone_number", String(255)),
    Column("email", String(255)),
    Column("address_1", String(255)),
    Column("address_2", String(255)),
    Column("city", String(255)),
    Column("state", String(255)),
    Column("zip", String(255)),
    Column("country", String(255)),
    Column("comments", Text),
    Column("create_date", DateTime),
    Column("last_modified", DateTime),
    Column("title", String(255)),
    Column("phone_code", String(255)),
)

customers = Table(
    "phppos_customers",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("person_id", Integer, index=True),
    Column("account_number", String(255)),
    Column("company_name", String(255)),
    Column("deleted", Integer),
    # Custom fields — meanings configured in phppos_app_config:
    Column("custom_field_1_value", String(255)),  # NRIC / Passport No.
    Column("custom_field_2_value", String(255)),  # bitrix_user_id
)
