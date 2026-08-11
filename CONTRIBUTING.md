# Contributing

Keep memory source text immutable unless a tool explicitly performs a user-confirmed edit. New background features should write to independent sidecars, fail without blocking core memory writes, hide sealed content by default, and provide a dry-run or confirmation step for destructive behavior.

Do not submit real memories, credentials, domains, local paths, databases, logs, or generated backups. Use synthetic fixtures only.

