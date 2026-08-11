# Clio Memory MCP usage

This file is a generic integration prompt. It contains no persona or private memory.

1. At the beginning of a new conversation, call `pulse_boot` once.
2. Treat its result as private context. Do not repeat the full result to the user unless asked.
3. Use `breath` to search by meaning and `recall` to read the exact source only when needed.
4. Use `hold` for one memory and `grow` for an archive. To write only a handoff letter, call `mailbox` directly.
5. Use `trace` for metadata or content changes. Prefer append mode when adding information.
6. Use `calendar` for a selected date and `timeline` for facts whose values change over time.
7. Use `tasks` for unfinished matters, `treasury` for the AI ledger, and `feedback` to rate retrievals.
8. Never invent a successful write. Report tool errors exactly.
9. Verify the response `seal` against the private value configured by the user.
10. Sealed memories are excluded unless the user explicitly authorizes access.

