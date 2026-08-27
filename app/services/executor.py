import uuid
import os
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from decimal import Decimal
from app.services.gateway import SimulatedPaymentGateway, RazorpayPaymentGateway

# Canonical persistent execution records store mapping
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
        ground_truth: Optional[Dict[str, Any]] = None,
        action_id: str = None,
        case_id: str = None,
        event_id: str = None,
    ) -> Dict[str, Any]:
        """
        Deterministic downstream payment recovery execution wrapper.
        Delegates core processing to underlying PaymentGateway abstractions.
        """
        # 1. Hard validation: Blocked actions must never execute
        if not is_guard_approved or not auth_token:
            raise PermissionError(
                "Action Guard Violation: Attempted to execute an action without authorization."
            )

        target_id = action_id or f"SIM-{uuid.uuid4().hex[:8].upper()}"

        with _execution_lock:
            # 2. Idempotency Check: Return existing execution result if already completed
            if target_id in EXECUTION_REGISTRY:
                existing = EXECUTION_REGISTRY[target_id]
                if existing.get("status") != "PENDING":
                    return existing

            # Determine gateway adapter (default to simulated/demo mode)
            gateway_mode = os.getenv("GATEWAY_MODE", "SIMULATION")
            if gateway_mode == "PRODUCTION":
                gateway = RazorpayPaymentGateway()
            else:
                gateway = SimulatedPaymentGateway()

            # 3. Call the Gateway Abstraction layer
            result = gateway.execute_action(
                action_type=action_type,
                amount=amount,
                currency=currency,
                case_id=case_id or "demo-case",
                event_id=event_id or "event-id",
                action_id=target_id,
                ground_truth=ground_truth
            )

            res = {
                "execution_id": result.provider_reference,
                "action_id": target_id,
                "case_id": case_id or "demo-case",
                "event_id": event_id or "event-id",
                "action": action_type,
                "amount": amount,
                "currency": currency,
                "status": "SUCCESS" if result.success or action_type in ("DO_NOTHING", "ESCALATE_TO_HUMAN") else "FAILED",
                "recovered": result.recovered,
                "recovered_amount": result.recovered_amount,
                "message": result.failure_reason or "Execution completed",
                "executed_at": datetime.now(timezone.utc).isoformat(),
            }

            # Persist execution result in canonical registry
            EXECUTION_REGISTRY[target_id] = res
            return res
