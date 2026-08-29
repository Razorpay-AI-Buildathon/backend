from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Dict, Any, Optional
import uuid

class PaymentGatewayResult:
    def __init__(
        self,
        success: bool,
        recovered: bool,
        provider_reference: str,
        result_code: str,
        failure_reason: Optional[str] = None,
        recovered_amount: Decimal = Decimal("0.00"),
        metadata: Optional[Dict[str, Any]] = None,
        async_reconciliation: bool = False
    ):
        self.success = success
        self.recovered = recovered
        self.provider_reference = provider_reference
        self.result_code = result_code
        self.failure_reason = failure_reason
        self.recovered_amount = recovered_amount
        self.metadata = metadata or {}
        self.async_reconciliation = async_reconciliation

class PaymentGateway(ABC):
    @abstractmethod
    def execute_action(
        self,
        action_type: str,
        amount: Decimal,
        currency: str,
        case_id: str,
        event_id: str,
        action_id: str,
        ground_truth: Optional[Dict[str, Any]] = None
    ) -> PaymentGatewayResult:
        pass

class SimulatedPaymentGateway(PaymentGateway):
    def execute_action(
        self,
        action_type: str,
        amount: Decimal,
        currency: str,
        case_id: str,
        event_id: str,
        action_id: str,
        ground_truth: Optional[Dict[str, Any]] = None
    ) -> PaymentGatewayResult:
        
        provider_ref = f"SIM-TXN-{uuid.uuid4().hex[:12].upper()}"

        if action_type == "DO_NOTHING":
            return PaymentGatewayResult(
                success=True,
                recovered=False,
                provider_reference=provider_ref,
                result_code="NO_OP",
                failure_reason="No financial action requested",
                recovered_amount=Decimal("0.00")
            )

        if action_type == "ESCALATE_TO_HUMAN":
            return PaymentGatewayResult(
                success=True,
                recovered=False,
                provider_reference=provider_ref,
                result_code="ESCALATED",
                failure_reason="Case escalated to human collections representative",
                recovered_amount=Decimal("0.00")
            )

        action_success = False
        recovered_amount = Decimal("0.00")
        reason = "Recovery action failed: No ground truth simulation outcome provided."

        if ground_truth:
            action_success = ground_truth.get("action_success", False)
            if action_success:
                recovered_amount = Decimal(str(ground_truth.get("recovered_amount", amount)))
                reason = f"Payment recovery successful via strategy: {action_type}"

        async_reconcile = ground_truth.get("async_reconciliation", False) if ground_truth else False
        return PaymentGatewayResult(
            success=action_success,
            recovered=action_success,
            provider_reference=provider_ref,
            result_code="SUCCESS" if action_success else "DECLINED",
            failure_reason=reason,
            recovered_amount=recovered_amount,
            async_reconciliation=async_reconcile
        )

class RazorpayPaymentGateway(PaymentGateway):
    """
    Placeholder/adapter for Razorpay Payment Link / Subscription API charges.
    Used for production mode in real deployment conditions.
    """
    def execute_action(
        self,
        action_type: str,
        amount: Decimal,
        currency: str,
        case_id: str,
        event_id: str,
        action_id: str,
        ground_truth: Optional[Dict[str, Any]] = None
    ) -> PaymentGatewayResult:
        # Placeholder integration logic
        provider_ref = f"RP-TXN-{uuid.uuid4().hex[:12].upper()}"
        return PaymentGatewayResult(
            success=False,
            recovered=False,
            provider_reference=provider_ref,
            result_code="UNCONFIGURED",
            failure_reason="Razorpay Gateway API credentials missing or unconfigured.",
            recovered_amount=Decimal("0.00")
        )
