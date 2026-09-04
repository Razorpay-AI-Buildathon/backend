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

## Workflow Architecture

The following sequence diagram illustrates how a failed payment event travels through the various microservices, AI layers, and deterministic guards.

```mermaid
sequenceDiagram
    participant Webhook as Payment Gateway
    participant API as Endpoints API
    participant DB as Postgres DB
    participant Redis as Redis Queue
    participant Engine as AI Strategy Council
    participant Guard as ActionGuard
    participant Worker as Execution Worker
    participant Human as Human Operator

    Webhook->>API: 1. FAILED_PAYMENT Event
    API->>DB: 2. Create RecoveryCase (IDENTIFIED)
    API->>Redis: 3. Enqueue 'evaluate_case'
    Redis->>Worker: 4. Dequeue task
    Worker->>Engine: 5. Request AI Strategy (Context, History)
    Engine-->>Worker: 6. Propose Action (e.g. RETRY_PAYMENT)
    Worker->>Guard: 7. Validate Constraints (Amount, Velocity)
    
    alt Action Guard Rejects
        Guard-->>Worker: 8a. DENIED
        Worker->>DB: 9a. Transition to HUMAN_REVIEW
        Human->>API: 10a. Operator Approves/Overrides
        API->>Redis: 11a. Enqueue 'execute_case'
    else Action Guard Approves
        Guard-->>Worker: 8b. APPROVED (Token)
        Worker->>Redis: 9b. Enqueue 'execute_case'
    end
    
    Redis->>Worker: 12. Dequeue 'execute_case'
    Worker->>Worker: 13. Execute Payment Simulation
    
    alt Execution Fails
        Worker->>DB: 14a. Increment Attempt, status FAILED
        Worker->>Redis: 15a. Schedule next attempt (Exponential Backoff)
        Note over Worker,DB: After Max Retries -> HUMAN_REVIEW
    else Execution Succeeds
        Worker->>DB: 14b. Transition to RECOVERED
    end
```

## State Machine

Every `RecoveryCase` follows a strict state machine. Below is the state transition diagram detailing the lifecycle of a single case from identification to resolution.

```mermaid
stateDiagram-v2
    [*] --> IDENTIFIED: Payment Failure Received
    
    IDENTIFIED --> ANALYZING: Enqueued for AI Evaluation
    
    state ANALYZING {
        direction LR
        AI_Evaluation --> ActionGuard
    }
    
    ANALYZING --> ACTION_PROPOSED: AI proposes strategy
    
    ACTION_PROPOSED --> GUARD_REVIEW: Policy rules applied
    
    GUARD_REVIEW --> APPROVED: Guard Authorizes
    GUARD_REVIEW --> HUMAN_REVIEW: Guard Blocks (Risk/Amount)
    
    HUMAN_REVIEW --> APPROVED: Operator Approves
    HUMAN_REVIEW --> CLOSED: Operator Rejects
    
    APPROVED --> EXECUTING: Sent to Gateway/Simulator
    
    EXECUTING --> RECOVERED: Payment Success
    EXECUTING --> FAILED: Payment Error
    
    FAILED --> ANALYZING: Attempt < Max Retries (Backoff Applied)
    FAILED --> HUMAN_REVIEW: Attempt >= Max Retries (Escalation)
    
    RECOVERED --> [*]
    CLOSED --> [*]
```
