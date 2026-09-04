import unittest
from decimal import Decimal
from app.services.gateway import SimulatedPaymentGateway, RazorpayPaymentGateway

class TestPaymentGatewayAbstractions(unittest.TestCase):

    def test_simulated_gateway_do_nothing(self):
        gateway = SimulatedPaymentGateway()
        res = gateway.execute_action(
            action_type="DO_NOTHING",
            amount=Decimal("100.00"),
            currency="INR",
            case_id="case-1",
            event_id="evt-1",
            action_id="act-1"
        )
        self.assertTrue(res.success)
        self.assertFalse(res.recovered)
        self.assertEqual(res.result_code, "NO_OP")
        self.assertEqual(res.recovered_amount, Decimal("0.00"))

    def test_simulated_gateway_success(self):
        gateway = SimulatedPaymentGateway()
        res = gateway.execute_action(
            action_type="RETRY_PAYMENT",
            amount=Decimal("500.00"),
            currency="INR",
            case_id="case-2",
            event_id="evt-2",
            action_id="act-2",
            ground_truth={"action_success": True, "recovered_amount": Decimal("500.00")}
        )
        self.assertTrue(res.success)
        self.assertTrue(res.recovered)
        self.assertEqual(res.result_code, "SUCCESS")
        self.assertEqual(res.recovered_amount, Decimal("500.00"))

    def test_razorpay_gateway_placeholder(self):
        gateway = RazorpayPaymentGateway()
        res = gateway.execute_action(
            action_type="RETRY_PAYMENT",
            amount=Decimal("500.00"),
            currency="INR",
            case_id="case-3",
            event_id="evt-3",
            action_id="act-3"
        )
        self.assertFalse(res.success)
        self.assertFalse(res.recovered)
        self.assertEqual(res.result_code, "UNCONFIGURED")

if __name__ == "__main__":
    unittest.main()
