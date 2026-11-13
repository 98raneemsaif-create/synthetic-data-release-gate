# Planned scenarios

- Passing batch: all hard checks and measured risk checks pass.
- Broken FK: `order_items.order_id` points to a missing order.
- Critical near-copy: artificially inject nearly identical entity-level rows in a controlled test fixture.
- Error injection: a checker throws; the gate must report ERROR and withhold publication.
- Policy change: same cached metrics, new policy hash, changed decision.

Use only demonstrative synthetic input; never upload actual personal data to a training account.
