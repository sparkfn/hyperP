# Chat Customer-Only Ingestion Design

## Problem

Chat ingestion can extract sales-agent identity facts alongside customer facts. When an agent such as Tonni appears in the extracted `persons` list, downstream ingestion treats that person as customer identity data. This can attach agent identifiers to the customer and overwrite the customer profile name with the agent's full name.

## Goal

Conversation source records must preserve the full conversation context, but every normalized customer identity attribute and identifier produced from chat ingestion must belong only to the customer/client/prospect.

## Extraction contract

Update the chat LLM extraction prompt so `persons` means only external customers, clients, prospects, or other people whose identity should be attached to the customer profile.

The prompt must explicitly exclude from `persons`:

- sales agents
- staff
- internal users
- tenant or business representatives
- message senders acting on behalf of the business

The prompt must also state that `transactions` and `summary` are conversation-level outputs. They should still use the full conversation, including agent-written messages, because those messages often contain order details, follow-up state, and customer intent context.

## Connector behavior

Keep the Bitrix and WhatsApp connector flow mostly unchanged:

- call the shared chat extraction helper
- derive normalized identifiers from `extraction["persons"]`
- derive normalized `attributes["full_name"]` from the first extracted person
- preserve conversation metadata in raw payload

This remains safe because the extraction contract makes `persons` customer-only.

Bitrix agent metadata remains preserved in `raw_payload["chat_members"]`. WhatsApp participants, endpoints, and message text remain preserved in raw payload. Agent facts can remain source metadata, but must never become normalized customer identity data.

## Customer-only normalized identity rule

All normalized identity fields derived from chat ingestion must be customer-only, including:

- `attributes["full_name"]`
- phone identifiers
- email identifiers
- NRIC/FIN identifiers
- addresses
- any future normalized profile attribute derived from extracted persons

Agent, staff, and internal-user facts may remain in raw payload metadata such as Bitrix `chat_members`, WhatsApp `participants`, `message_endpoints`, and conversation text. They must not be emitted as normalized identifiers or profile attributes.

## Regression coverage

Add ingestion tests for a conversation where sales agent Tonni appears in the message text or metadata and a distinct customer is also present.

Tests should verify:

- normalized `attributes["full_name"]` is the customer, not Tonni
- normalized phone/email identifiers come only from the customer
- no normalized identity field contains agent/staff/internal-user facts
- raw payload still preserves agent metadata
- summary and transactions still preserve order details from the full conversation
- the extraction prompt explicitly excludes agents/staff/internal users from `persons`
- the extraction prompt explicitly allows transactions and summary to use the full conversation

## Scope

This change should not add role-aware parser fields or post-extraction known-agent filtering. Those are future options if prompt-contract regression tests show the LLM still includes agents in `persons` despite the stronger instruction.
