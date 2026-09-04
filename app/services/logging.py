import logging
import json
import sys
from datetime import datetime

class StructuredLogger:
    def __init__(self, name: str):
        self.logger = logging.getLogger(name)
        # Use stdout for logs
        if not self.logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter('%(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
            
    def info(self, message: str, **kwargs):
        log_payload = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": "INFO",
            "message": message,
            **kwargs
        }
        self.logger.info(json.dumps(log_payload))

    def error(self, message: str, **kwargs):
        log_payload = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": "ERROR",
            "message": message,
            **kwargs
        }
        self.logger.error(json.dumps(log_payload))

logger = StructuredLogger("recoverai.structured")
