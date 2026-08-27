from fastapi import APIRouter, Depends, HTTPException, Query, status, Header
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any
import os
import json
import uuid
import secrets
from pathlib import Path
from datetime import datetime, timezone

from app.db.session import get_db
from app.models.case import (
    RecoveryCase,
    PaymentEvent,
    RecoveryAction,
    CaseStatus,
    ActionType,
    ActionState,
    CaseStateMachine,
)
from app.schemas.api import (
    ScoreRequest,
    ScoreResponse,
    ActionGuardRequest,
    ActionGuardResponse,
    ExecuteRequest,
    ExecuteResponse,
    CaseSummary,
    CaseListResponse,
    CaseDetail,
    MetricsResponse,
    ActionTypeEnum,
)
from app.services.scoring import (
    calculate_erv,
    calculate_priority_score,
    calculate_recoverability_probability,
)
from app.services.action_guard import ActionGuard
from app.services.executor import ExecutionSimulator
from app.services.evaluator import BatchEvaluator
from app.services.redis_cache import RedisCache

router = APIRouter()

import hmac


# API Authentication dependency
def verify_api_key(x_api_key: Optional[str] = Header(None)):
    expected_key = os.getenv("RECOVERAI_API_KEY")
    if not expected_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="RECOVERAI_API_KEY environment variable is not configured on the server.",
        )
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API Key validation header credential",
        )
    # constant-time comparison to prevent side-channel timing attacks
    if not hmac.compare_digest(x_api_key.encode("utf-8"), expected_key.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: Invalid API Key credential",
        )


# Helper function to redact raw secrets/keys recursively
def redact_secrets(val: Any) -> Any:
    if isinstance(val, dict):
        return {
            k: (
                "**REDACTED**"
                if "key" in k.lower() or "secret" in k.lower() or "token" in k.lower()
                else redact_secrets(v)
            )
            for k, v in val.items()
        }
    elif isinstance(val, list):
        return [redact_secrets(item) for item in val]
    return val


@router.get("/health")
def get_health():
    # Public Endpoint
    return {"status": "ok", "service": "recoverai-backend"}


@router.get("/api/metrics", response_model=MetricsResponse, dependencies=[Depends(verify_api_key)])
def get_metrics():
    # Read-Only Endpoint (Public/Unauthenticated for buildathon demo observability)
    # Check cache first
    cached_metrics = RedisCache.get("metrics_data")
    if cached_metrics:
        try:
            data = json.loads(cached_metrics)
            return MetricsResponse(**data)
        except Exception:
            pass

    eval_file = (
        Path(__file__).parent.parent.parent / "tests" / "evaluation_results.json"
    )
    if not eval_file.exists():
        events_path = (
            Path(__file__).parent.parent.parent / "tests" / "synthetic_events.json"
        )
        if events_path.exists():
            try:
                res = BatchEvaluator.evaluate_batch(
                    str(events_path), limit=25, stratify=True, mode="backend"
                )
                with open(eval_file, "w") as f:
                    json.dump(res, f, indent=2)
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to generate baseline metrics: {str(e)}",
                )
        else:
            return MetricsResponse(
                revenue_at_risk=0.0,
                revenue_recovered=0.0,
                recovery_rate=0.0,
                action_success_rate=0.0,
                guard_block_rate=0.0,
                human_escalation_rate=0.0,
                backend_action_agreement=0.0,
                council_action_agreement=0.0,
            )

    try:
        with open(eval_file, "r") as f:
            data = json.load(f)
            metrics_response = MetricsResponse(
                revenue_at_risk=data.get("total_amount_at_risk", 0.0),
                revenue_recovered=data.get("revenue_recovered", 0.0),
                recovery_rate=data.get("recovery_rate", 0.0),
                action_success_rate=data.get("action_success_rate", 0.0),
                guard_block_rate=data.get("guard_block_rate", 0.0),
                human_escalation_rate=data.get("human_escalation_rate", 0.0),
                backend_action_agreement=data.get("backend_action_agreement"),
                council_action_agreement=data.get("council_action_agreement"),
            )
            # Write to cache
            try:
                RedisCache.set("metrics_data", json.dumps(metrics_response.dict()), expire_seconds=300)
            except Exception:
                pass
            return metrics_response
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to read metrics: {str(e)}")


# Shared helper mapping priority score to risk taxonomy
def get_risk_level(priority_score: int) -> str:
    if priority_score < 40:
        return "LOW"
    elif priority_score < 70:
        return "MEDIUM"
    return "HIGH"


@router.get("/api/cases", response_model=CaseListResponse, dependencies=[Depends(verify_api_key)])
def list_cases(
    status: Optional[str] = None,
    event_type: Optional[str] = None,
    action: Optional[str] = None,
    risk_level: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    # Read-Only Endpoint
    query = db.query(RecoveryCase).join(PaymentEvent)

    if status:
        query = query.filter(RecoveryCase.status == status)
    if event_type:
        query = query.filter(PaymentEvent.event_type == event_type)

    # Enforce risk filtering bounds using the same taxonomy logic
    if risk_level:
        risk_upper = risk_level.upper().strip()
        if risk_upper == "LOW":
            query = query.filter(RecoveryCase.priority_score < 40)
        elif risk_upper == "MEDIUM":
            query = query.filter(
                RecoveryCase.priority_score >= 40, RecoveryCase.priority_score < 70
            )
        elif risk_upper == "HIGH":
            query = query.filter(RecoveryCase.priority_score >= 70)

    # Determine action filter mappings if requested
    if action:
        query = query.join(RecoveryAction).filter(RecoveryAction.action_type == action)

    # Hard bounds and pagination limits
    total = query.count()
    offset = (page - 1) * page_size

    # Deterministic query order sort descending by creation timestamps
    cases = (
        query.order_by(RecoveryCase.created_at.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    items = []
    for c in cases:
        proposed_action = None
        confidence = 0.0

        latest_action = (
            db.query(RecoveryAction)
            .filter(RecoveryAction.case_id == c.id)
            .order_by(RecoveryAction.created_at.desc())
            .first()
        )
        if latest_action:
            proposed_action = latest_action.action_type.value

        # Parse audit log events to extract the dynamic planner confidence score
        if c.audit_log:
            planner_events = [
                e
                for e in c.audit_log
                if isinstance(e, dict) and e.get("node") == "planner"
            ]
            if planner_events:
                confidence = planner_events[-1].get("confidence", 0.0)

        items.append(
            CaseSummary(
                case_id=c.id,
                event_id=c.event_id,
                event_type=c.event.event_type,
                amount=c.event.amount,
                currency=c.event.currency,
                failure_code=c.event.failure_code,
                status=c.status.value,
                priority_score=c.priority_score,
                expected_recovery_value=c.expected_recovery_value,
                current_recovery_attempt=c.current_recovery_attempt,
                proposed_action=proposed_action,
                confidence=confidence,
                created_at=c.created_at,
            )
        )

    return CaseListResponse(total=total, page=page, page_size=page_size, items=items)


@router.get("/api/cases/{case_id}", response_model=CaseDetail, dependencies=[Depends(verify_api_key)])
def get_case(case_id: str, db: Session = Depends(get_db)):
    # Read-Only Endpoint
    c = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Recovery Case not found")

    actions = db.query(RecoveryAction).filter(RecoveryAction.case_id == case_id).all()
    actions_list = [
        {
            "id": a.id,
            "action_type": a.action_type.value,
            "proposed_by": a.proposed_by,
            "state": a.state.value,
            "authorization_token": a.authorization_token,
            "action_id": a.action_id,
            "execution_id": a.execution_id,
            "created_at": a.created_at.isoformat(),
        }
        for a in actions
    ]

    # Redact sensitive parameters recursively inside audit log outputs before response dispatches
    redacted_logs = redact_secrets(c.audit_log or [])

    return CaseDetail(
        case_id=c.id,
        event_id=c.event_id,
        event_type=c.event.event_type,
        amount=c.event.amount,
        currency=c.event.currency,
        failure_code=c.event.failure_code,
        status=c.status.value,
        priority_score=c.priority_score,
        expected_recovery_value=c.expected_recovery_value,
        current_recovery_attempt=c.current_recovery_attempt,
        audit_log=redacted_logs,
        created_at=c.created_at,
        actions=actions_list,
    )


@router.post(
    "/api/score", response_model=ScoreResponse, dependencies=[Depends(verify_api_key)]
)
def post_score(req: ScoreRequest):
    # PROTECTED Endpoint
    if req.amount < 0:
        raise HTTPException(status_code=400, detail="Invalid negative amount")
    if req.history_success_rate < 0.0 or req.history_success_rate > 1.0:
        raise HTTPException(
            status_code=400, detail="Success rate must be between 0.0 and 1.0"
        )

    try:
        prob = calculate_recoverability_probability(
            failure_code=req.failure_code,
            history_success_rate=req.history_success_rate,
            attempt=req.attempt,
        )
        erv = calculate_erv(
            amount=req.amount,
            currency=req.currency,
            failure_code=req.failure_code,
            history_success_rate=req.history_success_rate,
            attempt=req.attempt,
        )
        priority = calculate_priority_score(
            amount=req.amount,
            currency=req.currency,
            failure_code=req.failure_code,
            history_success_rate=req.history_success_rate,
            attempt=req.attempt,
            urgency_factor=req.urgency_factor,
        )
        return ScoreResponse(
            expected_recovery_value=erv,
            recoverability_probability=prob,
            priority_score=priority,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Scoring error: {str(e)}")


@router.post(
    "/api/action-guard/evaluate",
    response_model=ActionGuardResponse,
    dependencies=[Depends(verify_api_key)],
)
def evaluate_guard(req: ActionGuardRequest, db: Session = Depends(get_db)):
    # PROTECTED Endpoint
    if req.amount < 0:
        raise HTTPException(status_code=400, detail="Invalid negative amount")
    if req.planner_confidence < 0.0 or req.planner_confidence > 1.0:
        raise HTTPException(
            status_code=400, detail="Confidence must be between 0.0 and 1.0"
        )

    case_id = req.case_id.strip()
    event_id = req.event_id.strip()
    action_id = req.action_id.strip()

    if not case_id or not event_id or not action_id:
        raise HTTPException(
            status_code=400,
            detail="case_id, event_id, and action_id must be non-empty strings",
        )

    # Verify central state transitions lifecycles
    c = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if c:
        # Enforce transition rules
        if not CaseStateMachine.validate_transition(c.status, CaseStatus.GUARD_REVIEW):
            raise HTTPException(
                status_code=400,
                detail=f"Illegal transition: {c.status} -> GUARD_REVIEW",
            )

    approved, token, violations = ActionGuard.validate_action(
        action_type=req.action_type.value,
        amount=req.amount,
        currency=req.currency,
        current_attempts=req.current_attempts,
        max_retries=req.max_retries,
        amount_threshold_inr=req.amount_threshold,
        has_active_action=req.has_active_action,
        last_contact_at_str=req.last_contact_at,
        now_str=req.now,
        planner_confidence=req.planner_confidence,
        case_id=case_id,
        event_id=event_id,
        action_id=action_id,
    )

    res_status = "APPROVED" if approved else "REJECTED"
    if req.action_type == ActionTypeEnum.ESCALATE_TO_HUMAN:
        res_status = "HUMAN_REVIEW"

    # Insert dynamic RecoveryAction authorization request record into database
    from app.models.case import ActionState

    action_state = (
        ActionState.APPROVED_BY_GUARD if approved else ActionState.REJECTED_BY_GUARD
    )

    # Check if case exists in db to append the action record
    if c:
        db_action = RecoveryAction(
            case_id=case_id,
            action_type=req.action_type.value,
            proposed_by="RecoveryCouncil",
            state=action_state,
            authorization_token=token if approved else None,
            action_id=action_id,
            execution_id=None,
        )
        db.add(db_action)
        db.commit()

    # Invalidate cached metrics
    RedisCache.delete("metrics_data")

    return ActionGuardResponse(
        approved=approved,
        authorization_token=token if approved else None,
        resulting_status=res_status,
        violations=violations,
        warnings=[],
    )


@router.post(
    "/api/execute",
    response_model=ExecuteResponse,
    dependencies=[Depends(verify_api_key)],
)
def execute_recovery_action(req: ExecuteRequest, db: Session = Depends(get_db)):
    # PROTECTED Endpoint
    # 1. Hard validation: Blocked actions must never execute
    if not req.guard_approved or not req.authorization_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Action Guard Violation: Attempted to execute an action without authorization.",
        )

    case_id = req.case_id.strip()
    event_id = req.event_id.strip()
    action_id = req.action_id.strip()

    if not case_id or not event_id or not action_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="case_id, event_id, and action_id must be non-empty strings.",
        )

    from decimal import Decimal

    try:
        req_amount_dec = Decimal(str(req.amount)).quantize(Decimal("1.00"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid amount format."
        )

    # 3. Look up action_id in EXECUTION_REGISTRY under execution registry lock
    from app.services.executor import EXECUTION_REGISTRY, _execution_lock
    import time

    existing_res = None
    is_wait_required = False

    with _execution_lock:
        existing_res = EXECUTION_REGISTRY.get(action_id)
        if existing_res:
            if existing_res.get("status") == "PENDING":
                is_wait_required = True
            else:
                # Check if the execution result is authoritative/immutable
                if not existing_res.get("execution_id"):
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Corrupt idempotency state: Cached execution record for action_id {action_id} is incomplete.",
                    )

                try:
                    exist_amount_dec = Decimal(str(existing_res["amount"])).quantize(
                        Decimal("1.00")
                    )
                except Exception:
                    exist_amount_dec = Decimal("0.00")

                # Verify if all parameter bindings match exactly (idempotency check)
                if (
                    existing_res["case_id"] != case_id
                    or existing_res["event_id"] != event_id
                    or existing_res["action"] != req.action_type.value
                    or exist_amount_dec != req_amount_dec
                    or existing_res["currency"].upper() != req.currency.upper()
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Idempotency conflict: action_id {action_id} already executed with different parameters.",
                    )

                return ExecuteResponse(
                    execution_id=existing_res["execution_id"],
                    action=existing_res["action"],
                    status=existing_res["status"],
                    recovered=existing_res["recovered"],
                    recovered_amount=existing_res["recovered_amount"],
                    message=existing_res["message"],
                    executed_at=existing_res["executed_at"],
                )
        else:
            # Set a placeholder PENDING status block in registry to notify other concurrent requests
            # that execution is in progress for this action_id
            EXECUTION_REGISTRY[action_id] = {
                "status": "PENDING",
                "case_id": case_id,
                "event_id": event_id,
                "action": req.action_type.value,
                "amount": req.amount,
                "currency": req.currency,
            }

    # If another thread is actively running this execution, wait for it to complete
    if is_wait_required:
        start_wait = time.time()
        while True:
            time.sleep(0.05)
            with _execution_lock:
                current_res = EXECUTION_REGISTRY.get(action_id)
                if current_res and current_res.get("status") != "PENDING":
                    # Completed execution committed successfully, return it directly
                    return ExecuteResponse(
                        execution_id=current_res["execution_id"],
                        action=current_res["action"],
                        status=current_res["status"],
                        recovered=current_res["recovered"],
                        recovered_amount=current_res["recovered_amount"],
                        message=current_res["message"],
                        executed_at=current_res["executed_at"],
                    )
            if time.time() - start_wait > 5.0:
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail="Gateway Timeout: Concurrent execution timed out waiting for original request to commit.",
                )

    # 4. Strictly verify token and atomically consume under ActionGuard registry lock
    valid, err_msg = ActionGuard.verify_and_claim_token(
        token=req.authorization_token,
        case_id=case_id,
        event_id=event_id,
        action_id=action_id,
        action_type=req.action_type.value,
        amount=req.amount,
        currency=req.currency,
    )
    if not valid:
        # Clear the PENDING placeholder on authorization failures to allow clean retries
        with _execution_lock:
            EXECUTION_REGISTRY.pop(action_id, None)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Authorization validation failed: {err_msg}",
        )

    # Check case state constraints if db record exists
    c = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if c:
        if not CaseStateMachine.validate_transition(c.status, CaseStatus.EXECUTING):
            with _execution_lock:
                EXECUTION_REGISTRY.pop(action_id, None)
            raise HTTPException(
                status_code=400, detail=f"Illegal transition: {c.status} -> EXECUTING"
            )

    try:
        # Update case state database tables dynamically on transition execution
        if c:
            c.status = CaseStatus.EXECUTING
            db.commit()

        res = ExecutionSimulator.execute_action(
            action_type=req.action_type.value,
            amount=req.amount,
            currency=req.currency,
            is_guard_approved=req.guard_approved,
            auth_token=req.authorization_token,
            ground_truth=req.ground_truth,
            action_id=action_id,
            case_id=case_id,
            event_id=event_id,
        )

        # Update case execution status final outcomes
        if c:
            target_state = (
                CaseStatus.RECOVERED if res["recovered"] else CaseStatus.FAILED
            )
            if CaseStateMachine.validate_transition(c.status, target_state):
                c.status = target_state
                # Update corresponding RecoveryAction state and execution_id in database
                act_state = (
                    ActionState.SUCCESSFUL if res["recovered"] else ActionState.FAILED
                )
                db_act = (
                    db.query(RecoveryAction)
                    .filter(
                        RecoveryAction.case_id == case_id,
                        RecoveryAction.action_id == action_id,
                    )
                    .first()
                )
                if db_act:
                    db_act.state = act_state
                    db_act.execution_id = res["execution_id"]

                # Append append-only structured audit logs mapping explainability traces
                # Ground truth does NOT establish trusted decision source provenance.
                decision_source = "API_SIMULATION"

                log_entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "node": "execution",
                    "event": "execution_result",
                    "inputs": {
                        "action": req.action_type.value,
                        "amount": float(req.amount),
                    },
                    "outputs": {"status": res["status"], "recovered": res["recovered"]},
                    "decision": res["status"],
                    "confidence": 1.0,
                    "decision_source": decision_source,
                    "model": "fallback_rules",
                    "request_id": res["execution_id"],
                    "playbook_id": "PLAYBOOK_SIMULATION_DEFAULT",
                }
                c.audit_log = (c.audit_log or []) + [log_entry]
                db.commit()

        # Invalidate cached metrics
        RedisCache.delete("metrics_data")

        return ExecuteResponse(
            execution_id=res["execution_id"],
            action=res["action"],
            status=res["status"],
            recovered=res["recovered"],
            recovered_amount=res["recovered_amount"],
            message=res["message"],
            executed_at=res["executed_at"],
        )
    except PermissionError as pe:
        # Safe release prior to actual dispatch on permission faults
        with _execution_lock:
            EXECUTION_REGISTRY.pop(action_id, None)
        ActionGuard.release_token(req.authorization_token)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(pe))
    except Exception as e:
        # Failure after dispatch never releases token
        with _execution_lock:
            # Update the registry record with error status to unblock waiting threads
            EXECUTION_REGISTRY[action_id] = {
                "execution_id": f"ERROR-{secrets.token_hex(4).upper()}",
                "action": req.action_type.value,
                "status": "FAILED",
                "recovered": False,
                "recovered_amount": Decimal("0.00"),
                "message": f"Simulation execution error: {str(e)}",
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "case_id": case_id,
                "event_id": event_id,
            }
        raise HTTPException(
            status_code=500, detail=f"Simulation execution error: {str(e)}"
        )
