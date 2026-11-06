"""A mock HR system with fake employees. Everything here is plain deterministic Python.

A real HRMS would replace this class. The agent only depends on these methods.
"""
import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import Engine, text

from app.config import PROJECT_ROOT

DEFAULT_DATA_FILE = PROJECT_ROOT / "config" / "mock_hrms.json"
LEAVE_TYPES = ("earned", "sick", "casual")
MAX_LEAVE_DAYS = 30


class HRMSRejected(Exception):
    """A business rule said no, so trying again won't help. The message is safe to show the employee."""


class HRMSUnavailable(Exception):
    """A temporary failure that's worth trying again."""


def working_days(start: date, end: date) -> int:
    """Count Monday to Friday. Public holidays aren't considered by this mock."""
    return sum((start + timedelta(days=offset)).weekday() < 5 for offset in range((end - start).days + 1))


class MockHRMS:
    def __init__(self, engine: Engine, tenant_id: str, data_file: Path = DEFAULT_DATA_FILE):
        self.engine = engine
        self.tenant_id = tenant_id
        self.employees = json.loads(data_file.read_text(encoding="utf-8"))["employees"]

    def _employee(self, employee_id: str) -> dict:
        employee = self.employees.get(employee_id)
        if not employee:
            raise HRMSRejected("I couldn't find your employee record. Please contact HR.")
        return employee

    def _days_requested(self, employee_id: str) -> dict[str, int]:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT leave_type, sum(days) AS days FROM hr_leave_requests "
                "WHERE tenant_id = :tenant_id AND employee_id = :employee_id GROUP BY leave_type"
            ), {"tenant_id": self.tenant_id, "employee_id": employee_id}).all()
        return {row.leave_type: int(row.days) for row in rows}

    def leave_balance(self, employee_id: str) -> dict[str, int]:
        employee = self._employee(employee_id)
        requested = self._days_requested(employee_id)
        return {kind: employee["leave_entitlement"][kind] - employee["leave_used"][kind] - requested.get(kind, 0)
                for kind in LEAVE_TYPES}

    def latest_payslip(self, employee_id: str) -> dict:
        return dict(self._employee(employee_id)["latest_payslip"])

    def check_leave(self, employee_id: str, leave_type: str, start: date, end: date, today: date) -> int:
        """Apply the rules without changing anything, and return the working days requested."""
        balance = self.leave_balance(employee_id)
        if leave_type not in balance:
            raise HRMSRejected(f"Leave type must be one of: {', '.join(LEAVE_TYPES)}.")
        if end < start:
            raise HRMSRejected("The end date is before the start date.")
        if start < today:
            raise HRMSRejected("Leave can't start in the past.")
        days = working_days(start, end)
        if days == 0:
            raise HRMSRejected("Those dates don't include a working day.")
        if days > MAX_LEAVE_DAYS:
            raise HRMSRejected(f"One request can't be longer than {MAX_LEAVE_DAYS} working days.")
        if days > balance[leave_type]:
            raise HRMSRejected(f"That needs {days} days of {leave_type} leave, "
                               f"and you have {balance[leave_type]} left.")
        return days

    def apply_for_leave(self, employee_id: str, leave_type: str, start: date, end: date, reason: str,
                        idempotency_key: str, today: date) -> dict:
        """Record the request once. Calling again with the same key changes nothing."""
        with self.engine.begin() as connection:
            existing = connection.execute(text(
                "SELECT id, days FROM hr_leave_requests WHERE tenant_id = :tenant_id AND idempotency_key = :key"
            ), {"tenant_id": self.tenant_id, "key": idempotency_key}).first()
            if existing:
                return {"request_id": existing.id, "days": existing.days}
            days = self.check_leave(employee_id, leave_type, start, end, today)
            row = connection.execute(text(
                "INSERT INTO hr_leave_requests (tenant_id, employee_id, leave_type, start_date, end_date, days, "
                "reason, idempotency_key) VALUES (:tenant_id, :employee_id, :leave_type, :start, :end, :days, "
                ":reason, :key) RETURNING id"
            ), {"tenant_id": self.tenant_id, "employee_id": employee_id, "leave_type": leave_type, "start": start,
                "end": end, "days": days, "reason": reason, "key": idempotency_key}).one()
        return {"request_id": row.id, "days": days}

    def raise_ticket(self, employee_id: str, subject: str, description: str, idempotency_key: str) -> dict:
        self._employee(employee_id)
        with self.engine.begin() as connection:
            row = connection.execute(text(
                "INSERT INTO hr_tickets (tenant_id, employee_id, subject, description, idempotency_key) "
                "VALUES (:tenant_id, :employee_id, :subject, :description, :key) "
                "ON CONFLICT (tenant_id, idempotency_key) DO UPDATE SET subject = hr_tickets.subject RETURNING id"
            ), {"tenant_id": self.tenant_id, "employee_id": employee_id, "subject": subject,
                "description": description, "key": idempotency_key}).one()
        return {"ticket_id": row.id}
