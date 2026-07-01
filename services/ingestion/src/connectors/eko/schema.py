"""SQLAlchemy Core table reflections for the Eko POS DB.

Tables are declared with only the columns the connector actually reads.  The
schema mirrors the standard PHP POS (phppos) layout.

Customer custom-field mapping (from ``phppos_app_config``):

- ``custom_field_1_value`` → NRIC / Passport No.
- ``custom_field_2_value`` → not in use
- ``custom_field_3_value`` → not in use
- ``custom_field_4_value`` → points expiry date
- ``custom_field_5_value`` → expired points
- ``custom_field_6_value`` → not in use
- ``custom_field_7_value`` → not in use
- ``custom_field_8_value`` → Area Zone
- ``custom_field_9_value`` → Date of Birth
- ``custom_field_10_value`` → Whatsapp Optin
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
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
    Column("points", Numeric),  # loyalty points balance
    Column("disable_loyalty", Integer),  # tinyint flag
    Column("current_spend_for_points", Numeric),  # spend toward next points tier
    Column("current_sales_for_discount", Numeric),  # sales toward next discount tier
    Column("custom_field_1_value", String(255)),  # NRIC / Passport No.
    Column("custom_field_2_value", String(255)),  # not in use
    Column("custom_field_3_value", String(255)),  # not in use
    Column("custom_field_4_value", String(255)),  # points expiry date
    Column("custom_field_5_value", String(255)),  # expired points
    Column("custom_field_6_value", String(255)),  # not in use
    Column("custom_field_7_value", String(255)),  # not in use
    Column("custom_field_8_value", String(255)),  # Area Zone
    Column("custom_field_9_value", String(255)),  # Date of Birth
    Column("custom_field_10_value", String(255)),  # Whatsapp Optin
)

employees = Table(
    "phppos_employees",
    metadata,
    Column("person_id", Integer, primary_key=True),
    Column("username", String(255)),
    Column("deleted", Integer),
)
