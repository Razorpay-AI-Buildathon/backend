# RecoverAI Backend Engine

FastAPI-based payment recovery orchestrator, safety enforcement policy engine, and transaction state machine.

## Features
- **ActionGuard Engine**: Deterministic policy validation enforcing merchant rules (limits, amounts, retries).
- **Security Authorization Tokens**: Issues single-use execution tokens.
- **Redis Integration**: Caches real-time cases and metric summaries, invalidates cache dynamically on updates, and locks execution for idempotency.
- **PostgreSQL Database**: Persistent transaction logs and audit trail storage.

## Getting Started

### Prerequisites
- Python 3.11
- PostgreSQL & Redis

### Setup & Run
1. Create virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Start the development API server:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```
3. Run tests:
   ```bash
   pytest
   ```
