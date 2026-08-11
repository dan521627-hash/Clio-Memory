# Clio Memory

Clio Memory is a self-hosted MCP memory and state system for long-running AI relationships and assistants. It stores source text locally, exposes explicit memory tools, and provides a warm web manager for humans.

`Clio` is named after the Muse of history in Greek mythology: not because memory should trap a person in the past, but because continuity gives later conversations somewhere real to begin.

> This repository is a privacy-clean source shell. It contains no user memory, API key, domain, tunnel credential, manager password, push key, database, log, backup, or private persona.

## What it includes

- Semantic and keyword hybrid retrieval with local `bge-small-zh-v1.5` embeddings
- Startup memory index with fixed and flowing layers
- Exact source recall with pagination and newest-first reading
- Append-only memory writes, write-before snapshots, history retention, and safe split tools
- Independent mailbox with search, edit, delete, history, and retention
- Feeling memories and V/A mood resonance
- Prospective reminders and date-based memory calendar
- Fact timeline with automatic candidate detection and human confirmation
- Contradiction warnings without silent overwrite
- Sealed memory, pin levels, topic cabinet, relations, and retrieval feedback
- Unfinished matters, AI treasury, private thoughts, resonance, tension, and state causality
- Silence/static state engine, optional Bark actions, and a private editable judge configuration
- Dry-run digestion planning that never archives or merges automatically
- Authenticated responsive web manager and encrypted exports
- Response seal and request diagnostics

See [Features](docs/FEATURES.md) for the complete tool list.

## Quick start

Requirements: Docker Desktop on Windows, or Docker Engine with Compose on Linux.

### Windows

```powershell
git clone https://github.com/dan521627-hash/Clio-Memory.git
cd Clio-Memory
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

### Linux / VPS

```bash
git clone https://github.com/dan521627-hash/Clio-Memory.git
cd Clio-Memory
chmod +x start.sh
./start.sh
```

The first start creates a private `.env`, an editable `config.yaml`, and empty local data directories.

- MCP endpoint: `http://127.0.0.1:18001/mcp`
- Web manager: `http://127.0.0.1:8787`
- Health check: `http://127.0.0.1:18001/health`

The default Compose file binds both ports to localhost. It does **not** create a public tunnel or fixed domain. Public access requires the deployer's own HTTPS reverse proxy and security configuration.

## Optional model and push services

The service can run without an API key. When an OpenAI-compatible API is configured, it can improve summaries, emotion evaluation, task extraction, topic suggestions, and fact-change candidates. Put the deployer's own key in `.env`; never paste it into source files.

Bark delivery is optional and disabled until the deployer supplies a device key and changes behavior mode from rehearsal after testing.

## Data ownership

Runtime data lives under `./data`, model files under `./models`, and exports under `./exports`. These directories are ignored by Git. Back them up separately. Never publish them with a fork.

Before exposing the manager or MCP endpoint outside localhost, read [Privacy and security](docs/PRIVACY.md).

## Project status

This is an experimental personal-memory system, not a medical device or a claim that language models possess biological hormones or human consciousness. State names are computational metaphors used to create continuity and inspectable behavior.

## Attribution and license

Clio Memory is based on [P0lar1zzZ/Ombre-Brain](https://github.com/P0lar1zzZ/Ombre-Brain), released under the MIT License. The original license is preserved in [LICENSE](LICENSE). Modifications remain available under the same license.

