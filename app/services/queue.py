import os
import json
import redis
from typing import Optional, Dict, Any

from urllib.parse import urlparse
from app.core.config import settings

redis_url = settings.REDIS_URL
parsed = urlparse(redis_url)
REDIS_HOST = parsed.hostname or "localhost"
REDIS_PORT = parsed.port or 6379

class RedisQueue:
    QUEUE_KEY = "recoverai_task_queue"

    def __init__(self):
        self.client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

    def enqueue(self, task_name: str, payload: Dict[str, Any]):
        message = json.dumps({"task_name": task_name, "payload": payload})
        self.client.rpush(self.QUEUE_KEY, message)

    def dequeue(self, timeout: int = 5) -> Optional[Dict[str, Any]]:
        msg = self.client.blpop(self.QUEUE_KEY, timeout=timeout)
        if msg:
            return json.loads(msg[1])
        return None

class RedisScheduler:
    SCHEDULER_KEY = "recoverai_scheduled_tasks"

    def __init__(self):
        self.client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

    def schedule(self, task_name: str, payload: Dict[str, Any], execute_at_timestamp: float):
        message = json.dumps({"task_name": task_name, "payload": payload})
        self.client.zadd(self.SCHEDULER_KEY, {message: execute_at_timestamp})

    def poll_due_tasks(self) -> list:
        import time
        now = time.time()
        due_tasks = self.client.zrangebyscore(self.SCHEDULER_KEY, min=0, max=now)
        results = []
        for task in due_tasks:
            if self.client.zrem(self.SCHEDULER_KEY, task) > 0:
                results.append(json.loads(task))
        return results
