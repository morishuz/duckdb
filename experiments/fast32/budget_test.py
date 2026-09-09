#!/usr/bin/env python3
"""Memory-budget regression checks against the pinned DuckDB C API builds.

This is a correctness/reliability harness, not a timing benchmark. It reuses
run.py's C API bindings and existing uniform datasets without modifying them.
Each library/case runs in a fresh process; concurrent queries use connections
to ONE database handle and therefore share its buffer manager and memory limit.

Examples (after building the fork and preparing the two datasets):
    python3 experiments/fast32/budget_test.py
    python3 experiments/fast32/budget_test.py --case-filter 2m-single --threads 4
    python3 experiments/fast32/budget_test.py --case-filter shared --repeats 5

DUCKDB_LIBRARY and DUCKDB_DATA retain run.py's meanings for the patched library
and the 2M database. DUCKDB_STOCK_LIBRARY, DUCKDB_DATA_8M, and DUCKDB_BUDGET_OUT
override the other paths. Outputs are written to a new directory by default;
an existing results.jsonl is never appended to or overwritten.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes as C
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import resource
import subprocess
import sys
import threading
import time

import run


class SharedConnection(run.Connection):
    """A second connection sharing the owner's existing duckdb_database."""

    def __init__(self, owner):
        self.lib, self.db, self.conn = owner.lib, owner.db, C.c_void_p()
        if self.lib.duckdb_connect(self.db, C.byref(self.conn)):
            raise RuntimeError("duckdb_connect failed for shared database")

    def close(self):
        self.lib.duckdb_disconnect(C.byref(self.conn))


PRESSURE_SQL = run.sql_for("uniform:payload")
VALIDATION_SQL = """
SELECT count(*) AS rows,
       count(*) FILTER (WHERE previous_k IS NOT NULL AND k < previous_k) AS out_of_order,
       count(*) FILTER (WHERE k IS NULL OR p IS NULL OR
           k != ((hash(p) % 4294967296)::BIGINT - 2147483648)::INTEGER) AS mismatched_payloads,
       min(p), max(p), sum(k), sum(p), sum(p::HUGEINT * p)
FROM (
    SELECT k, p, lag(k) OVER () AS previous_k
    FROM (SELECT k, p FROM uniform ORDER BY k)
)
"""


def check_validation(actual, rows, key_sum):
    """Check order and every key/payload association, plus multiset invariants.

    Row count, payload range, and two exact integer moments detect lost or
    repeated row ids. They are useful regression checks, not a mathematical
    proof that arbitrary adversarial multisets are identical.
    """
    if len(actual) != 1 or len(actual[0]) != 8:
        return {"result_shape": False}
    values = [None if v is None else int(v) for v in actual[0]]
    return {
        "row_count": values[0] == rows,
        "sorted_ascending": values[1] == 0,
        "every_payload_matches_key": values[2] == 0,
        "payload_min": values[3] == 0,
        "payload_max": values[4] == rows - 1,
        "key_sum": values[5] == key_sum,
        "payload_sum": values[6] == rows * (rows - 1) // 2,
        "payload_square_sum": values[7] == rows * (rows - 1) * (2 * rows - 1) // 6,
    }


def worker(args):
    case = json.loads(args.worker)
    owner = run.Connection(case["database"])
    connections = [owner]
    record = dict(case, library=str(run.LIB), mode=os.environ.get("FAST32_DUCKDB", "0"))
    try:
        owner.query(f"SET threads={case['threads']}")
        owner.query(f"SET memory_limit='{case['memory']}'")
        connections.extend(SharedConnection(owner) for _ in range(case["connections"] - 1))
        settings = []
        for connection in connections:
            connection.query(f"SET max_execution_time={case['query_timeout_ms']}")
            settings.append(connection.query("SELECT current_setting('threads'), current_setting('memory_limit')"))
        record["connection_settings"] = settings
        if not all(setting == settings[0] for setting in settings):
            raise RuntimeError("Connections sharing the database disagree on global settings")
        if int(settings[0][0][0]) != case["threads"]:
            raise RuntimeError("The effective thread setting differs from the requested setting")

        reference = owner.query("SELECT count(*), sum(k), sum(p) FROM uniform")
        if int(reference[0][0]) != case["rows"]:
            raise RuntimeError(f"Dataset row count differs from case: {reference}")
        key_sum = int(reference[0][1])
        expected = [[reference[0][1], reference[0][2]]]
        if int(reference[0][2]) != case["rows"] * (case["rows"] - 1) // 2:
            raise RuntimeError("Dataset does not have the expected row-id sum")
        plans = {
            "pressure": owner.query("EXPLAIN " + PRESSURE_SQL),
            "validation": owner.query("EXPLAIN " + VALIDATION_SQL),
        }
        for name, plan in plans.items():
            if "ORDER_BY" not in str(plan).upper().replace(" ", "_"):
                raise RuntimeError(f"{name} plan no longer contains ORDER_BY: {plan}")
        if "STREAMING_WINDOW" not in str(plans["validation"]).upper().replace(" ", "_"):
            raise RuntimeError("Validation must use a streaming window without another sort")
        record["plans"] = plans
        record["reference"] = reference
        barrier = threading.Barrier(case["connections"])

        def exercise(index):
            connection = connections[index]
            attempts = []
            jobs = [("pressure", repeat, PRESSURE_SQL) for repeat in range(case["repeats"])]
            jobs.append(("validation", 0, VALIDATION_SQL))
            for kind, repeat, sql in jobs:
                attempt = {"connection": index, "kind": kind, "repeat": repeat}
                try:
                    barrier.wait(timeout=case["query_timeout_ms"] / 1000 + 10)
                    started = time.perf_counter()
                    actual = connection.query(sql)
                    attempt["elapsed_seconds"] = time.perf_counter() - started
                    attempt["result"] = actual
                    checks = (
                        {"aggregate_matches_source": actual == expected}
                        if kind == "pressure"
                        else check_validation(actual, case["rows"], key_sum)
                    )
                    attempt["checks"] = checks
                    attempt["ok"] = all(checks.values())
                except Exception as exc:
                    attempt["ok"] = False
                    attempt["error"] = str(exc)
                # A failed reservation/query must not poison the connection.
                try:
                    attempt["connection_reusable"] = connection.query("SELECT 42") == [["42"]]
                except Exception as exc:
                    attempt["connection_reusable"] = False
                    attempt["recovery_error"] = str(exc)
                attempt["ok"] = attempt["ok"] and attempt["connection_reusable"]
                attempts.append(attempt)
            return attempts

        with ThreadPoolExecutor(max_workers=len(connections)) as pool:
            groups = list(pool.map(exercise, range(len(connections))))
        record["attempts"] = [attempt for group in groups for attempt in group]
        record["ok"] = all(attempt["ok"] for attempt in record["attempts"])
    except Exception as exc:
        record["ok"] = False
        record["setup_error"] = str(exc)
    finally:
        for connection in reversed(connections):
            connection.close()
    record["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
        1 if sys.platform == "darwin" else 1024
    )
    print(json.dumps(record), flush=True)


def run_matrix(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results_path = output / "results.jsonl"
    # Reserve this output before doing any work. Preserve earlier artifacts.
    with results_path.open("x"):
        pass
    libraries = {"stock": args.stock_library.resolve(), "patched": args.library.resolve()}
    for library in libraries.values():
        if not library.is_file():
            raise SystemExit(f"Library not found: {library}")
    datasets = [("2m", 2_000_000, args.data, "64MB", "128MB"), ("8m", 8_000_000, args.data_8m, "128MB", "256MB")]
    cases = []
    for label, rows, database, memory, shared_memory in datasets:
        for connections, scope in [(1, "single"), (2, "shared")]:
            name = f"{label}-{scope}"
            if args.case_filter and args.case_filter not in name:
                continue
            for threads in args.threads:
                # Default shared tests use 4/8 threads; explicitly supplied 1
                # is still useful when --case-filter targets shared cases.
                if connections == 2 and threads == 1 and args.case_filter is None:
                    continue
                if not database.is_file():
                    raise SystemExit(f"Dataset not found: {database}")
                cases.append(
                    dict(
                        case=name,
                        rows=rows,
                        database=str(database.resolve()),
                        memory=args.memory or (memory if connections == 1 else shared_memory),
                        threads=threads,
                        connections=connections,
                        repeats=args.repeats,
                        query_timeout_ms=args.query_timeout_ms,
                    )
                )
    if not cases:
        raise SystemExit("No cases matched --case-filter")
    metadata = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "libraries": {name: str(path) for name, path in libraries.items()},
        "cases": cases,
        "purpose": "Memory reliability and correctness, not timing",
        "shared_database_instance": "Both concurrent connections use one duckdb_database handle",
        "query_sql": PRESSURE_SQL,
        "validation_sql": VALIDATION_SQL,
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    records = []
    rng = random.Random(20260909)
    for case in cases:
        modes = list(libraries)
        rng.shuffle(modes)
        for mode in modes:
            env = dict(os.environ, DUCKDB_LIBRARY=str(libraries[mode]), FAST32_DUCKDB="1" if mode == "patched" else "0")
            if not args.trace:
                env.pop("FAST32_DUCKDB_TRACE", None)
            else:
                env["FAST32_DUCKDB_TRACE"] = "1"
            started = time.perf_counter()
            try:
                completed = subprocess.run(
                    [sys.executable, __file__, "--worker", json.dumps(case)],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=args.timeout,
                )
                if completed.returncode:
                    record = dict(
                        case,
                        ok=False,
                        worker_returncode=completed.returncode,
                        error=completed.stderr,
                        stdout=completed.stdout,
                    )
                else:
                    record = json.loads(completed.stdout)
                if completed.stderr:
                    stderr_name = f"{case['case']}-t{case['threads']}-{mode}.stderr.txt"
                    (output / stderr_name).write_text(completed.stderr)
                    record["stderr_file"] = stderr_name
            except subprocess.TimeoutExpired as exc:
                record = dict(
                    case,
                    ok=False,
                    error=f"Worker exceeded {args.timeout}s",
                    stderr=str(exc.stderr),
                    stdout=str(exc.stdout),
                )
            except (ValueError, OSError) as exc:
                record = dict(case, ok=False, error=str(exc))
            record["build"] = mode
            record["worker_elapsed_seconds"] = time.perf_counter() - started
            records.append(record)
            with results_path.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            failed = sum(not item["ok"] for item in record.get("attempts", []))
            print(
                f"{case['case']} threads={case['threads']} {mode}: "
                f"{'PASS' if record['ok'] else 'FAIL'} ({failed} failed query checks)",
                flush=True,
            )
    comparisons = []
    for case in cases:
        pair = {r["build"]: r for r in records if r["case"] == case["case"] and r["threads"] == case["threads"]}
        comparisons.append(
            {
                "case": case["case"],
                "threads": case["threads"],
                "memory": case["memory"],
                "stock_ok": pair["stock"]["ok"],
                "patched_ok": pair["patched"]["ok"],
                "added_failure": pair["stock"]["ok"] and not pair["patched"]["ok"],
            }
        )
    summary = {
        "comparisons": comparisons,
        "added_failures": sum(c["added_failure"] for c in comparisons),
        "all_patched_pass": all(c["patched_ok"] for c in comparisons),
        "all_stock_pass": all(c["stock_ok"] for c in comparisons),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"Results: {results_path}")
    return 0 if summary["all_patched_pass"] and summary["all_stock_pass"] else 1


def main():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--library", type=Path, default=run.LIB)
    parser.add_argument(
        "--stock-library", type=Path, default=Path(os.environ.get("DUCKDB_STOCK_LIBRARY", str(run.STOCK_LIB)))
    )
    parser.add_argument("--data", type=Path, default=run.DB)
    parser.add_argument(
        "--data-8m",
        type=Path,
        default=Path(os.environ.get("DUCKDB_DATA_8M", str(run.ROOT / "build/fast32/data-8m.duckdb"))),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("DUCKDB_BUDGET_OUT", str(run.ROOT / "build/fast32/budget-results" / timestamp))),
    )
    parser.add_argument("--case-filter", help="Substring of 2m-single, 8m-single, 2m-shared, or 8m-shared")
    parser.add_argument("--memory", help="Override every selected case's database-wide memory limit, e.g. 1GB")
    parser.add_argument("--threads", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--query-timeout-ms", type=int, default=120_000)
    parser.add_argument("--timeout", type=float, default=600, help="Maximum seconds per worker process")
    parser.add_argument("--trace", action="store_true", help="Keep adapter trace stderr in output artifacts")
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return 0
    if args.repeats < 1 or min(args.threads) < 1 or args.query_timeout_ms < 1 or args.timeout <= 0:
        parser.error("Repeat, thread, and timeout values must be positive")
    if len(args.threads) != len(set(args.threads)):
        parser.error("Thread values must be unique")
    return run_matrix(args)


if __name__ == "__main__":
    sys.exit(main())
