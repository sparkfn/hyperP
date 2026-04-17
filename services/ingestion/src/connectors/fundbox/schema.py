"""SQLAlchemy Core table reflections for the Fundbox source DB.

Tables are declared with only the columns the connectors actually read. This
keeps the connector queries declarative and refactor-safe (column renames in
the source DB will surface as load-time errors instead of runtime KeyErrors).
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("email", String(255)),
    Column("mobile_number", String(255)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)

basic_profiles = Table(
    "basic_profiles",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger, index=True),
    Column("nric", String(255)),
    Column("full_name", String(255)),
    Column("date_of_birth", Date),
    Column("gender", String(255)),
    Column("nationality", String(255)),
    Column("email", String(255)),
    Column("mobile_number", String(255)),
)

basic_plus_profiles = Table(
    "basic_plus_profiles",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger, index=True),
    Column("whatsapp_phone", String(255)),
    Column("facebook_id", String(255)),
)

addresses = Table(
    "addresses",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger, index=True),
    Column("address_line_1", String(255)),
    Column("address_line_2", String(255)),
    Column("street", String(255)),
    Column("block", String(255)),
    Column("building", String(255)),
    Column("unit", String(255)),
    Column("floor", String(255)),
    Column("city", String(255)),
    Column("country", String(255)),
    Column("postal_code", String(255)),
    Column("address_type", String(255)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)

social_accounts = Table(
    "social_accounts",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger, index=True),
    Column("provider", String(255)),
    Column("provider_id", String(255)),
)

device_ids = Table(
    "device_ids",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger, index=True),
    Column("device_id", String(255)),
)

last_logins = Table(
    "last_logins",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger, index=True),
    Column("last_logged_in", DateTime),
)

contacts = Table(
    "contacts",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger, index=True),
    Column("full_name", String(255)),
    Column("mobile_number", String(255)),
    Column("relationship", String(255)),
    Column("status", String(255)),
    Column("approved_at", DateTime),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)

log_legacy_profiles = Table(
    "log_legacy_profiles",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", Integer, index=True),
    Column("nric", String(255)),
    Column("full_name", String(255)),
    Column("date_of_birth", Date),
    Column("gender", String(255)),
    Column("nationality", String(255)),
    Column("email", String(255)),
    Column("mobile_number", String(255)),
    Column("whatsapp_phone", String(255)),
    Column("facebook_id", String(255)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)

log_legacy_profile_addresses = Table(
    "log_legacy_profile_addresses",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", Integer, index=True),
    Column("address_line_1", String(255)),
    Column("address_line_2", String(255)),
    Column("street", String(255)),
    Column("block", String(255)),
    Column("building", String(255)),
    Column("unit", String(255)),
    Column("floor", String(255)),
    Column("city", String(255)),
    Column("country", String(255)),
    Column("postal_code", String(255)),
)

orders = Table(
    "orders",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("user_id", BigInteger, index=True),
    Column("merchant_id", BigInteger, index=True),
    Column("merchant_staff_id", BigInteger),
    Column("order_no", String(255)),
    Column("total_amount", Numeric(10, 2)),
    Column("total_items", Integer),
    Column("transaction_reference", String(255)),
    Column("release_date", Date),
    Column("status", String(255), index=True),
    Column("expiry_at", String(255)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
    Column("deleted_at", DateTime),
)

order_items = Table(
    "order_items",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("order_id", BigInteger, index=True),
    Column("merchant_product_id", BigInteger, index=True),
    Column("quantity", Integer),
    Column("price", Numeric(10, 2)),
    Column("lta_tag", String(255)),
    Column("serial_no", String(255)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)

merchant_products = Table(
    "merchant_products",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("merchant_id", BigInteger, index=True),
    Column("product_variant_id", BigInteger, index=True),
    Column("price", Numeric(10, 2)),
)

product_variants = Table(
    "product_variants",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("product_id", BigInteger, index=True),
    Column("sku", String(255)),
    Column("name", String(255)),
    Column("price", Numeric(10, 2)),
    Column("attributes", JSON),
    Column("active", Integer),
)

products = Table(
    "products",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("product_id", String(255)),
    Column("name", String(255)),
    Column("type", String(255)),
    Column("sub_type", String(255)),
    Column("category", String(255)),
    Column("sub_category", String(255)),
    Column("description", Text),
    Column("make", String(255)),
    Column("model", String(255)),
    Column("active", Integer),
)

merchants = Table(
    "merchants",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("name", String(255)),
    Column("official_name", String(255)),
)

merged_users = Table(
    "merged_users",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("email", String(255)),
    Column("mobile_number", String(255)),
    Column("nric", String(255)),
    Column("new_user_id", BigInteger),
    Column("new_email", String(255)),
    Column("new_mobile_number", String(255)),
    Column("new_nric", String(255)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)
