from typing import Dict, Any, Tuple, List
import secrets
import threading
from datetime import datetime, timezone
from decimal import Decimal

# Threading Lock for process-level registry synchronization
_registry_lock = threading.Lock()

# Deployment constraint:
# The in-memory token registry and locking mechanism are appropriate for the single-process demo/simulator.
# A multi-worker or production deployment requires Redis or database-backed token state with distributed atomic locking/claim semantics.
TOKEN_REGISTRY: Dict[str, Dict[str, Any]] = {}


class ActionGuard:
    @staticmethod
    def validate_action(
        action_type: str,
        amount: Decimal,
        currency: str,
        current_attempts: int,
        max_retries: int,
        amount_threshold_inr: Decimal,
        has_active_action: bool = False,
        last_contact_at_str: str = None,
        now_str: str = None,
        contact_cooldown_hours: int = 24,
        planner_confidence: float = 1.0,
        min_confidence_threshold: float = 0.55,
        case_id: str = None,
        event_id: str = None,
        action_id: str = None,
    ) -> Tuple[bool, str, List[str]]:
        """
        Validates the proposed recovery action against deterministic business policies.
        Enforces allowlists, active locks, limits, thresholds, and generates cryptographic tokens.
        """
        violations = []
        allowed_actions = {
            "RETRY_PAYMENT",
            "SEND_PAYMENT_REMINDER",
            "SEND_CHECKOUT_RECOVERY_MESSAGE",
            "RETRY_SUBSCRIPTION",
            "SEND_INVOICE_REMINDER",
            "ESCALATE_TO_HUMAN",
            "DO_NOTHING",
        }

        # 1. Allowlist Validation
        if action_type not in allowed_actions:
            violations.append(f"Action Guard: action '{action_type}' is not supported.")
            return False, "", violations

        # Currency Normalization (USD normalized to INR at 1 USD = 83.00 INR)
        normalized_amount = Decimal(str(amount))
        if currency and currency.upper().strip() == "USD":
            normalized_amount = Decimal(str(amount)) * Decimal("83.00")

        # DO_NOTHING & ESCALATE_TO_HUMAN bypass limits, but must still generate secure token
        if action_type not in ("DO_NOTHING", "ESCALATE_TO_HUMAN"):
            # 2. Duplicate Action Validation
            if has_active_action:
                violations.append(
                    "Action Guard: An active recovery action is already executing for this case."
                )

            # 3. Confidence Threshold Check
            if planner_confidence < min_confidence_threshold:
                violations.append(
                    f"Action Guard: Planner confidence {planner_confidence} is below minimum allowed threshold {min_confidence_threshold}."
                )

            # 4. Action-Specific Policies
            if action_type in ("RETRY_PAYMENT", "RETRY_SUBSCRIPTION"):
                if current_attempts >= max_retries:
                    violations.append(
                        f"Action Guard: Retry payment limit reached ({current_attempts}/{max_retries}). Action '{action_type}' blocked."
                    )

                if normalized_amount > amount_threshold_inr:
                    violations.append(
                        f"Action Guard: Transaction amount {amount} {currency} exceeds security threshold of {amount_threshold_inr} INR for auto-retry action '{action_type}'. Human review required."
                    )

            elif action_type in (
                "SEND_PAYMENT_REMINDER",
                "SEND_INVOICE_REMINDER",
                "SEND_CHECKOUT_RECOVERY_MESSAGE",
            ):
                if last_contact_at_str and now_str:
                    try:
                        last_contact = datetime.fromisoformat(
                            last_contact_at_str.replace("Z", "+00:00")
                        )
                        now = datetime.fromisoformat(now_str.replace("Z", "+00:00"))
                        diff_hours = (now - last_contact).total_seconds() / 3600.0
                        if diff_hours < contact_cooldown_hours:
                            violations.append(
                                f"Action Guard: Cooldown active. Last contact was {diff_hours:.1f} hours ago. Minimum required is {contact_cooldown_hours} hours."
                            )
                    except Exception:
                        pass

        if len(violations) > 0:
            return False, "", violations

        # Cryptographically secure random token URL safe string
        auth_token = f"AUTH-EXEC-{secrets.token_urlsafe(32)}"

        # Register the token in registry
        ActionGuard.register_token(
            token=auth_token,
            case_id=case_id,
            event_id=event_id,
            action_id=action_id,
            action_type=action_type,
            amount=amount,
            currency=currency,
            expires_in_seconds=300,
        )

        return True, auth_token, []

    @staticmethod
    def register_token(
        token: str,
        case_id: str,
        event_id: str,
        action_id: str,
        action_type: str,
        amount: Decimal,
        currency: str,
        expires_in_seconds: int = 300,
    ):
        with _registry_lock:
            # Lazy cleanup of expired tokens to prevent memory leaks
            now_ts = datetime.now(timezone.utc).timestamp()
            expired_keys = [
                k for k, v in TOKEN_REGISTRY.items() if v["expires_at"] < now_ts
            ]
            for k in expired_keys:
                TOKEN_REGISTRY.pop(k, None)

            # Store mapping
            TOKEN_REGISTRY[token] = {
                "case_id": case_id,
                "event_id": event_id,
                "action_id": action_id,
                "action_type": action_type,
                "amount": amount,
                "currency": currency,
                "issued_at": now_ts,
                "expires_at": now_ts + expires_in_seconds,
                "consumed_at": None,
            }

    @staticmethod
    def verify_token(
        token: str,
        case_id: str,
        event_id: str,
        action_id: str,
        action_type: str,
        amount: float,
        currency: str,
    ) -> Tuple[bool, str]:
        """
        Validates token existence, expiration, consumption, and all key binding parameters.
        Does not consume/claim the token.
        """
        from decimal import Decimal

        try:
            req_amount_dec = Decimal(str(amount)).quantize(Decimal("1.00"))
        except Exception:
            return False, "Invalid decimal format for request amount"

        with _registry_lock:
            entry = TOKEN_REGISTRY.get(token)
            if not entry:
                return False, "Token not found in registry"

            now_ts = datetime.now(timezone.utc).timestamp()
            if entry["expires_at"] < now_ts:
                return False, "Token has expired"

            if entry["consumed_at"] is not None:
                return False, "Token has already been consumed"

            # Validate parameter bindings strictly
            if entry["case_id"] != case_id:
                return (
                    False,
                    f"Token parameter mismatch: case_id ({entry['case_id']} vs {case_id})",
                )
            if entry["event_id"] != event_id:
                return (
                    False,
                    f"Token parameter mismatch: event_id ({entry['event_id']} vs {event_id})",
                )
            if entry["action_id"] != action_id:
                return (
                    False,
                    f"Token parameter mismatch: action_id ({entry['action_id']} vs {action_id})",
                )
            if entry["action_type"] != action_type:
                return (
                    False,
                    f"Token parameter mismatch: action_type ({entry['action_type']} vs {action_type})",
                )

            try:
                entry_amount_dec = Decimal(str(entry["amount"])).quantize(
                    Decimal("1.00")
                )
            except Exception:
                return False, "Invalid decimal format for token registry amount"

            if entry_amount_dec != req_amount_dec:
                return (
                    False,
                    f"Token parameter mismatch: amount ({entry_amount_dec} vs {req_amount_dec})",
                )
            if entry["currency"].upper() != currency.upper():
                return (
                    False,
                    f"Token parameter mismatch: currency ({entry['currency']} vs {currency})",
                )

            return True, ""

    @staticmethod
    def verify_and_claim_token(
        token: str,
        case_id: str,
        event_id: str,
        action_id: str,
        action_type: str,
        amount: float,
        currency: str,
    ) -> Tuple[bool, str]:
        """
        Atomically validates token parameter bindings and claims/consumes the token inside one single lock context
        to eliminate the race window between verify and claim steps.
        """
        from decimal import Decimal

        try:
            req_amount_dec = Decimal(str(amount)).quantize(Decimal("1.00"))
        except Exception:
            return False, "Invalid decimal format for request amount"

        with _registry_lock:
            entry = TOKEN_REGISTRY.get(token)
            if not entry:
                return False, "Token not found in registry"

            now_ts = datetime.now(timezone.utc).timestamp()
            if entry["expires_at"] < now_ts:
                return False, "Token has expired"

            if entry["consumed_at"] is not None:
                return False, "Token has already been consumed"

            # Validate parameter bindings strictly
            if entry["case_id"] != case_id:
                return (
                    False,
                    f"Token parameter mismatch: case_id ({entry['case_id']} vs {case_id})",
                )
            if entry["event_id"] != event_id:
                return (
                    False,
                    f"Token parameter mismatch: event_id ({entry['event_id']} vs {event_id})",
                )
            if entry["action_id"] != action_id:
                return (
                    False,
                    f"Token parameter mismatch: action_id ({entry['action_id']} vs {action_id})",
                )
            if entry["action_type"] != action_type:
                return (
                    False,
                    f"Token parameter mismatch: action_type ({entry['action_type']} vs {action_type})",
                )

            try:
                entry_amount_dec = Decimal(str(entry["amount"])).quantize(
                    Decimal("1.00")
                )
            except Exception:
                return False, "Invalid decimal format for token registry amount"

            if entry_amount_dec != req_amount_dec:
                return (
                    False,
                    f"Token parameter mismatch: amount ({entry_amount_dec} vs {req_amount_dec})",
                )
            if entry["currency"].upper() != currency.upper():
                return (
                    False,
                    f"Token parameter mismatch: currency ({entry['currency']} vs {currency})",
                )

            # Consume the token atomically
            entry["consumed_at"] = now_ts
            return True, ""

    @staticmethod
    def claim_token(token: str) -> bool:
        """
        Atomically claims/consumes the token using a threading Lock.
        Returns True if successfully claimed, False if already consumed or invalid.
        """
        with _registry_lock:
            entry = TOKEN_REGISTRY.get(token)
            if not entry:
                return False
            if entry["consumed_at"] is not None:
                return False
            now_ts = datetime.now(timezone.utc).timestamp()
            if entry["expires_at"] < now_ts:
                return False

            entry["consumed_at"] = now_ts
            return True

    @staticmethod
    def release_token(token: str):
        """
        Releases a claimed token back to unconsumed status if a pre-dispatch execution failure occurs.
        """
        with _registry_lock:
            entry = TOKEN_REGISTRY.get(token)
            if entry:
                entry["consumed_at"] = None
