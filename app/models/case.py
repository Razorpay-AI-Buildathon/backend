from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    ForeignKey,
    Numeric,
    JSON,
    Enum,
)
# pyrefly: ignore [missing-import]
from sqlalchemy.dialects.postgresql import UUID, JSONB
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import relationship
import uuid
from datetime import datetime
from app.db.session import Base
import enum


class CaseStatus(str, enum.Enum):
    IDENTIFIED = "IDENTIFIED"
    ANALYZING = "ANALYZING"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    GUARD_REVIEW = "GUARD_REVIEW"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class ActionType(str, enum.Enum):
    RETRY_PAYMENT = "RETRY_PAYMENT"
    SEND_PAYMENT_REMINDER = "SEND_PAYMENT_REMINDER"
    SEND_CHECKOUT_RECOVERY_MESSAGE = "SEND_CHECKOUT_RECOVERY_MESSAGE"
    RETRY_SUBSCRIPTION = "RETRY_SUBSCRIPTION"
    SEND_INVOICE_REMINDER = "SEND_INVOICE_REMINDER"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    DO_NOTHING = "DO_NOTHING"


class ActionState(str, enum.Enum):
    PROPOSED = "PROPOSED"
    APPROVED_BY_GUARD = "APPROVED_BY_GUARD"
    REJECTED_BY_GUARD = "REJECTED_BY_GUARD"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    SUCCESSFUL = "SUCCESSFUL"


class Merchant(Base):
    __tablename__ = "merchants"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    razorpay_key_id = Column(String, nullable=True)
    razorpay_key_secret = Column(String, nullable=True)
    amount_threshold = Column(
        Numeric(12, 2), default=5000.00
    )  # Currency-neutral base threshold
    max_retries = Column(Integer, default=3)
    created_at = Column(DateTime, default=datetime.utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, nullable=False, index=True)
    phone = Column(String, nullable=True)
    risk_score = Column(Numeric(3, 2), default=0.00)
    payment_history_success_rate = Column(Numeric(3, 2), default=1.00)


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type = Column(
        String, nullable=False
    )  # e.g. FAILED_PAYMENT, CHECKOUT_ABANDONMENT
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String, default="INR")
    failure_code = Column(String, nullable=True)
    payload_metadata = Column(JSON, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class RecoveryCase(Base):
    __tablename__ = "recovery_cases"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id = Column(String, ForeignKey("payment_events.id"), nullable=False)
    status = Column(Enum(CaseStatus), default=CaseStatus.IDENTIFIED)
    priority_score = Column(Integer, default=0)
    expected_recovery_value = Column(Numeric(12, 2), default=0.00)
    current_recovery_attempt = Column(Integer, default=0)
    audit_log = Column(JSON, default=list)  # Append-only structured decision traces
    created_at = Column(DateTime, default=datetime.utcnow)

    event = relationship("PaymentEvent")
    actions = relationship("RecoveryAction", back_populates="case")


class RecoveryAction(Base):
    __tablename__ = "recovery_actions"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id = Column(String, ForeignKey("recovery_cases.id"), nullable=False)
    action_type = Column(Enum(ActionType), nullable=False)
    proposed_by = Column(String, nullable=False)
    state = Column(Enum(ActionState), default=ActionState.PROPOSED)
    authorization_token = Column(String, nullable=True)
    action_id = Column(String, nullable=True)
    execution_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    case = relationship("RecoveryCase", back_populates="actions")


# Centralized State Machine Transitions Logic
class CaseStateMachine:
    # Allowable transitions: current -> set of allowed next statuses
    VALID_TRANSITIONS = {
        CaseStatus.IDENTIFIED: {
            CaseStatus.ANALYZING,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.ANALYZING: {
            CaseStatus.ACTION_PROPOSED,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.ACTION_PROPOSED: {
            CaseStatus.GUARD_REVIEW,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.GUARD_REVIEW: {
            CaseStatus.APPROVED,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.APPROVED: {
            CaseStatus.EXECUTING,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.EXECUTING: {
            CaseStatus.RECOVERED,
            CaseStatus.FAILED,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        # Terminal states (cannot transition back to active executing flows)
        CaseStatus.RECOVERED: set(),
        CaseStatus.FAILED: set(),
        CaseStatus.BLOCKED: set(),
        CaseStatus.HUMAN_REVIEW: set(),
    }

    @classmethod
    def validate_transition(
        cls, current_status: CaseStatus, target_status: CaseStatus
    ) -> bool:
        if current_status == target_status:
            return True
        allowed = cls.VALID_TRANSITIONS.get(current_status, set())
        return target_status in allowed
