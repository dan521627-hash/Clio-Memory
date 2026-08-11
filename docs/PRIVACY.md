# Privacy and security

## Never commit

- `.env` or `config.yaml`
- API keys, access tokens, response seals, manager passwords, push device keys
- domains, tunnel credentials, SSH keys, server addresses, or private paths
- `data/`, `models/`, `exports/`, databases, logs, diagnostics, or backups
- personalized prompts, real names, relationship details, or screenshots of private memory

## Default boundary

The provided Compose file binds MCP and manager ports to `127.0.0.1`. This is intentional. Do not change it to `0.0.0.0` without authentication, TLS, firewall rules, and a clear threat model.

The manager password protects browser access but is not a substitute for a secure network boundary. The response seal helps a client detect missing or unexpected tool responses; it is not encryption.

An external model provider can receive text sent for optional summaries or evaluation. Review the provider's data policy before configuring a key. Leave the key empty for local fallback behavior.

Before publishing a fork, run a secret scanner and inspect the complete Git diff. Git history can retain files even after deletion.

