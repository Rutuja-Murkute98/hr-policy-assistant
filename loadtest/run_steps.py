"""Run Locust at rising numbers of users, record the server's memory, and save real results.

    python loadtest/run_steps.py --host http://127.0.0.1:8031 --server-port 8031 --name simulated --users 1,5,10,20,40

Needs only the separate Locust environment: pip install -r loadtest/requirements.txt
"""
import argparse
import csv
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
REPORTS = HERE.parent / "evaluation" / "reports"
SAMPLE_SECONDS = 1.0


def server_processes(port: int) -> list[psutil.Process]:
    """Find the server listening on this port, with any worker processes it started."""
    for connection in psutil.net_connections(kind="tcp"):
        if connection.laddr and connection.laddr.port == port and connection.status == "LISTEN" and connection.pid:
            parent = psutil.Process(connection.pid)
            return [parent, *parent.children(recursive=True)]
    raise SystemExit(f"Nothing is listening on port {port}")


def watch_memory(port: int, stop: threading.Event, peak: list[float]) -> None:
    while not stop.wait(SAMPLE_SECONDS):
        total = 0.0
        for process in server_processes(port):
            try:
                total += process.memory_info().rss / 1_000_000
            except psutil.Error:
                continue
        peak[0] = max(peak[0], total)


def run_step(host: str, users: int, seconds: int, port: int) -> dict:
    peak = [0.0]
    stop = threading.Event()
    watcher = threading.Thread(target=watch_memory, args=(port, stop, peak))
    watcher.start()
    with tempfile.TemporaryDirectory() as folder:
        prefix = Path(folder) / "step"
        spawn = max(1, users // 5)
        subprocess.run([sys.executable, "-m", "locust", "-f", str(HERE / "locustfile.py"), "--headless", "--host", host,
                        "-u", str(users), "-r", str(spawn), "-t", f"{seconds}s", "--csv", str(prefix), "--only-summary",
                        "--stop-timeout", "120", "--loglevel", "WARNING"], check=False, capture_output=True, text=True)
        with open(f"{prefix}_stats.csv", encoding="utf-8", newline="") as file:
            rows = {row["Name"]: row for row in csv.DictReader(file)}
        failures_file = Path(f"{prefix}_failures.csv")
        with failures_file.open(encoding="utf-8", newline="") as file:
            failures = [{"name": row["Name"], "error": row["Error"][:120], "count": int(row["Occurrences"])}
                        for row in csv.DictReader(file)]
    stop.set()
    watcher.join()
    ask, overall = rows["/ask"], rows["Aggregated"]
    return {
        "users": users,
        "seconds": seconds,
        "requests_per_second": round(float(overall["Requests/s"]), 2),
        "ask_requests": int(ask["Request Count"]),
        "ask_failures": int(ask["Failure Count"]),
        "ask_p50_ms": int(float(ask["50%"])),
        "ask_p95_ms": int(float(ask["95%"])),
        "ask_p99_ms": int(float(ask["99%"])),
        "all_failures": int(overall["Failure Count"]),
        "failure_kinds": failures,
        "peak_memory_mb": round(peak[0]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--server-port", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--users", default="1,5,10,20,40")
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--rest", type=int, default=10, help="seconds between steps")
    args = parser.parse_args()
    steps = []
    for users in (int(value) for value in args.users.split(",")):
        steps.append(run_step(args.host, users, args.seconds, args.server_port))
        print(json.dumps(steps[-1]), flush=True)
        time.sleep(args.rest)
    (REPORTS / f"load_{args.name}.json").write_text(json.dumps({"name": args.name, "steps": steps}, indent=1),
                                                    encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
