import os
import sys
import logging

logging.basicConfig()
logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)

sys.path.append("/Users/vaaheesan/Desktop/RazorPay/backend")

from app.services.executor import SimulatorGateway
from app.services.worker import RecoveryWorker

gateway = SimulatorGateway()
evt = gateway.ingest_event(amount=200, currency="INR", failure_code="insufficient_funds")
print(f"Created event: {evt.id}, case: {evt.case_id}")

worker = RecoveryWorker()
worker.process_case_evaluation(evt.case_id)
