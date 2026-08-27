import uuid
import threading
from datetime import datetime, timezone
from typing import Dict, Any
from decimal import Decimal

# Canonical persistent execution records store mapping
# Maps execution_id (or action_id) -> execution result
EXECUTION_REGISTRY: Dict[str, Dict[str, Any]] = {}
_execution_lock = threading.Lock()


class ExecutionSimulator:
    @staticmethod
    def execute_action(
        action_type: str,
        amount: Decimal,
        currency: str,
        is_guard_approved: bool,
        auth_token: str = None,
        ground_truth: Dict[str, Any] = None,
        action_id: str = None,
        case_id: str = None,
        event_id: str = None,
    ) -> Dict[str, Any]:
        """
        Deterministic downstream payment recovery execution simulator.
        Ensures that blocked actions are never executed and enforces idempotency bounds.
        """
        # 1. Hard validation: Blocked actions must never execute
        if not is_guard_approved or not auth_token:
            raise PermissionError(
                "Action Guard Violation: Attempted to execute an action without authorization."
            )

        target_id = action_id or f"SIM-{uuid.uuid4().hex[:8].upper()}"

        with _execution_lock:
            # 2. Idempotency Check: Return existing execution result if already completed and not a placeholder PENDING record
            if target_id in EXECUTION_REGISTRY:
                existing = EXECUTION_REGISTRY[target_id]
                if existing.get("status") != "PENDING":
                    return existing

            # 3. DO_NOTHING & ESCALATE_TO_HUMAN are no-ops on payment systems
            if action_type == "DO_NOTHING":
                res = {
                    "execution_id": target_id,
                    "action_id": target_id,
                    "case_id": case_id or "demo-case",
                    "event_id": event_id or "event-id",
                    "action": action_type,
                    "amount": amount,
                    "currency": currency,
                    "status": "SUCCESS",
                    "recovered": False,
                    "recovered_amount": Decimal("0.00"),
                    "message": "No financial action requested",
                    "executed_at": datetime.now(timezone.utc).isoformat(),
                }
                EXECUTION_REGISTRY[target_id] = res
                return res

            if action_type == "ESCALATE_TO_HUMAN":
                res = {
                    "execution_id": target_id,
                    "action_id": target_id,
                    "case_id": case_id or "demo-case",
                    "event_id": event_id or "event-id",
                    "action": action_type,
                    "amount": amount,
                    "currency": currency,
                    "status": "SUCCESS",
                    "recovered": False,
                    "recovered_amount": Decimal("0.00"),
                    "message": "Case escalated to human collections representative",
                    "executed_at": datetime.now(timezone.utc).isoformat(),
                }
                EXECUTION_REGISTRY[target_id] = res
                return res

            # 4. Simulate execution based on ground truth outcomes
            # Note: ground_truth is simulation/test metadata only. In production this data is fetched from payment provider.
            # Genuinely prevent success-by-default execution. Missing outcome defaults to failure.
            action_success = False
            recovered_amount = Decimal("0.00")
            message = (
                "Recovery action failed: No ground truth simulation outcome provided."
            )

            if ground_truth:
                action_success = ground_truth.get("action_success", False)
                if action_success:
                    recovered_amount = Decimal(
                        str(ground_truth.get("recovered_amount", amount))
                    )
                    message = f"Payment recovery successful via strategy: {action_type}"

            # Generate unique execution_id for every actual execution attempt
            attempt_execution_id = f"EXEC-{uuid.uuid4().hex[:12].upper()}"

            res = {
                "execution_id": attempt_execution_id,
                "action_id": target_id,
                "case_id": case_id or "demo-case",
                "event_id": event_id or "event-id",
                "action": action_type,
                "amount": amount,
                "currency": currency,
                "status": "SUCCESS" if action_success else "FAILED",
                "recovered": action_success,
                "recovered_amount": recovered_amount,
                "message": message,
                "executed_at": datetime.now(timezone.utc).isoformat(),
            }

            # Persist execution result in canonical registry
            EXECUTION_REGISTRY[target_id] = res
            return res
