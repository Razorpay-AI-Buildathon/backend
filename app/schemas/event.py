from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any
from decimal import Decimal

class PaymentEventIngest(BaseModel):
    event_id: str = Field(..., min_length=1, description="Unique transaction event identifier")
    merchant_id: str = Field(..., min_length=1, description="Identifier of the merchant")
    customer_id: str = Field(..., min_length=1, description="Identifier of the customer")
    event_type: str = Field(..., min_length=1, description="Type of event e.g. FAILED_PAYMENT, CHECKOUT_ABANDONMENT")
    amount: Decimal = Field(..., ge=0.0, description="The payment transaction amount")
    currency: str = Field("INR", min_length=1, description="The currency code (3 letters)")
    failure_code: Optional[str] = Field(None, description="Optional payment failure code")
    provider: Optional[str] = Field("razorpay", description="The gateway/event provider name")
    provider_event_id: Optional[str] = Field(None, description="Unique reference ID from the payment provider")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Metadata dictionary")

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        val = v.upper().strip()
        if not val:
            raise ValueError("currency must be non-empty")
        return val

class PaymentEventIngestResponse(BaseModel):
    status: str
    message: str
    event_id: str
    case_id: str
