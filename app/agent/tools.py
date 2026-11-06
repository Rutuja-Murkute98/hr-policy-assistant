"""The self-service tools. The LLM only chooses a tool and its arguments; everything else is deterministic.

The employee is never an argument. It always comes from the signed-in request, so the LLM
can't be talked into looking at someone else's record.
"""
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Engine, text

from app import config
from app.agent.hrms import HRMSRejected, HRMSUnavailable, MockHRMS


@dataclass
class ToolContext:
    tenant_id: str
    user_id: str
    conversation_id: str | None
    run_id: str
    today: date


class NoArguments(BaseModel):
    pass


class ApplyForLeave(BaseModel):
    leave_type: str = Field(description="One of: earned, sick, casual")
    start_date: date = Field(description="The first day of leave, as YYYY-MM-DD")
    end_date: date = Field(description="The last day of leave, as YYYY-MM-DD")
    reason: str = Field(default="", max_length=500, description="The reason, if the employee gave one")


class RaiseTicket(BaseModel):
    subject: str = Field(min_length=3, max_length=120, description="A short title for the request")
    description: str = Field(min_length=3, max_length=2000, description="What the employee needs")


@dataclass
class ToolSpec:
    name: str
    description: str
    arguments: type[BaseModel]
    requires_approval: bool
    run: Callable[[MockHRMS, ToolContext, BaseModel, str], dict]
    describe: Callable[[BaseModel], str]
    report: Callable[[dict, BaseModel], str]
    changes_records: bool = False


def run_leave_balance(hrms: MockHRMS, context: ToolContext, args: BaseModel, key: str) -> dict:
    return hrms.leave_balance(context.user_id)


def report_leave_balance(result: dict, args: BaseModel) -> str:
    return (f"Your leave balance: {result['earned']} earned, {result['sick']} sick and {result['casual']} casual days."
            " This is from the mock HR system.")


def run_payslip(hrms: MockHRMS, context: ToolContext, args: BaseModel, key: str) -> dict:
    return hrms.latest_payslip(context.user_id)


def report_payslip(result: dict, args: BaseModel) -> str:
    return (f"Your {result['month']} payslip: gross Rs. {result['gross']:,}, deductions Rs. {result['deductions']:,}, "
            f"net Rs. {result['net']:,}, credited on {result['credited_on']}. This is from the mock HR system.")


def run_apply_for_leave(hrms: MockHRMS, context: ToolContext, args: BaseModel, key: str) -> dict:
    assert isinstance(args, ApplyForLeave)
    return hrms.apply_for_leave(context.user_id, args.leave_type, args.start_date, args.end_date, args.reason,
                                key, context.today)


def describe_leave(args: BaseModel) -> str:
    assert isinstance(args, ApplyForLeave)
    return f"apply for {args.leave_type} leave from {args.start_date} to {args.end_date}"


def report_leave(result: dict, args: BaseModel) -> str:
    assert isinstance(args, ApplyForLeave)
    return (f"Your {args.leave_type} leave from {args.start_date} to {args.end_date} ({result['days']} working days) "
            f"has been submitted as request {result['request_id']}. This is in the mock HR system.")


def run_ticket(hrms: MockHRMS, context: ToolContext, args: BaseModel, key: str) -> dict:
    assert isinstance(args, RaiseTicket)
    return hrms.raise_ticket(context.user_id, args.subject, args.description, key)


def report_ticket(result: dict, args: BaseModel) -> str:
    assert isinstance(args, RaiseTicket)
    return f"I've raised HR ticket {result['ticket_id']}: {args.subject}. This is in the mock HR system."


TOOLS = {
    "get_leave_balance": ToolSpec(
        "get_leave_balance", "Get the signed-in employee's remaining leave days.", NoArguments, False,
        run_leave_balance, lambda args: "check your leave balance", report_leave_balance),
    "get_latest_payslip": ToolSpec(
        "get_latest_payslip", "Get a summary of the signed-in employee's latest payslip.", NoArguments, False,
        run_payslip, lambda args: "check your latest payslip", report_payslip),
    "apply_for_leave": ToolSpec(
        "apply_for_leave", "Apply for leave for the signed-in employee. The employee must approve it first.",
        ApplyForLeave, True, run_apply_for_leave, describe_leave, report_leave, changes_records=True),
    "raise_hr_ticket": ToolSpec(
        "raise_hr_ticket", "Raise a ticket with the HR team for the signed-in employee.", RaiseTicket, False,
        run_ticket, lambda args: "raise an HR ticket", report_ticket, changes_records=True),
}


def langchain_tools() -> list[StructuredTool]:
    """Describe the tools for the LLM. It can only ask for them: this code is what runs them."""
    def never_called(**kwargs) -> None:
        raise RuntimeError("Tools run through ToolRunner, not through the LLM")

    return [StructuredTool.from_function(func=never_called, name=spec.name, description=spec.description,
                                         args_schema=spec.arguments) for spec in TOOLS.values()]


@dataclass
class ToolOutcome:
    status: str  # success, rejected, invalid_arguments, unknown_tool, failed, blocked
    message: str


class ToolRunner:
    """Run tools with validation, retries with backoff, approval checks and an audit entry for every call."""

    def __init__(self, engine: Engine, hrms: MockHRMS, sleep: Callable[[float], None] = time.sleep):
        self.engine = engine
        self.hrms = hrms
        self.sleep = sleep

    def _audit(self, context: ToolContext, tool: str, arguments: dict, status: str, attempts: int,
               approval_id: str | None, started: float) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO tool_audit_log (tenant_id, user_id, conversation_id, run_id, tool, arguments, status, "
                "attempts, approval_id, duration_ms) VALUES (:tenant_id, :user_id, :conversation_id, :run_id, :tool, "
                "CAST(:arguments AS jsonb), :status, :attempts, :approval_id, :duration_ms)"
            ), {"tenant_id": context.tenant_id, "user_id": context.user_id, "conversation_id": context.conversation_id,
                "run_id": context.run_id, "tool": tool, "arguments": json.dumps(arguments, default=str),
                "status": status, "attempts": attempts, "approval_id": approval_id,
                "duration_ms": int((time.perf_counter() - started) * 1000)})

    def _approved(self, context: ToolContext, approval_id: str | None) -> bool:
        """Read the decision from the database, so nothing but a recorded approval can allow the action."""
        if not approval_id:
            return False
        with self.engine.connect() as connection:
            status = connection.execute(text(
                "SELECT status FROM pending_actions WHERE id = :id AND tenant_id = :tenant_id AND user_id = :user_id"
            ), {"id": approval_id, "tenant_id": context.tenant_id, "user_id": context.user_id}).scalar()
        return status == "approved"

    def execute(self, context: ToolContext, tool: str, arguments: dict, approval_id: str | None = None) -> ToolOutcome:
        started = time.perf_counter()
        spec = TOOLS.get(tool)
        if not spec:
            self._audit(context, tool, arguments, "unknown_tool", 0, approval_id, started)
            return ToolOutcome("unknown_tool", "I can't do that yet. Please contact HR.")
        try:
            args = spec.arguments(**arguments)
        except ValidationError:
            self._audit(context, tool, arguments, "invalid_arguments", 0, approval_id, started)
            return ToolOutcome("invalid_arguments", "I need a few more details to do that. Could you give them again?")
        if spec.requires_approval and not self._approved(context, approval_id):
            self._audit(context, tool, arguments, "blocked", 0, approval_id, started)
            return ToolOutcome("blocked", "That needs your approval first, so nothing was changed.")

        key = approval_id or f"{context.run_id}:{tool}"  # a retry can't repeat the change
        for attempt in range(1, config.TOOL_MAX_ATTEMPTS + 1):
            try:
                result = spec.run(self.hrms, context, args, key)
            except HRMSRejected as rejection:
                self._audit(context, tool, arguments, "rejected", attempt, approval_id, started)
                return ToolOutcome("rejected", str(rejection))
            except HRMSUnavailable:
                if attempt < config.TOOL_MAX_ATTEMPTS:
                    self.sleep(config.TOOL_BACKOFF_SECONDS * 2 ** (attempt - 1))
                    continue
                self._audit(context, tool, arguments, "failed", attempt, approval_id, started)
                return ToolOutcome("failed", "The HR system isn't available right now. Please try again later.")
            self._audit(context, tool, arguments, "success", attempt, approval_id, started)
            return ToolOutcome("success", spec.report(result, args))
        raise AssertionError("unreachable")

    def prepare_approval(self, context: ToolContext, tool: str, arguments: dict) -> tuple[str, str]:
        """Check the request and record it as pending. Returns (action id, what will be done).

        Raises ValidationError or HRMSRejected if it could never succeed, so nobody approves it for nothing.
        """
        spec = TOOLS[tool]
        args = spec.arguments(**arguments)
        if isinstance(args, ApplyForLeave):
            self.hrms.check_leave(context.user_id, args.leave_type, args.start_date, args.end_date, context.today)
        action_id = uuid.uuid4().hex
        summary = spec.describe(args)
        expires = datetime.now(UTC) + timedelta(hours=config.APPROVAL_EXPIRY_HOURS)
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO pending_actions (id, tenant_id, user_id, conversation_id, run_id, tool, arguments, "
                "summary, expires_at) VALUES (:id, :tenant_id, :user_id, :conversation_id, :run_id, :tool, "
                "CAST(:arguments AS jsonb), :summary, :expires)"
            ), {"id": action_id, "tenant_id": context.tenant_id, "user_id": context.user_id,
                "conversation_id": context.conversation_id, "run_id": context.run_id, "tool": tool,
                "arguments": json.dumps(arguments, default=str), "summary": summary, "expires": expires})
        return action_id, summary


class ActionNotFound(Exception):
    """The action doesn't exist or belongs to someone else. Callers can't tell which."""


class ActionNotPending(Exception):
    """The action was already decided, or it expired."""


class PendingActions:
    def __init__(self, engine: Engine, tenant_id: str):
        self.engine = engine
        self.tenant_id = tenant_id

    def list_pending(self, user_id: str) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT id, tool, summary, expires_at FROM pending_actions WHERE tenant_id = :tenant_id "
                "AND user_id = :user_id AND status = 'pending' AND expires_at > now() ORDER BY created_at"
            ), {"tenant_id": self.tenant_id, "user_id": user_id}).all()
        return [{"id": row.id, "tool": row.tool, "summary": row.summary, "expires_at": row.expires_at.isoformat()}
                for row in rows]

    def decide(self, user_id: str, action_id: str, approve: bool) -> dict:
        """Record the decision once, atomically, so two clicks can't both act. Returns the action."""
        owner = {"id": action_id, "tenant_id": self.tenant_id, "user_id": user_id}
        status = "approved" if approve else "rejected"
        with self.engine.begin() as connection:
            row = connection.execute(text(
                "UPDATE pending_actions SET status = :status, decided_at = now() WHERE id = :id "
                "AND tenant_id = :tenant_id AND user_id = :user_id AND status = 'pending' AND expires_at > now() "
                "RETURNING run_id, conversation_id, tool, arguments"
            ), {**owner, "status": status}).first()
            if row:
                return {"run_id": row.run_id, "conversation_id": row.conversation_id, "tool": row.tool,
                        "arguments": row.arguments, "status": status}
            exists = connection.execute(text(
                "SELECT 1 FROM pending_actions WHERE id = :id AND tenant_id = :tenant_id AND user_id = :user_id"
            ), owner).first()
        raise ActionNotPending(action_id) if exists else ActionNotFound(action_id)

    def expire_old(self) -> list[str]:
        """Mark passed deadlines as expired, and return their paused workflow runs."""
        with self.engine.begin() as connection:
            rows = connection.execute(text(
                "UPDATE pending_actions SET status = 'expired' WHERE tenant_id = :tenant_id "
                "AND status = 'pending' AND expires_at <= now() RETURNING run_id"
            ), {"tenant_id": self.tenant_id}).all()
        return [row.run_id for row in rows]

    def cancel_for_conversations(self, user_id: str, conversation_id: str | None = None) -> list[str]:
        """Cancel a deleted conversation's waiting actions, and return their paused workflow runs."""
        clause = "AND conversation_id = :conversation_id" if conversation_id else ""
        with self.engine.begin() as connection:
            rows = connection.execute(text(
                "UPDATE pending_actions SET status = 'cancelled' WHERE tenant_id = :tenant_id AND user_id = :user_id "
                f"AND status = 'pending' {clause} RETURNING run_id"
            ), {"tenant_id": self.tenant_id, "user_id": user_id, "conversation_id": conversation_id}).all()
        return [row.run_id for row in rows]
