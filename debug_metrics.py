import sys
import os

# Add the backend directory to sys.path so we can import from app
sys.path.insert(0, "/Users/vaaheesan/Desktop/RazorPay/backend")

from app.db.session import SessionLocal
from app.api.endpoints import get_metrics

db = SessionLocal()
try:
    metrics = get_metrics(db=db, merchant_id=None)
    print("Success:", metrics)
except Exception as e:
    import traceback
    traceback.print_exc()
finally:
    db.close()
