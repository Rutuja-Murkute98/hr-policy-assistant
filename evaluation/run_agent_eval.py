"""Measure the workflow's decisions on the agent test set, with real model calls.

    python -m evaluation.run_agent_eval

It saves after each item, so an interrupted run carries on where it stopped. It uses the
mock HR system's fake employees, and removes what it created.
"""
import json
import time
from datetime import date

from sqlalchemy import text

from app import config
from app.rag.providers import get_chat_model
from app.rag.rerank import get_reranker
from app.rag.service import RagService
from app.rag.store import PolicyIndex, get_database_uri, get_store, run_migrations
from app.workflow import ChatWorkflow, create_checkpointer
from evaluation.agent_metrics import agent_metrics
from evaluation.run_eval import CACHE, PAUSE_SECONDS, REPORTS, call_with_retry, load_json, save_json

AGENT_SET = config.PROJECT_ROOT / "evaluation" / "data" / "agent_set.json"
PROGRESS_FILE = CACHE / "agent_eval.json"
ROUTES = {"greeting": "greeting", "out_of_scope": "refuse", "policy_question": "rag"}


def route_taken(result) -> str:
    if result.abstain_reason == "needs_identity":
        return "needs_identity"
    if result.intent in ("personal_data_request", "action_request"):
        return "self_service"
    return ROUTES.get(result.intent, "unknown")


def count_rows(engine, table: str, key: str | None = None) -> int:
    with engine.connect() as connection:
        query = f"SELECT count(*) FROM {table}"
        if key:
            return connection.execute(text(query + " WHERE idempotency_key = :key"), {"key": key}).scalar() or 0
        return connection.execute(text(query)).scalar() or 0


def run_item(workflow: ChatWorkflow, engine, item: dict) -> dict:
    user = item["user_id"]
    balances_before = workflow.runner.hrms.leave_balance(user) if user else {}
    audit_before = count_rows(engine, "tool_audit_log")
    started = time.perf_counter()
    result = call_with_retry(lambda: workflow.ask(item["message"], user_id=user))
    row = {"intent": result.intent, "route": route_taken(result), "tool": result.tool or None,
           "tool_status": result.tool_status, "answer": result.answer,
           "pending": bool(result.pending_action), "seconds": round(time.perf_counter() - started, 2)}

    with engine.connect() as connection:
        if result.tool:
            call = connection.execute(text(
                "SELECT arguments FROM pending_actions WHERE id = :id"
            ), {"id": (result.pending_action or {}).get("id", "")}).scalar()
            row["arguments"] = call
    if result.pending_action:
        action_id = result.pending_action["id"]
        # The key checks: nothing was done or logged as done before the approval
        row["changed_before_approval"] = (count_rows(engine, "hr_leave_requests", action_id) > 0
                                          or workflow.runner.hrms.leave_balance(user) != balances_before)
        decided = workflow.decide(user, action_id, approve=True)
        row["status_after_approval"] = decided.tool_status
        row["applied_after_approval"] = count_rows(engine, "hr_leave_requests", action_id)
    else:
        row["applied_without_approval"] = count_rows(engine, "hr_leave_requests")
    row["audit_entries"] = count_rows(engine, "tool_audit_log") - audit_before

    with engine.begin() as connection:  # each item starts from the same balances
        connection.execute(text("DELETE FROM hr_leave_requests"))
        connection.execute(text("DELETE FROM hr_tickets"))
    if result.conversation_id:
        workflow.delete_conversation(user, result.conversation_id)
    return row


def main() -> None:
    from app.db import get_engine

    data = json.loads(AGENT_SET.read_text(encoding="utf-8"))
    items = [item for item in data["items"] if item["review_status"] != "rejected"]
    today = date.fromisoformat(data["today"])
    run_migrations()
    uri = get_database_uri()
    engine = get_engine(uri)
    llm = get_chat_model()
    rag = RagService(PolicyIndex(get_store()), llm, get_reranker())
    workflow = ChatWorkflow(rag, llm, engine, create_checkpointer(uri), today=lambda: today)
    saved = load_json(PROGRESS_FILE)
    for number, item in enumerate(items, start=1):
        if item["id"] in saved:
            continue
        saved[item["id"]] = run_item(workflow, engine, item)
        save_json(PROGRESS_FILE, saved)
        print(f"{number}/{len(items)} {item['id']} {saved[item['id']]['intent']} {saved[item['id']]['tool']}", flush=True)
        time.sleep(PAUSE_SECONDS)
    report = {"review": {"unreviewed": sum(i["review_status"] == "unreviewed" for i in items),
                         "approved": sum(i["review_status"] == "approved" for i in items)},
              "chat_model": config.CHAT_MODEL, **agent_metrics(items, saved)}
    save_json(REPORTS / "agent_evaluation.json", report)
    print(json.dumps({key: value for key, value in report.items() if key != "failures"}, indent=1))
    print(json.dumps(report["failures"]))
    workflow.close()


if __name__ == "__main__":
    main()
