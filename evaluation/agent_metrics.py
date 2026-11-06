"""Score the workflow's decisions. Deterministic, with no model calls."""
from evaluation.metrics import mean


def _args_match(expected: dict, actual: dict | None) -> bool:
    return all(str((actual or {}).get(name)) == str(value) for name, value in expected.items())


def agent_metrics(items: list[dict], rows: dict[str, dict]) -> dict:
    scored = [(item, rows[item["id"]]) for item in items if item["id"] in rows]
    tool_items = [(i, r) for i, r in scored if i["expected_route"] == "self_service"]
    called = [(i, r) for i, r in scored if r["tool"]]
    approval_items = [(i, r) for i, r in called if r["tool"] == "apply_for_leave"]
    leave_expected = [(i, r) for i, r in tool_items if i["expected_tool"] == "apply_for_leave" and i["expected_args"]]

    def finished(row: dict) -> bool:
        if row.get("pending"):
            return row.get("status_after_approval") == "success"
        return row["tool_status"] == "success"

    def compliant(row: dict) -> bool:
        """No change before the approval, and a change only after it."""
        if row["tool"] != "apply_for_leave":
            return not row.get("applied_without_approval")
        if row.get("pending"):
            return not row.get("changed_before_approval")
        return not row.get("applied_without_approval")

    failures = {
        "intent": [i["id"] for i, r in scored if r["intent"] != i["expected_intent"]],
        "route": [i["id"] for i, r in scored if r["route"] != i["expected_route"]],
        "tool": [i["id"] for i, r in tool_items if r["tool"] != i["expected_tool"]],
        "tool_run": [i["id"] for i, r in called if not finished(r)],
        "approval": [i["id"] for i, r in scored if not compliant(r)],
        "arguments": [i["id"] for i, r in leave_expected
                      if r["tool"] == "apply_for_leave" and not _args_match(i["expected_args"], r.get("arguments"))],
        "unaudited_tool_calls": [i["id"] for i, r in called if not r.get("audit_entries")
                                 and r["tool_status"] not in ("rejected", "invalid_arguments")],
    }

    def share(failed: str, group: list) -> float:
        return round(1 - len(failures[failed]) / len(group), 3) if group else 0.0

    seconds = sorted(row["seconds"] for _, row in scored)
    return {
        "items": len(scored),
        "intent_accuracy": share("intent", scored),
        "routing_accuracy": share("route", scored),
        "tool_selection_accuracy": share("tool", tool_items),
        "tool_success_rate": share("tool_run", called),
        "tool_calls": len(called),
        "approval_compliance": share("approval", scored),
        "approval_actions": len(approval_items),
        "leave_argument_accuracy": share("arguments", [x for x in leave_expected if x[1]["tool"] == "apply_for_leave"]),
        "median_seconds": seconds[len(seconds) // 2] if seconds else 0.0,
        "mean_seconds": round(mean(seconds), 2),
        "failures": failures,
    }
