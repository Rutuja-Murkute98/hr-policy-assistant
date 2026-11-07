"""Behave like employees using the helpdesk. Set HELPDESK_API_KEY, and run with loadtest/run_steps.py or Locust itself.

Installed separately: pip install -r requirements-loadtest.txt
"""
import json
import os
import random
import uuid
from pathlib import Path

from locust import HttpUser, between, task

GOLDEN = Path(__file__).resolve().parent.parent / "evaluation" / "data" / "golden_set.json"
QUESTIONS = [item["question"] for item in json.loads(GOLDEN.read_text(encoding="utf-8"))["items"]
             if not item.get("history")]
EMPLOYEES = ["E1001", "E1002", "E1003"]
MIN_WAIT = float(os.getenv("LOADTEST_MIN_WAIT", "2"))
MAX_WAIT = float(os.getenv("LOADTEST_MAX_WAIT", "6"))


class Employee(HttpUser):
    wait_time = between(MIN_WAIT, MAX_WAIT)

    def on_start(self) -> None:
        self.client.headers.update({"X-API-Key": os.environ["HELPDESK_API_KEY"], "X-Client-Id": uuid.uuid4().hex,
                                    "X-User-Id": random.choice(EMPLOYEES)})

    @task(10)
    def ask(self) -> None:
        self.client.post("/ask", json={"question": random.choice(QUESTIONS)}, name="/ask", timeout=120)

    @task(1)
    def list_chats(self) -> None:
        self.client.get("/conversations", name="/conversations", timeout=60)

    @task(1)
    def health(self) -> None:
        self.client.get("/health", name="/health", timeout=60)
