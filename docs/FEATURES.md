# Features and MCP tools

## Memory

- `pulse_boot`: compact startup context with core index, latest flowing memory, mailbox/darkflow handoff, tasks, reminders, thoughts, behavior history, and response seal. Empty sections stay silent.
- `pulse`: system status and bucket listing.
- `breath`: semantic plus keyword retrieval, optional mood resonance, filters, and retrieval identifiers.
- `recall`: exact source reading with pagination, newest-first segments, and sealed-memory authorization.
- `hold`: store one memory without model-based source rewriting.
- `grow`: archive an event and optionally leave a separate mailbox message.
- `trace`: update metadata or content, append safely, seal/unseal, pin, resolve reminders, and preserve a write-before snapshot.
- `split_bucket`: copy selected marked sections into a child bucket while preserving source text.
- `cabinet`: browse the topic tree and its memory buckets.

## Continuity

- `mailbox`: write, search, edit, delete, paginate, and inspect message history.
- `calendar`: retrieve all supported records for any `YYYY-MM-DD` date.
- `timeline`: read or explicitly confirm dated versions of changing facts.
- Automatic fact detection: new writes can create review candidates, but never auto-confirm or overwrite facts.
- `tasks`: create, update, complete, cancel, delete, and search unfinished matters.
- Prospective memory: optional trigger dates surface due and overdue items.

## State and reflection

- `xinchao_status`: inspect the current computational state without clearing it.
- `inner_state`: inspect private thoughts, resonance, and tension.
- `heartbeat`: report an active conversation without adding narrative memory.
- State evaluation can use event text and expressed feeling; duplicate write events are deduplicated.
- Silence nudges are isolated from longer static/absence state cycles.
- Optional Bark actions can be acknowledged in the manager.

## Safety and maintenance

- `digest_preview`: produce a human-readable dry-run only; it never auto-merges, archives, or deletes.
- `feedback`: rate a retrieval as useful or irrelevant.
- `treasury`: AI-oriented income, expense, balance, and ledger history.
- Contradiction detection compares closely related memories and warns without editing either side.
- Sealed memories are omitted from default retrieval, counts, and relation building.
- History snapshots, response seals, diagnostics, health checks, encrypted exports, and permanent-delete previews are available.

## Web manager

The responsive manager supports memory editing, append mode, histories, topics, mailbox, semantic search, arbitrary-date calendar, fact timeline review, tasks, treasury, state views, thoughts, resonance, behavior acknowledgement, judge rules, exports, and health checks.

