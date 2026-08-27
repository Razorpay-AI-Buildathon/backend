import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
from app.services.action_guard import ActionGuard
from app.services.scoring import calculate_erv, calculate_priority_score
from app.services.executor import ExecutionSimulator


class BatchEvaluator:
    @staticmethod
    def evaluate_batch(
        events_path: str, limit: int = 25, stratify: bool = True, mode: str = "backend"
    ) -> Dict[str, Any]:
        """
        Ingests events and evaluates them through scoring, Action Guard, and execution pipelines.
        Supports development limitations and stratified sample partitions.
        Modes:
            - 'backend': fast evaluation passing event data directly through backend Action Guard & Executor.
            - 'council': passes events through full 5-Agent LangGraph Council before the Action Guard handoff.
        """
        with open(events_path, "r") as f:
            all_events = json.load(f)

        selected_events = all_events
        if stratify and limit < len(all_events):
            # Select stratified samples across diverse failure modes and event boundaries
            failed_payments = [
                e for e in all_events if e["event_type"] == "FAILED_PAYMENT"
            ]
            checkout_ab = [
                e for e in all_events if e["event_type"] == "CHECKOUT_ABANDONMENT"
            ]
            subscriptions = [
                e for e in all_events if e["event_type"] == "SUBSCRIPTION_FAILURE"
            ]
            invoices = [e for e in all_events if e["event_type"] == "OVERDUE_INVOICE"]

            # Boundary cases (limits, high amounts, fraud risk profiles)
            boundary_cases = [
                e
                for e in all_events
                if e["recovery_context"]["attempt_number"] >= 3 or e["amount"] > 5000.0
            ]

            # Select slices evenly
            slice_size = max(1, limit // 5)
            selected_events = (
                failed_payments[:slice_size]
                + checkout_ab[:slice_size]
                + subscriptions[:slice_size]
                + invoices[:slice_size]
                + boundary_cases[:slice_size]
            )
            # Trim to exact limit
            selected_events = selected_events[:limit]
        elif limit < len(all_events):
            selected_events = all_events[:limit]

        from decimal import Decimal

        # Ingest and process evaluations
        total_cases = len(selected_events)
        total_amount_at_risk = Decimal("0.00")
        revenue_recovered = Decimal("0.00")
        total_proposed = 0
        executed_actions = 0
        successful_actions = 0
        guard_blocked = 0
        human_escalations = 0
        action_agreements = 0

        case_logs = []

        # If executing council mode, initialize graph workflow context
        graph_runner = None
        if mode == "council":
            os.environ["STANDALONE_CLI_TEST"] = "true"
            ai_service_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..", "ai-service")
            )
            backend_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend")
            )

            # Remove all 'app' modules from cache
            cached_app_modules = {
                k: v
                for k, v in list(sys.modules.items())
                if k == "app" or k.startswith("app.")
            }
            for k in cached_app_modules:
                sys.modules.pop(k, None)

            # Temporarily modify sys.path to prioritize ai-service and remove backend
            original_path = list(sys.path)
            if backend_root in sys.path:
                sys.path.remove(backend_root)
            if ai_service_root not in sys.path:
                sys.path.insert(0, ai_service_root)

            try:
                from app.graph import graph as resolved_graph

                graph_runner = resolved_graph
            finally:
                # Restore original path and modules
                sys.path = original_path
                for k, v in cached_app_modules.items():
                    sys.modules[k] = v

        for event in selected_events:
            amount = Decimal(str(event["amount"]))
            currency = event["currency"]
            failure_code = event.get("failure_code")
            customer_history = event["customer_history"]
            recovery_context = event["recovery_context"]
            ground_truth = event["ground_truth"]

            # Derive cooldown time delta checks correctly:
            last_contact_str = event.get("timestamp")
            now_eval_str = event.get("timestamp")

            if last_contact_str and "hours_since_event" in recovery_context:
                try:
                    last_contact = datetime.fromisoformat(
                        last_contact_str.replace("Z", "+00:00")
                    )
                    now_eval = last_contact + timedelta(
                        hours=float(recovery_context["hours_since_event"])
                    )
                    now_eval_str = now_eval.isoformat().replace("+00:00", "Z")
                except Exception:
                    pass

            # Normalization conversion to INR base for risk checks
            norm_multiplier = Decimal("83.00") if currency == "USD" else Decimal("1.00")
            amount_inr = amount * norm_multiplier
            total_amount_at_risk += amount_inr

            # Propose action: Determine whether it's generated by AI or pulled from playbook simulator
            proposed_action = ground_truth["recommended_action"]
            planner_confidence = 0.90

            if mode == "council" and graph_runner:
                state_input = {
                    "case_id": event["id"],
                    "event_id": event["id"],
                    "event_type": event["event_type"],
                    "amount": float(amount),
                    "currency": currency,
                    "failure_code": failure_code,
                    "customer_id": event["customer_id"],
                    "customer_risk_score": customer_history["risk_score"],
                    "customer_payment_history_success_rate": customer_history[
                        "success_rate"
                    ],
                    "recovery_attempt_count": recovery_context["attempt_number"],
                    "max_retries": recovery_context["max_retries"],
                    "has_active_action": recovery_context["has_active_action"],
                    "last_contact_at_str": (
                        last_contact_str
                        if recovery_context.get("previous_customer_contacts", 0) > 0
                        else None
                    ),
                    "now_str": now_eval_str,
                }
                council_state = graph_runner.invoke(state_input)
                proposed_action = council_state.get("final_action", "DO_NOTHING")
                planner_confidence = council_state.get("final_confidence", 0.0)

            total_proposed += 1

            import uuid

            action_id = f"act-{uuid.uuid4().hex}"

            # Action Guard evaluation
            approved, token, violations = ActionGuard.validate_action(
                action_type=proposed_action,
                amount=amount,
                currency=currency,
                current_attempts=recovery_context["attempt_number"],
                max_retries=recovery_context["max_retries"],
                amount_threshold_inr=Decimal("5000.00"),
                has_active_action=recovery_context["has_active_action"],
                last_contact_at_str=(
                    last_contact_str
                    if recovery_context.get("previous_customer_contacts", 0) > 0
                    else None
                ),
                now_str=now_eval_str,
                contact_cooldown_hours=24,
                planner_confidence=planner_confidence,
                min_confidence_threshold=0.55,
                case_id=event["id"],
                event_id=event["id"],
                action_id=action_id,
            )

            recovered_amount_local = Decimal("0.00")
            action_status = "BLOCKED"

            if approved and proposed_action not in ("DO_NOTHING", "ESCALATE_TO_HUMAN"):
                executed_actions += 1
                res = ExecutionSimulator.execute_action(
                    action_type=proposed_action,
                    amount=amount,
                    currency=currency,
                    is_guard_approved=approved,
                    auth_token=token,
                    ground_truth={
                        **(ground_truth or {}),
                        "case_id": event["id"],
                        "event_id": event["id"],
                        "action_id": action_id,
                    },
                )

                recovered_amount_local = Decimal(str(res["recovered_amount"]))
                revenue_recovered += recovered_amount_local * norm_multiplier

                if res["status"] == "SUCCESS" and res["recovered"]:
                    successful_actions += 1
                    action_status = "RECOVERED"
                else:
                    action_status = "FAILED"
            elif proposed_action == "DO_NOTHING":
                action_status = "BLOCKED"  # DO NOTHING results in zero recovery
            elif proposed_action == "ESCALATE_TO_HUMAN":
                human_escalations += 1
                action_status = "HUMAN_REVIEW"
            else:
                guard_blocked += 1
                action_status = "BLOCKED"

            # Compute ERV & priority scores
            erv = calculate_erv(
                amount,
                currency,
                failure_code,
                customer_history["success_rate"],
                recovery_context["attempt_number"],
            )
            priority = calculate_priority_score(
                amount,
                currency,
                failure_code,
                customer_history["success_rate"],
                recovery_context["attempt_number"],
            )

            # Verify AI proposed action agreement with ground truth
            is_agree = proposed_action == ground_truth["recommended_action"]
            if is_agree:
                action_agreements += 1

            case_logs.append(
                {
                    "case_id": event["id"],
                    "amount": float(amount),
                    "currency": currency,
                    "proposed_action": proposed_action,
                    "guard_approved": approved,
                    "resulting_status": action_status,
                    "erv": float(erv),
                    "priority_score": priority,
                    "recovered_amount": float(recovered_amount_local),
                    "agreed_with_ground_truth": is_agree,
                }
            )

        # Calculate percentages
        recovery_rate = (
            (float(revenue_recovered) / float(total_amount_at_risk) * 100.0)
            if total_amount_at_risk > 0
            else 0.0
        )
        action_success_rate = (
            (successful_actions / executed_actions * 100.0)
            if executed_actions > 0
            else 0.0
        )
        guard_block_rate = (
            (guard_blocked / total_proposed * 100.0) if total_proposed > 0 else 0.0
        )
        human_escalation_rate = (
            (human_escalations / total_cases * 100.0) if total_cases > 0 else 0.0
        )
        agreement_rate = (
            (action_agreements / total_cases * 100.0) if total_cases > 0 else 0.0
        )

        # Cleanup env variables if set
        if mode == "council":
            os.environ["STANDALONE_CLI_TEST"] = "false"

        agreement_key = (
            "council_action_agreement"
            if mode == "council"
            else "backend_action_agreement"
        )

        return {
            "total_cases": total_cases,
            "total_amount_at_risk": float(round(total_amount_at_risk, 2)),
            "revenue_recovered": float(round(revenue_recovered, 2)),
            "recovery_rate": round(recovery_rate, 2),
            "total_proposed_actions": total_proposed,
            "executed_actions": executed_actions,
            "successful_actions": successful_actions,
            "action_success_rate": round(action_success_rate, 2),
            "guard_block_rate": round(guard_block_rate, 2),
            "human_escalation_rate": round(human_escalation_rate, 2),
            agreement_key: round(agreement_rate, 2),
            "cases": case_logs,
        }
