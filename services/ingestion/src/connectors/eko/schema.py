"""SQLAlchemy Core table reflections for the Eko POS DB.

Tables are declared with only the columns the connector actually reads.  The
schema mirrors the standard PHP POS (phppos) layout.

Customer custom-field mapping (from ``phppos_app_config``):

- ``custom_field_1_value`` → NRIC / Passport No.
- ``custom_field_4_value`` → bitrix_user_id (numeric)
- ``custom_field_5_value`` → external customer ID (numeric)
- ``custom_field_8_value`` → region (Central, East, North, etc.)
- ``custom_field_9_value`` → DOB epoch (negative = pre-1970)
- ``custom_field_10_value`` → opt-in status or DOB string
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
    Column("custom_field_1_value", String(255)),  # NRIC / Passport No.
    Column("custom_field_4_value", String(255)),  # bitrix_user_id
    Column("custom_field_5_value", String(255)),  # external customer ID
    Column("custom_field_8_value", String(255)),  # region
    Column("custom_field_9_value", String(255)),  # DOB epoch
    Column("custom_field_10_value", String(255)),  # opt-in status / DOB string
)
