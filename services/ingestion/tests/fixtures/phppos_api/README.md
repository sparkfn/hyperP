# Synthetic PHPPOS API fixtures

These fixtures are hand-written, synthetic payloads for the bounded PHPPOS
customer and sales adapter tests. They are not captured from a live POS tenant:
`customer_rows.json` and `sale_rows.json` are plausible `phppos_people` /
`phppos_customers` / `phppos_sales` shaped rows with fabricated names, NRICs,
contacts, and amounts.

They exist so the bounded contract, transport, resume, and writer tests can run
without live source credentials, and so the mapping helpers produce the same
envelopes as the direct-database and dump connectors.

Row identity matters: `person_id` for customers and `sale_id` for sales are the
`source_id` values the frozen bounded window reports for each change.
