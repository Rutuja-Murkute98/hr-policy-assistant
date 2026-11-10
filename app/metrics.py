"""Measure each answer and report on it. A row holds counts and times, never question or answer text.

Kept in PostgreSQL because the free stack has no metrics service, a free server's memory is lost when it
sleeps, and each organisation should only see its own numbers.
"""
import json
import logging
from dataclasses import asdict, dataclass, field

from sqlalchemy import Engine, text

from app.tenancy import TenantEngine, tenant_scoped

logger = logging.getLogger("hr_helpdesk")


@dataclass
class AnswerMetrics:
    kind: str  # "question" or "decision"
    total_ms: float
    failed: bool = False
    intent: str = ""
    answered: bool = False
    abstain_reason: str = ""
    tool: str = ""
    tool_status: str = ""
    embedding_ms: float = 0.0
    retrieval_ms: float = 0.0
    llm_ms: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    step_ms: dict = field(default_factory=dict)  # milliseconds per workflow step


# The steps whose timings are summarised. Others are still stored.
TIMED_STEPS = ("understand", "confirm_out_of_scope", "retrieve", "rewrite_query", "generate", "select_tool")
INSERT = text(
    "INSERT INTO answer_metrics (tenant_id, kind, total_ms, failed, intent, answered, abstain_reason, tool, "
    "tool_status, embedding_ms, retrieval_ms, llm_ms, llm_calls, input_tokens, output_tokens, estimated_cost_usd, "
    "step_ms) "
    "VALUES (:tenant_id, :kind, :total_ms, :failed, :intent, :answered, :abstain_reason, :tool, :tool_status, "
    ":embedding_ms, :retrieval_ms, :llm_ms, :llm_calls, :input_tokens, :output_tokens, :estimated_cost_usd, "
    "CAST(:step_ms AS jsonb))"
)
SUMMARY = text(
    "SELECT count(*) AS requests, count(*) FILTER (WHERE failed) AS failures, "
    "count(*) FILTER (WHERE NOT failed AND NOT answered AND kind = 'question') AS refusals, "
    "percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms) FILTER (WHERE NOT failed) AS total_p50, "
    "percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) FILTER (WHERE NOT failed) AS total_p95, "
    "percentile_cont(0.5) WITHIN GROUP (ORDER BY retrieval_ms) FILTER (WHERE retrieval_ms > 0) AS retrieval_p50, "
    "percentile_cont(0.95) WITHIN GROUP (ORDER BY retrieval_ms) FILTER (WHERE retrieval_ms > 0) AS retrieval_p95, "
    "percentile_cont(0.5) WITHIN GROUP (ORDER BY embedding_ms) FILTER (WHERE embedding_ms > 0) AS embedding_p50, "
    "percentile_cont(0.5) WITHIN GROUP (ORDER BY llm_ms) FILTER (WHERE llm_calls > 0) AS llm_p50, "
    "percentile_cont(0.95) WITHIN GROUP (ORDER BY llm_ms) FILTER (WHERE llm_calls > 0) AS llm_p95, "
    "COALESCE(sum(llm_calls), 0) AS llm_calls, COALESCE(sum(input_tokens), 0) AS input_tokens, "
    "COALESCE(sum(output_tokens), 0) AS output_tokens, COALESCE(sum(estimated_cost_usd), 0) AS estimated_cost_usd, "
    "count(*) FILTER (WHERE tool <> '') AS tool_calls, "
    "count(*) FILTER (WHERE tool_status IN ('failed', 'blocked', 'unknown_tool')) AS tool_failures "
    "FROM answer_metrics WHERE tenant_id = :tenant_id AND created_at >= now() - make_interval(days => :days)"
)
STEPS = text(
    "SELECT " + ", ".join(
        f"percentile_cont(0.5) WITHIN GROUP (ORDER BY (step_ms->>'{step}')::float) "
        f"FILTER (WHERE step_ms ? '{step}') AS {step}_p50, "
        f"percentile_cont(0.95) WITHIN GROUP (ORDER BY (step_ms->>'{step}')::float) "
        f"FILTER (WHERE step_ms ? '{step}') AS {step}_p95, "
        f"count(*) FILTER (WHERE step_ms ? '{step}') AS {step}_count" for step in TIMED_STEPS)
    + " FROM answer_metrics WHERE tenant_id = :tenant_id AND NOT failed "
    "AND created_at >= now() - make_interval(days => :days)"
)
FEEDBACK = text(
    "SELECT count(*) FILTER (WHERE rating > 0) AS up, count(*) FILTER (WHERE rating < 0) AS down FROM feedback "
    "WHERE tenant_id = :tenant_id AND created_at >= now() - make_interval(days => :days)"
)


def _whole(value) -> int | None:
    return None if value is None else round(value)


class MetricsStore:
    def __init__(self, engine: Engine | TenantEngine, tenant_id: str):
        self.engine = tenant_scoped(engine, tenant_id)
        self.tenant_id = tenant_id

    def record(self, metrics: AnswerMetrics) -> None:
        """Save one answer's numbers. A failure here only loses a measurement, never the answer."""
        try:
            with self.engine.begin() as connection:
                connection.execute(INSERT, {**asdict(metrics), "tenant_id": self.tenant_id,
                                            "step_ms": json.dumps(metrics.step_ms)})
        except Exception as error:
            logger.warning("Couldn't save an answer's metrics: %s", type(error).__name__)

    def summary(self, days: int = 7) -> dict:
        scope = {"tenant_id": self.tenant_id, "days": days}
        with self.engine.begin() as connection:
            row = connection.execute(SUMMARY, scope).one()
            steps = connection.execute(STEPS, scope).one()._mapping
            feedback = connection.execute(FEEDBACK, scope).one()
        answered_or_refused = row.requests - row.failures
        return {
            "days": days,
            "requests": row.requests,
            "failures": row.failures,
            "refusals": row.refusals,
            "refusal_rate": round(row.refusals / answered_or_refused, 3) if answered_or_refused else None,
            "latency_ms": {"total_p50": _whole(row.total_p50), "total_p95": _whole(row.total_p95),
                           "embedding_p50": _whole(row.embedding_p50),
                           "retrieval_p50": _whole(row.retrieval_p50), "retrieval_p95": _whole(row.retrieval_p95),
                           "llm_p50": _whole(row.llm_p50), "llm_p95": _whole(row.llm_p95)},
            # Where the time goes, step by step: the slowest step is the one to work on
            "steps_ms": {step: {"p50": _whole(steps[f"{step}_p50"]), "p95": _whole(steps[f"{step}_p95"]),
                                "count": steps[f"{step}_count"]}
                         for step in TIMED_STEPS if steps[f"{step}_count"]},
            "llm_calls": row.llm_calls,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "estimated_cost_usd": round(float(row.estimated_cost_usd), 4),
            "tool_calls": row.tool_calls,
            "tool_failures": row.tool_failures,
            "thumbs_up": feedback.up,
            "thumbs_down": feedback.down,
        }
