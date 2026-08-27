from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum
from decimal import Decimal


class ActionTypeEnum(str, Enum):
    RETRY_PAYMENT = "RETRY_PAYMENT"
    SEND_PAYMENT_REMINDER = "SEND_PAYMENT_REMINDER"
    SEND_CHECKOUT_RECOVERY_MESSAGE = "SEND_CHECKOUT_RECOVERY_MESSAGE"
    RETRY_SUBSCRIPTION = "RETRY_SUBSCRIPTION"
    SEND_INVOICE_REMINDER = "SEND_INVOICE_REMINDER"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    DO_NOTHING = "DO_NOTHING"


class CaseStatusEnum(str, Enum):
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


# Score request and response schemas
class ScoreRequest(BaseModel):
    amount: Decimal = Field(..., ge=0.0, description="The event transaction amount")
    currency: str = Field(
        "INR", min_length=1, description="The event transaction currency code"
    )
    failure_code: str = Field(
        ..., min_length=1, description="Failure code mapping to failure probabilities"
    )
    history_success_rate: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Customer success rate history ratio (0.0 to 1.0)",
    )
    attempt: int = Field(0, ge=0, description="Current retry attempt number count")
    urgency_factor: float = Field(
        1.0, description="Urgency weight factor calculation parameter"
    )

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        val = v.upper().strip()
        if not val:
            raise ValueError("currency must be non-empty")
        return val


class ScoreResponse(BaseModel):
    expected_recovery_value: Decimal
    recoverability_probability: float
    priority_score: int


# Action Guard request and response schemas
class ActionGuardRequest(BaseModel):
    action_type: ActionTypeEnum
    amount: Decimal = Field(..., ge=0.0)
    currency: str = Field("INR", min_length=1)
    current_attempts: int = Field(..., ge=0)
    max_retries: int = Field(..., ge=0)
    amount_threshold: Decimal = Field(Decimal("5000.00"), gt=0.0)
    has_active_action: bool = False
    last_contact_at: Optional[str] = None
    now: Optional[str] = None
    planner_confidence: float = Field(1.0, ge=0.0, le=1.0)

    # Explicit identifiers to separate case/event identifiers from timestamps
    case_id: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    action_id: str = Field(..., min_length=1)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        val = v.upper().strip()
        if not val:
            raise ValueError("currency must be non-empty")
        return val


class ActionGuardResponse(BaseModel):
    approved: bool
    authorization_token: Optional[str] = None
    resulting_status: str
    violations: List[str] = []
    warnings: List[str] = []


# Execute request and response schemas
class ExecuteRequest(BaseModel):
    action_type: ActionTypeEnum
    amount: Decimal = Field(..., ge=0.0)
    currency: str = Field("INR", min_length=1)
    authorization_token: str = Field(..., min_length=1)
    guard_approved: bool
    case_id: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    action_id: str = Field(..., min_length=1)
    ground_truth: Optional[Dict[str, Any]] = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        val = v.upper().strip()
        if not val:
            raise ValueError("currency must be non-empty")
        return val


class ExecuteResponse(BaseModel):
    execution_id: str
    action: str
    status: str
    recovered: bool
    recovered_amount: Decimal
    message: str
    executed_at: str


# Paginated case response schemas
class CaseSummary(BaseModel):
    case_id: str
    event_id: str
    event_type: str
    amount: Decimal
    currency: str
    failure_code: Optional[str]
    status: str
    priority_score: int
    expected_recovery_value: Decimal
    current_recovery_attempt: int
    proposed_action: Optional[str]
    confidence: float
    created_at: datetime

    class Config:
        from_attributes = True


class CaseListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[CaseSummary]


# Detailed case schema
class CaseDetail(BaseModel):
    case_id: str
    event_id: str
    event_type: str
    amount: Decimal
    currency: str
    failure_code: Optional[str]
    status: str
    priority_score: int
    expected_recovery_value: Decimal
    current_recovery_attempt: int
    audit_log: List[Any]
    created_at: datetime
    actions: List[Dict[str, Any]] = []
    audit_events: Optional[List[Dict[str, Any]]] = []

    class Config:
        from_attributes = True


# Metrics response schemas
class MetricsResponse(BaseModel):
    revenue_at_risk: float
    revenue_recovered: float
    recovery_rate: float
    action_success_rate: float
    guard_block_rate: float
    human_escalation_rate: float
    backend_action_agreement: Optional[float] = None
    council_action_agreement: Optional[float] = None


class HumanReviewRequest(BaseModel):
    action: str
    operator_id: str
    notes: Optional[str] = None
