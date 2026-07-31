"""LLM prompts used by WhatsApp / Bitrix message extractors."""

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
BATCH_EXTRACTION_TEMPLATE = """\
Extract customer identity and transaction information from EACH numbered conversation below.
Return a JSON object {{"conversations": [ ... ]}} with one object per conversation (any order).

IMPORTANT: A line beginning with "[Deal]" is a CRM deal header that names the customer the
conversation is about, formatted "[Deal] <Customer Name> - <Business> ...". ALWAYS extract that
<Customer Name> as a possible_person (role primary_customer) even when the customer sent no
message and the rest of the conversation is only automated agent/template outreach. The text
before " - " in the deal header is the customer's name; the business name after it is NOT a
customer.

Each conversation object has:
- "conversation_index": the integer index shown before the conversation
- "persons": legacy array of customers, clients, prospects, or other external people whose
  identity should be attached to the customer profile. Prefer `possible_persons` for new
  grouped output. Do not include sales agents, staff, internal users, tenant or business
  representatives, or message senders acting on behalf of the business.
- "possible_persons": array of customers, clients, prospects, or secondary external people
  mentioned in the conversation. Group identifiers under the possible person they describe;
  never mix identifiers from two people in one object. Do not include sales agents, staff,
  internal users, tenant or business representatives, or message senders acting on behalf
  of the business. Each possible person has:
    - "name": full name if stated
    - "phone": phone number if stated (Singapore format like +65 or 8-digit local)
    - "email": email address if stated
    - "address": full address if stated
    - "nric": NRIC/FIN number if stated
    - "role": primary_customer | secondary_person | prospect | other if evident
    - "relationship_to_primary": relationship such as brother, wife, referrer if stated
    - "relationship_label": exact relationship phrase if stated; used as pending KNOWS evidence
    - "identifiers": strong identifiers for this possible person only
    - "weak_identifiers": weak/contextual identifiers for this possible person only
    - "evidence": short text explaining why these identifiers belong together
    - "confidence": confidence for this person grouping from 0.0 to 1.0
    - "notes": any other relevant context about this possible person
- "transactions": array of orders/invoices mentioned anywhere in the full conversation.
  Include order details stated by customers or business representatives. Each has:
    - "order_id": order/invoice reference number if stated
    - "product": product name or description if stated
    - "amount": numerical amount if stated
    - "currency": currency code (default SGD)
    - "status": status mentioned (e.g. pending, paid, completed, cancelled)
    - "notes": any other relevant context
- "chat_members": array of non-customer chat participants, agents, staff, senders, or
  business representatives. Do not use chat_members as customer identifiers. Each has:
    - "name": full name if stated
    - "phone": phone number if stated
    - "role": role in the conversation (e.g. agent, staff, sender, member)
    - "notes": relevant context about this chat member
- "inquiries": array of machines, products, or units the customer asked about. Each has:
    - "vehicle_product": vehicle/product model, product name, or description if stated
    - "unit": unit identifier, unit number, or stock/unit reference if stated
    - "lta_tag": LTA tag if stated
    - "serial_number": serial number if stated
    - "notes": any other relevant context
- "customer_sentiment": concise customer sentiment label or phrase if evident
- "tone": one overall customer-facing tone: "positive" | "neutral" | "negative" | "mixed" |
  "unknown". Classify the conversation as a whole; do not use this field for urgency or handling
  complexity. Use "unknown" when the messages do not support a reliable tone.
- "purpose": one primary conversation purpose: "product_inquiry" | "purchase_intent" |
  "order_management" | "support_request" | "complaint" | "appointment" | "follow_up" |
  "feedback" | "relationship_management" | "other" | "unknown". Choose the dominant purpose
  when several topics appear; use "unknown" when no primary purpose is reliably established.
- "outcome": the state at the end of the available transcript: "resolved" |
  "partially_resolved" | "pending_customer" | "pending_business" | "unresolved" |
  "no_action_required" | "unknown". Use "unknown" only when the transcript does not establish
  an end-state.
- "difficulty": one handling-complexity level: "low" | "medium" | "high" | "unknown". Use low
  for routine direct requests, medium for requests needing clarification or coordination, and high
  for complex, escalated, contentious, or materially blocked requests. Use "unknown" when the
  transcript does not support a reliable complexity assessment.
- "strong_identifiers": array of explicitly stated customer identity identifiers. Each has:
    - "type": "phone" | "email" | "government_id" | "source_customer_ref"
    - "value": exact extracted value
    - "label": optional source label such as nric, fin, customer_id
    - "person_name": associated customer name if stated
    - "confidence": confidence for this identifier from 0.0 to 1.0
    - "notes": short evidence context
- "weak_identifiers": array of contextual identifiers. Weak identifiers are evidence,
  not identity keys. Each has:
    - "type": "name" | "address" | "dob" | "vehicle_lta_tag" |
      "vehicle_serial_number" | "vehicle" | "product" | "order_ref" |
      "relationship" | "other"
    - "value": exact extracted value
    - "label": optional source label
    - "person_name": associated customer name if stated
    - "confidence": confidence for this value from 0.0 to 1.0
    - "notes": short evidence context
- "confidence": your overall confidence (0.0-1.0) in this extraction

Conversations (each prefixed with "=== Conversation N ==="; newest messages last):

{conversations}

Return one JSON object with the "conversations" array and nothing else.\
"""

#: System prompt for conversation summarization (separate model from extraction).
SUMMARY_SYSTEM = """\
You are a conversation summarizer for a customer profile unification platform.
Given message threads, write thorough, factual, sectioned summaries.
Only state facts explicitly present in the messages — do not guess or infer.
Output plain text only — never JSON or code fences (summaries are multi-line prose).\
"""

#: User prompt template for per-conversation summarization (plain-text, delimited).
#: A delimited text protocol — not JSON — because multi-line markdown prose breaks
#: JSON string escaping in models without a JSON mode.
BATCH_SUMMARY_TEMPLATE = """\
Summarize EACH numbered conversation below.
For EACH conversation, output a line exactly "=== Summary N ===" where N is that
conversation's integer index, followed by a thorough sectioned factual summary of
the full conversation. Use these section headings when evidence exists:
"Customer / Participants", "Identity Evidence", "Products / Vehicles",
"Orders / Commercial Terms", "Timeline / Follow-ups", "Uncertainties".

Output plain text only — no JSON, no code fences. Separate conversations with their
"=== Summary N ===" markers.

Conversations (each prefixed with "=== Conversation N ==="; newest messages last):

{conversations}\
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


def _number_conversations(texts: list[str]) -> str:
    """Prefix each conversation with its index for ``conversation_index`` keying."""
    blocks = [f"=== Conversation {index} ===\n{text}" for index, text in enumerate(texts)]
    return "\n\n".join(blocks)


def build_batch_extraction_prompt(texts: list[str]) -> str:
    """Build one user prompt extracting structured data from all conversations.

    Each conversation is prefixed with its index so the model can return one
    object per conversation keyed by ``conversation_index``.
    """
    return BATCH_EXTRACTION_TEMPLATE.format(conversations=_number_conversations(texts))


def build_batch_summary_prompt(texts: list[str]) -> str:
    """Build one user prompt summarizing all conversations in a batch."""
    return BATCH_SUMMARY_TEMPLATE.format(conversations=_number_conversations(texts))


def build_tenant_match_prompt(name: str | None, phone: str | None, tenants: list[str]) -> str:
    """Build the user prompt for tenant matching."""
    tenant_lines = "\n".join(f"- {t}" for t in tenants)
    return TENANT_MATCH_TEMPLATE.format(
        name=name or "(unknown)", phone=phone or "(unknown)", tenants=tenant_lines
    )
