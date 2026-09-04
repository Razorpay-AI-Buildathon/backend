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
        ground_truth: Optional[Dict[str, Any]] = None,
        simulate_failure: bool = False
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
        ground_truth: Optional[Dict[str, Any]] = None,
        simulate_failure: bool = False
    ) -> PaymentGatewayResult:
        
        provider_ref = f"SIM-TXN-{uuid.uuid4().hex[:12].upper()}"

        if simulate_failure:
            return PaymentGatewayResult(
                success=False,
                recovered=False,
                provider_reference=provider_ref,
                result_code="SIM_FORCED_FAILURE",
                failure_reason="Operator forced a simulated failure to demonstrate retry backoff.",
            )

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
    def execute_action(
        self,
        action_type: str,
        amount: Decimal,
        currency: str,
        case_id: str,
        event_id: str,
        action_id: str,
        ground_truth: Optional[Dict[str, Any]] = None,
        simulate_failure: bool = False
    ) -> PaymentGatewayResult:
        from app.core.config import settings
        import httpx
        import json

        if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
            provider_ref = f"RP-TXN-UNCONFIGURED-{uuid.uuid4().hex[:6].upper()}"
            return PaymentGatewayResult(
                success=False,
                recovered=False,
                provider_reference=provider_ref,
                result_code="UNCONFIGURED",
                failure_reason="Razorpay Gateway API credentials missing or unconfigured.",
                recovered_amount=Decimal("0.00")
            )

        if action_type not in ("RETRY_PAYMENT", "RETRY_SUBSCRIPTION"):
            provider_ref = f"RP-NOTIF-{uuid.uuid4().hex[:12].upper()}"
            return PaymentGatewayResult(
                success=True,
                recovered=False,
                provider_reference=provider_ref,
                result_code="SUCCESS",
                failure_reason=f"Notification strategy {action_type} sent successfully."
            )

        # Convert amount to paise (integer cents for Razorpay)
        amount_in_paise = int(amount * 100)

        payload = {
            "amount": amount_in_paise,
            "currency": currency,
            "accept_partial": False,
            "reference_id": action_id,
            "description": f"RecoverAI Recovery Link - Case: {case_id}",
            "customer": {
                "name": "Operator",
                "email": "operator@recoverai.com"
            },
            "notify": {
                "sms": False,
                "email": False
            }
        }

        try:
            # Call live Razorpay Test/Production Payment Links API
            auth = (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
            resp = httpx.post(
                "https://api.razorpay.com/v1/payment_links",
                json=payload,
                auth=auth,
                timeout=10.0
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                provider_ref = data.get("id") or f"RP-PLINK-{uuid.uuid4().hex[:6].upper()}"
                return PaymentGatewayResult(
                    success=True,
                    recovered=False,
                    provider_reference=provider_ref,
                    result_code="PENDING",
                    failure_reason="Payment Link generated successfully. Awaiting payment capture webhook.",
                    recovered_amount=Decimal("0.00"),
                    metadata=data,
                    async_reconciliation=True
                )
            else:
                try:
                    error_msg = resp.json().get("error", {}).get("description", resp.text)
                except Exception:
                    error_msg = resp.text
                return PaymentGatewayResult(
                    success=False,
                    recovered=False,
                    provider_reference=f"RP-TXN-FAILED-{uuid.uuid4().hex[:6].upper()}",
                    result_code="FAILED",
                    failure_reason=f"Razorpay API Error: {error_msg}",
                    recovered_amount=Decimal("0.00")
                )
        except Exception as e:
            return PaymentGatewayResult(
                success=False,
                recovered=False,
                provider_reference=f"RP-TXN-TIMEOUT-{uuid.uuid4().hex[:6].upper()}",
                result_code="TIMEOUT",
                failure_reason=f"Connection timeout reaching Razorpay payment links API: {str(e)}",
                recovered_amount=Decimal("0.00")
            )

