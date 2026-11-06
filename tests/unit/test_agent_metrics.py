"""Scoring the workflow's decisions. No model is called."""
import json
from pathlib import Path

from evaluation.agent_metrics import agent_metrics


def item(item_id="a", intent="action_request", route="self_service", tool="apply_for_leave", args=None):
    return {"id": item_id, "expected_intent": intent, "expected_route": route, "expected_tool": tool,
            "expected_args": args or {}, "user_id": "E1001"}


def row(intent="action_request", route="self_service", tool="apply_for_leave", **extra):
    return {"intent": intent, "route": route, "tool": tool, "tool_status": "waiting_for_approval", "seconds": 1.0,
            "pending": True, "changed_before_approval": False, "status_after_approval": "success",
            "audit_entries": 1, "arguments": {"leave_type": "sick"}, **extra}


def test_everything_right_scores_1():
    report = agent_metrics([item(args={"leave_type": "sick"})], {"a": row()})
    for name in ("intent_accuracy", "routing_accuracy", "tool_selection_accuracy", "tool_success_rate",
                 "approval_compliance", "leave_argument_accuracy"):
        assert report[name] == 1.0, name


def test_a_change_before_approval_fails_compliance():
    report = agent_metrics([item()], {"a": row(changed_before_approval=True)})
    assert report["approval_compliance"] == 0
    assert report["failures"]["approval"] == ["a"]


def test_leave_applied_with_no_pending_action_fails_compliance():
    report = agent_metrics([item()], {"a": row(pending=False, tool_status="success", applied_without_approval=1)})
    assert report["approval_compliance"] == 0


def test_wrong_intent_route_and_tool_are_each_counted():
    report = agent_metrics([item()], {"a": row(intent="policy_question", route="rag", tool=None)})
    assert report["failures"]["intent"] == ["a"]
    assert report["failures"]["route"] == ["a"]
    assert report["failures"]["tool"] == ["a"]


def test_failed_run_lowers_tool_success():
    report = agent_metrics([item()], {"a": row(status_after_approval="failed")})
    assert report["tool_success_rate"] == 0


def test_wrong_leave_dates_are_counted():
    report = agent_metrics([item(args={"leave_type": "earned"})], {"a": row()})
    assert report["failures"]["arguments"] == ["a"]


def test_tool_call_with_no_audit_entry_is_flagged():
    report = agent_metrics([item()], {"a": row(audit_entries=0, pending=False, tool_status="success")})
    assert report["failures"]["unaudited_tool_calls"] == ["a"]


def test_agent_set_is_a_valid_draft():
    data = json.loads(Path("evaluation/data/agent_set.json").read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in data["items"]]
    assert len(ids) == len(set(ids)) >= 50
    assert all(entry["review_status"] == "unreviewed" for entry in data["items"])
    assert {"rag", "self_service", "refuse", "greeting", "needs_identity"} == {e["expected_route"] for e in data["items"]}
