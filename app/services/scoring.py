from decimal import Decimal


def get_currency_multiplier(currency: str) -> Decimal:
    """Normalizes amounts to INR. Assumes $1 USD = ₹83.00 INR for hackathon purposes."""
    if not currency:
        return Decimal("1.00")
    curr = currency.upper().strip()
    if curr == "USD":
        return Decimal("83.00")
    return Decimal("1.00")


def get_failure_probability(failure_code: str) -> float:
    """Domain-consistent taxonomy base probabilities."""
    code_weights = {
        # Exogenous / Technical failures (high inherit recoverability, less customer-dependent)
        "bank_timeout": 0.85,
        "network_error": 0.80,
        "authentication_failed": 0.50,
        "payment_timeout": 0.60,
        "session_expired": 0.40,
        # Customer-driven failures (low base prob, heavily customer-dependent)
        "insufficient_funds": 0.40,
        "card_expired": 0.15,
        "card_declined": 0.25,
        "payment_method_invalid": 0.10,
        "user_abandoned": 0.35,
        "payment_method_error": 0.55,
        "payment_method_expired": 0.15,
        "recurring_payment_failed": 0.50,
        "overdue": 0.60,
        "partially_paid": 0.75,
        "payment_promise_broken": 0.50,
        "disputed": 0.20,
    }
    return code_weights.get(failure_code.lower() if failure_code else "", 0.35)


def calculate_recoverability_probability(
    failure_code: str, history_success_rate: float, attempt: int
) -> float:
    """
    Consolidated single authoritative calculation mapping recoverability probability.
    Applies exogenous vs customer-driven failure adjustments and retry attempts decay factors.
    """
    base_prob = get_failure_probability(failure_code)

    exogenous_failures = {
        "bank_timeout",
        "network_error",
        "payment_timeout",
        "session_expired",
    }
    code_lower = failure_code.lower() if failure_code else ""

    if code_lower in exogenous_failures:
        # Exogenous failures bypass major history success rate discounts
        adjusted_prob = base_prob * (0.8 + 0.2 * history_success_rate)
    else:
        # Customer-driven failures are highly correlated to user profile history rates
        adjusted_prob = base_prob * history_success_rate

    decay_factor = 0.85**attempt
    final_prob = max(0.01, min(0.99, adjusted_prob * decay_factor))
    return round(final_prob, 4)


def calculate_erv(
    amount: Decimal,
    currency: str,
    failure_code: str,
    history_success_rate: float,
    attempt: int,
) -> Decimal:
    """
    Calculates Expected Recovery Value (ERV) = Consolidated Recoverability Probability * Local Amount.
    Returned in local currency as a Decimal.
    """
    prob = calculate_recoverability_probability(
        failure_code, history_success_rate, attempt
    )
    return (Decimal(str(amount)) * Decimal(str(prob))).quantize(Decimal("1.00"))


def calculate_priority_score(
    amount: Decimal,
    currency: str,
    failure_code: str,
    history_success_rate: float,
    attempt: int,
    urgency_factor: float = 1.0,
) -> int:
    """
    Computes a score from 0-100 to determine routing urgency.
    Decoupled from ERV: relies on Normalized Amount (in INR), Probability, and Urgency metrics.
    """
    # 1. Normalize amount to INR
    norm_multiplier = get_currency_multiplier(currency)
    amount_inr = Decimal(str(amount)) * norm_multiplier

    # 2. Financial exposure component (Capped at 1,00,000 INR for scoring variance)
    norm_amount_score = min(
        Decimal("40.00"), (amount_inr / Decimal("100000.00")) * Decimal("40.00")
    )

    # 3. Consumed consolidated probability component
    prob = calculate_recoverability_probability(
        failure_code, history_success_rate, attempt
    )
    prob_score = Decimal(str(prob)) * Decimal("30.00")

    # 4. Urgency/Age of receivable component
    urgency_score = min(
        Decimal("30.00"), Decimal(str(urgency_factor)) * Decimal("20.00")
    )

    total_score = norm_amount_score + prob_score + urgency_score
    return int(min(100, max(0, total_score)))
