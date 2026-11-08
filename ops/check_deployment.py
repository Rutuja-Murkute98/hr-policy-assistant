"""Check a running deployment from the outside, the way an employee's screen or the Slack bot would reach it.

    python -m ops.check_deployment --api-url https://your-api.onrender.com --ui-url https://your-app.streamlit.app
    python -m ops.check_deployment --api-url http://localhost:8000 --no-model --allow-empty-index

It checks that the API is up, refuses callers without a key, greets without using the model, answers policy
questions from the golden set with citations, refuses questions it can't answer, and, with a second
organisation's key, that one organisation can't get answers from another's documents. It also measures how long
answers take. Keys are read from the environment so they don't end up in shell history:

    CHECK_API_KEY         a key for the first organisation: an organisation's API key, or the shared demo key
    CHECK_SECOND_API_KEY  optional: a key for a second organisation, for the isolation check

With --no-model, nothing uses the model, so it needs no quota. CI uses that mode against the Compose stack.
It only needs httpx, so it can run from any machine without the rest of the project's packages.
"""
import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

GOLDEN_SET = Path(__file__).resolve().parent.parent / "evaluation" / "data" / "golden_set.json"
CLIENT_ID = "deployment-check"  # counted as one client by the rate limit
NOT_INDEXED = "No policies indexed"
ASK_TIMEOUT_SECONDS = 150  # the chat screen waits as long, and a sleeping free-tier API can take a while to wake


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    seconds: float | None = None


@dataclass
class Report:
    api_url: str
    started_at: str
    checks: list[Check] = field(default_factory=list)
    answer_seconds: list[float] = field(default_factory=list)  # policy answers that succeeded

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        mark = "PASS" if check.passed else "FAIL"
        timing = f" ({check.seconds:.2f} s)" if check.seconds is not None else ""
        print(f"[{mark}] {check.name}{timing}: {check.detail}", flush=True)
        return check

    def summary(self) -> dict:
        return {"api_url": self.api_url, "started_at": self.started_at, "passed": self.passed,
                "checks": [asdict(check) for check in self.checks],
                "answers_timed": len(self.answer_seconds),
                "answer_p50_seconds": percentile(self.answer_seconds, 50),
                "answer_p95_seconds": percentile(self.answer_seconds, 95)}


def percentile(values: list[float], percent: float) -> float | None:
    """The nearest-rank percentile, so small samples give a value that was actually measured."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return round(ordered[rank - 1], 3)


def headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key, "X-Client-Id": CLIENT_ID}


def timed(call):
    started = time.perf_counter()
    response = call()
    return response, time.perf_counter() - started


def ask(client: httpx.Client, api_key: str, question: str) -> tuple[httpx.Response, float]:
    return timed(lambda: client.post("/ask", json={"question": question}, headers=headers(api_key),
                                     timeout=ASK_TIMEOUT_SECONDS))


def check_live(client: httpx.Client) -> Check:
    """The first request also shows how long a sleeping free-tier service takes to wake."""
    try:
        response, seconds = timed(lambda: client.get("/live", timeout=ASK_TIMEOUT_SECONDS))
    except httpx.HTTPError as error:
        return Check("API is up", False, f"no response: {type(error).__name__}")
    ok = response.status_code == 200 and response.json().get("status") == "ok"
    return Check("API is up", ok, f"status {response.status_code}, first response after {seconds:.1f} s", seconds)


def check_health(client: httpx.Client, allow_empty_index: bool) -> Check:
    response = client.get("/health", timeout=60)
    if response.status_code == 200:
        body = response.json()
        return Check("Database and index", body.get("chunks_indexed", 0) > 0,
                     f"{body.get('chunks_indexed')} chunks indexed, AI status {body.get('ai')!r}")
    detail = response.json().get("detail", "") if response.headers.get("content-type", "").startswith(
        "application/json") else response.text[:200]
    if allow_empty_index and response.status_code == 503 and NOT_INDEXED in detail:
        return Check("Database and index", True, "database reached and migrated, no policies indexed yet (allowed)")
    return Check("Database and index", False, f"status {response.status_code}: {detail}")


def check_refuses_without_key(client: httpx.Client) -> Check:
    response = client.post("/ask", json={"question": "How many days of leave do I get?"},
                           headers={"X-Client-Id": CLIENT_ID}, timeout=30)
    return Check("Refuses callers without a key", response.status_code == 401, f"status {response.status_code}")


def check_greeting(client: httpx.Client, api_key: str) -> Check:
    """A greeting goes through sign-in, rate limits, the database and the workflow, but never the model."""
    response, seconds = ask(client, api_key, "hello")
    if response.status_code != 200:
        return Check("Greets without using the model", False, f"status {response.status_code}: {response.text[:200]}",
                     seconds)
    body = response.json()
    ok = bool(body.get("answer")) and not body.get("sources")
    return Check("Greets without using the model", ok, f"intent {body.get('intent')!r}, {len(body.get('answer', ''))} "
                 "characters, no sources" if ok else f"unexpected reply: {str(body)[:200]}", seconds)


def golden_questions(path: Path, category: str, limit: int) -> list[dict]:
    """Single questions with no chat history, in the golden set's own order, so runs are comparable."""
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    chosen = [item for item in items if item["category"] == category and not item.get("history")]
    return chosen[:limit]


def check_policy_answer(client: httpx.Client, api_key: str, item: dict, report: Report) -> Check:
    name = f"Answers {item['id']} with citations"
    try:
        response, seconds = ask(client, api_key, item["question"])
    except httpx.HTTPError as error:
        return Check(name, False, f"no response: {type(error).__name__}")
    if response.status_code != 200:
        return Check(name, False, f"status {response.status_code}: {response.text[:200]}", seconds)
    body = response.json()
    answer = body.get("answer", "")
    facts = [fact for fact in item.get("must_contain", []) if fact.lower() in answer.lower()]
    cited = sorted({source["file"] for source in body.get("sources", [])})
    if cited:
        report.answer_seconds.append(seconds)
    detail = f"cited {', '.join(cited) or 'nothing'}; expected facts found {len(facts)} of " \
             f"{len(item.get('must_contain', []))}"
    return Check(name, bool(cited), detail, seconds)


def check_refusal(client: httpx.Client, api_key: str, item: dict) -> Check:
    name = f"Refuses {item['id']}"
    try:
        response, seconds = ask(client, api_key, item["question"])
    except httpx.HTTPError as error:
        return Check(name, False, f"no response: {type(error).__name__}")
    if response.status_code != 200:
        return Check(name, False, f"status {response.status_code}: {response.text[:200]}", seconds)
    sources = response.json().get("sources", [])
    return Check(name, not sources, "no sources given" if not sources else f"cited {len(sources)} sources", seconds)


def check_isolation(client: httpx.Client, first_key: str, second_key: str, question: str, file_name: str,
                    pause_seconds: float = 0.0) -> list[Check]:
    """The second organisation's document answers the question for it, and never for the first organisation."""
    checks = []
    for label, key, should_cite in (("second organisation", second_key, True),
                                    ("first organisation", first_key, False)):
        name = f"Isolation: {file_name} {'is' if should_cite else 'is not'} cited for the {label}"
        time.sleep(pause_seconds)
        try:
            response, seconds = ask(client, key, question)
        except httpx.HTTPError as error:
            checks.append(Check(name, False, f"no response: {type(error).__name__}"))
            continue
        if response.status_code != 200:
            checks.append(Check(name, False, f"status {response.status_code}: {response.text[:200]}", seconds))
            continue
        cited = {source["file"] for source in response.json().get("sources", [])}
        checks.append(Check(name, (file_name in cited) == should_cite, f"cited {sorted(cited) or 'nothing'}", seconds))
    return checks


def check_ui(ui_url: str) -> Check:
    try:
        response = httpx.get(f"{ui_url.rstrip('/')}/_stcore/health", timeout=ASK_TIMEOUT_SECONDS,
                             follow_redirects=True)
    except httpx.HTTPError as error:
        return Check("Chat screen is up", False, f"no response: {type(error).__name__}")
    ok = response.status_code == 200 and response.text.strip() == "ok"
    return Check("Chat screen is up", ok, f"status {response.status_code}")


def run(client: httpx.Client, api_key: str, *, use_model: bool = True, allow_empty_index: bool = False,
        questions: int = 10, refusals: int = 3, pause_seconds: float = 7.0, second_key: str = "",
        isolation_question: str = "", isolation_file: str = "", ui_url: str = "",
        golden_set: Path = GOLDEN_SET) -> Report:
    report = Report(str(client.base_url), time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    if not report.add(check_live(client)).passed:
        return report  # nothing else can work
    report.add(check_health(client, allow_empty_index))
    report.add(check_refuses_without_key(client))
    report.add(check_greeting(client, api_key))
    if ui_url:
        report.add(check_ui(ui_url))
    if not use_model:
        return report

    # A pause between questions keeps this under the per-client rate limit and the model's free quota
    model_checks = [lambda item=item: check_policy_answer(client, api_key, item, report)
                    for item in golden_questions(golden_set, "normal", questions)]
    model_checks += [lambda item=item: check_refusal(client, api_key, item)
                     for item in golden_questions(golden_set, "unanswerable", refusals)]
    for index, model_check in enumerate(model_checks):
        if index:
            time.sleep(pause_seconds)
        report.add(model_check())
    if second_key and isolation_question and isolation_file:
        for check in check_isolation(client, api_key, second_key, isolation_question, isolation_file, pause_seconds):
            report.add(check)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--ui-url", default="")
    parser.add_argument("--no-model", action="store_true", help="only checks that use no model quota")
    parser.add_argument("--allow-empty-index", action="store_true",
                        help="accept a database with no policies indexed yet, as in CI")
    parser.add_argument("--questions", type=int, default=10, help="answerable golden questions to ask")
    parser.add_argument("--refusals", type=int, default=3, help="unanswerable golden questions to ask")
    parser.add_argument("--pause", type=float, default=7.0, help="seconds between model questions")
    parser.add_argument("--isolation-question", default="",
                        help="a question only the second organisation's documents can answer")
    parser.add_argument("--isolation-file", default="", help="the file name of that document")
    parser.add_argument("--report", type=Path, help="save the results as JSON here")
    args = parser.parse_args(argv)

    api_key = os.getenv("CHECK_API_KEY", "")
    if not api_key:
        parser.error("Set CHECK_API_KEY to an organisation's API key, or the shared demo key")
    second_key = os.getenv("CHECK_SECOND_API_KEY", "")
    if bool(args.isolation_question) != bool(args.isolation_file):
        parser.error("--isolation-question and --isolation-file go together")
    if args.isolation_question and not second_key:
        parser.error("The isolation check needs CHECK_SECOND_API_KEY")

    with httpx.Client(base_url=args.api_url.rstrip("/")) as client:
        report = run(client, api_key, use_model=not args.no_model, allow_empty_index=args.allow_empty_index,
                     questions=args.questions, refusals=args.refusals, pause_seconds=args.pause, second_key=second_key,
                     isolation_question=args.isolation_question, isolation_file=args.isolation_file,
                     ui_url=args.ui_url)
    summary = report.summary()
    if summary["answers_timed"]:
        print(f"Answers timed: {summary['answers_timed']}, p50 {summary['answer_p50_seconds']} s, "
              f"p95 {summary['answer_p95_seconds']} s")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("All checks passed." if report.passed else "Some checks failed.")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
