import unittest
import time
from app.services.queue import RedisQueue, RedisScheduler

class TestRedisSchedulerAndQueue(unittest.TestCase):

    def setUp(self):
        self.queue = RedisQueue()
        self.scheduler = RedisScheduler()
        self.queue.client.delete(self.queue.QUEUE_KEY)
        self.scheduler.client.delete(self.scheduler.SCHEDULER_KEY)

    def test_queue_enqueue_dequeue(self):
        payload = {"case_id": "test-q-123"}
        self.queue.enqueue("evaluate_case", payload)

        task = self.queue.dequeue(timeout=2)
        self.assertIsNotNone(task)
        self.assertEqual(task["task_name"], "evaluate_case")
        self.assertEqual(task["payload"]["case_id"], "test-q-123")

    def test_scheduler_delay(self):
        payload = {"case_id": "test-s-456"}
        execute_at = time.time() + 1.5  # 1.5 seconds in the future
        self.scheduler.schedule("evaluate_case", payload, execute_at)

        # Poll immediately (should be empty because execution time has not passed)
        due_now = self.scheduler.poll_due_tasks()
        self.assertEqual(len(due_now), 0)

        # Wait 2 seconds
        time.sleep(2.0)

        # Poll again (should contain the scheduled task)
        due_after = self.scheduler.poll_due_tasks()
        self.assertEqual(len(due_after), 1)
        self.assertEqual(due_after[0]["task_name"], "evaluate_case")
        self.assertEqual(due_after[0]["payload"]["case_id"], "test-s-456")

if __name__ == "__main__":
    unittest.main()
