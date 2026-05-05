"""LLM prompts used by WhatsApp / Bitrix message extractors."""

from __future__ import annotations

#: System prompt for conversation extraction.
EXTRACTION_SYSTEM = """\
You are a data extraction specialist for a customer profile unification platform.
Given a message thread, extract structured identity and transaction data in JSON format.
Only extract information that is explicitly present in the messages.
Do NOT guess or infer information not stated.
Be conservative — when uncertain, leave the field empty rather than guessing.
All phone numbers are in Singapore format unless stated otherwise.
Always output valid JSON.\
"""

#: User prompt template for identity + transaction extraction.
EXTRACTION_TEMPLATE = """\
Extract from the following conversation all identity and transaction information.
Return a JSON object with these top-level keys:
- "persons": array of persons mentioned. Each person has:
    - "name": full name if stated
    - "phone": phone number if stated (Singapore format like +65 or 8-digit local)
    - "email": email address if stated
    - "address": full address if stated
    - "nric": NRIC/FIN number if stated
    - "notes": any other relevant context about this person
- "transactions": array of orders/invoices mentioned. Each has:
    - "order_id": order/invoice reference number if stated
    - "product": product name or description if stated
    - "amount": numerical amount if stated
    - "currency": currency code (default SGD)
    - "status": status mentioned (e.g. pending, paid, completed, cancelled)
    - "notes": any other relevant context
- "confidence": your overall confidence (0.0-1.0) in this extraction

Conversation (newest messages last):

{messages}

Return only valid JSON.\
"""

#: Template for confirming a person is a known tenant.
TENANT_MATCH_TEMPLATE = """\
A conversation mentions the following person:

Name: {name}
Phone: {phone}

Does this person match any of these known tenants?
Answer YES only if the name or phone matches exactly.
Known tenants:
{tenants}

Answer in JSON: {{"is_match": true/false, "matched_tenant_key":
"fundbox"|"eko"|"speedzone"|null, "confidence": 0.0-1.0}}
"""


def build_extraction_prompt(messages: str) -> str:
    """Build the user prompt for identity/transaction extraction."""
    return EXTRACTION_TEMPLATE.format(messages=messages)


def build_tenant_match_prompt(name: str | None, phone: str | None, tenants: list[str]) -> str:
    """Build the user prompt for tenant matching."""
    tenant_lines = "\n".join(f"- {t}" for t in tenants)
    return TENANT_MATCH_TEMPLATE.format(
        name=name or "(unknown)", phone=phone or "(unknown)", tenants=tenant_lines
    )
