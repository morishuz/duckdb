#!/usr/bin/env python3
"""Exact fixed-record DuckDB validation; deliberately not a timing benchmark.

Run after building the paged adapter:
    DUCKDB_LIBRARY=/absolute/path/libduckdb-fast32.dylib \
        python3 experiments/fast32/paged_validate.py --output results/duckdb-paged-validation

The same library executes each query with FAST32_DUCKDB=0 and =1. C stderr is
captured separately for every query to prove eligible 8/16-byte adapter paths
ran, and forced external sorting used the native fallback. JSON retains exact
multiset/order checks, source fingerprints, result samples, plans and traces.
Existing results are never overwritten. No external database files are used.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

import run


MIXED_KEY = """CASE WHEN i%17=0 THEN NULL
    WHEN i%19=0 THEN -2147483648 WHEN i%23=0 THEN 2147483647
    WHEN i%29=0 THEN -1 WHEN i%31=0 THEN 0
    ELSE (hash(i)%4294967296)::BIGINT-2147483648 END"""
DOMINANT_NULL_KEY = "CASE WHEN i%100=0 THEN (hash(i)%4294967296)::BIGINT-2147483648 ELSE NULL END"
SHIFTED_KEY = "((hash(i)%65536)::BIGINT-32768)*32768"


def typed(rows):
    return [tuple(None if value is None else int(value) for value in row) for row in rows]


def digest(values):
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def query_with_trace(connection, sql, mode):
    if mode is None:
        os.environ.pop("FAST32_DUCKDB", None)
    else:
        os.environ["FAST32_DUCKDB"] = str(mode)
    os.environ["FAST32_DUCKDB_TRACE"] = "1"
    saved_stderr = os.dup(2)
    with tempfile.TemporaryFile(mode="w+b") as capture:
        try:
            sys.stderr.flush()
            os.dup2(capture.fileno(), 2)
            rows = connection.query(sql)
        finally:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        capture.seek(0)
        trace = capture.read().decode(errors="replace")
    return typed(rows), trace


def expected_order(source, direction, nulls):
    values = sorted((row[0] for row in source if row[0] is not None), reverse=direction == "DESC")
    empty = [None] * sum(row[0] is None for row in source)
    return empty + values if nulls == "FIRST" else values + empty


def cases_for(threads):
    cases = []
    for dataset, expression in [
        ("mixed-null-extremes", MIXED_KEY),
        ("shifted-duplicates", SHIFTED_KEY),
        ("dominant-null", DOMINANT_NULL_KEY),
    ]:
        for payload in [False, True]:
            for direction in ["ASC", "DESC"]:
                for nulls in ["FIRST", "LAST"]:
                    cases.append(
                        dict(
                            dataset=dataset,
                            expression=expression,
                            rows=70003,
                            payload=payload,
                            direction=direction,
                            nulls=nulls,
                            threads=threads,
                            external=False,
                            require_adapter=True,
                        )
                    )
    for payload in [False, True]:
        cases.append(
            dict(
                dataset="descending-duplicates",
                expression="(70003-i)/7",
                rows=70003,
                payload=payload,
                direction="ASC",
                nulls="LAST",
                threads=threads,
                external=False,
                require_adapter=False,
            )
        )
        for n in [16383, 16384] if payload else [32767, 32768]:
            for direction in ["ASC", "DESC"]:
                cases.append(
                    dict(
                        dataset="page-boundary",
                        expression=MIXED_KEY,
                        rows=n,
                        payload=payload,
                        direction=direction,
                        nulls="FIRST",
                        threads=threads,
                        external=False,
                        require_adapter=True,
                    )
                )
    # Exercise external sorting explicitly without a large pressure benchmark.
    if threads == 4:
        cases.append(
            dict(
                dataset="forced-external",
                expression=MIXED_KEY,
                rows=131099,
                payload=True,
                direction="DESC",
                nulls="LAST",
                threads=threads,
                external=True,
                require_adapter=False,
            )
        )
    return cases


def exercise(db, case, number):
    columns = "k,p" if case["payload"] else "k"
    sql = f"SELECT {columns} FROM input_records ORDER BY k {case['direction']} " f"NULLS {case['nulls']}"
    record = dict(case, case_number=number, sql=sql, library=str(run.LIB), ok=False)
    try:
        os.environ["FAST32_DUCKDB"] = "0"
        db.query(f"SET threads={case['threads']}")
        db.query("SET memory_limit='1GB'")
        db.query("SET max_execution_time=120000")
        db.query(f"SET debug_force_external={'true' if case['external'] else 'false'}")
        db.query(
            "CREATE OR REPLACE TABLE input_records AS SELECT "
            f"({case['expression']})::INTEGER AS k, i::BIGINT AS p "
            f"FROM range({case['rows']}) t(i)"
        )
        source = typed(db.query(f"SELECT {columns} FROM input_records"))
        source_multiset = Counter(source)
        expected_keys = expected_order(source, case["direction"], case["nulls"])
        record["source"] = dict(
            rows=len(source),
            null_keys=expected_keys.count(None),
            unique_keys=len({row[0] for row in source}),
            sha256=digest(sorted(source, key=repr)),
        )
        record["plan"] = db.query("EXPLAIN " + sql)
        outputs = {}
        traces = {}
        # Alternate run order while keeping FAST32_DUCKDB mutations sequential.
        for mode in [0, 1] if number % 2 == 0 else [1, 0]:
            outputs[mode], traces[mode] = query_with_trace(db, sql, mode)
        default_rows, default_trace = query_with_trace(db, sql, None)
        stock, fast = outputs[0], outputs[1]
        stock_keys, fast_keys = [row[0] for row in stock], [row[0] for row in fast]
        adapter_runs = [
            {"rows": int(rows), "width": int(width)}
            for rows, width in re.findall(r"FAST32_PAGED rows=(\d+) width=(\d+)", traces[1])
        ]
        checks = {
            "default_exact_record_multiset": Counter(default_rows) == source_multiset,
            "default_independent_key_order": [row[0] for row in default_rows] == expected_keys,
            "plan_contains_order_by": "ORDER_BY" in str(record["plan"]).upper().replace(" ", "_"),
            "source_row_count": len(source) == case["rows"],
            "native_row_count": len(stock) == len(source),
            "adapter_row_count": len(fast) == len(source),
            "native_exact_record_multiset": Counter(stock) == source_multiset,
            "adapter_exact_record_multiset": Counter(fast) == source_multiset,
            "native_independent_key_order": stock_keys == expected_keys,
            "adapter_independent_key_order": fast_keys == expected_keys,
            "identical_key_sequence": stock_keys == fast_keys,
            "native_mode_did_not_use_adapter": "FAST32_PAGED" not in traces[0],
        }
        if case["require_adapter"]:
            checks["default_adapter_path_observed"] = "FAST32_PAGED" in default_trace
            checks["eligible_adapter_path_observed"] = bool(adapter_runs)
            checks["eligible_record_width"] = bool(adapter_runs) and all(
                item["width"] == (16 if case["payload"] else 8) for item in adapter_runs
            )
        if case["dataset"] == "dominant-null":
            checks["dominant_partition_observed"] = any(
                "shortcut=3" in line for line in traces[1].splitlines() if line.startswith("FAST32_PAGED")
            )
        if case["external"]:
            checks["default_external_fallback"] = "FAST32_PAGED" not in default_trace and "STOCK rows=" in default_trace
            checks["external_native_fallback_observed"] = not adapter_runs and "STOCK rows=" in traces[1]
        record.update(
            checks=checks,
            ok=all(checks.values()),
            adapter_runs=adapter_runs,
            default_trace=default_trace,
            native_trace=traces[0],
            adapter_trace=traces[1],
            native_key_sequence_sha256=digest(stock_keys),
            adapter_key_sequence_sha256=digest(fast_keys),
            native_first=stock[:6],
            native_last=stock[-6:],
            adapter_first=fast[:6],
            adapter_last=fast[-6:],
        )
    except Exception as exc:
        record["error"] = repr(exc)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--case-filter", help="Optional substring in dataset name")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Reserve before opening DuckDB; preserve any previously recorded evidence.
    results_file = (output / "results.jsonl").open("x")
    saved_env = {name: os.environ.get(name) for name in ["FAST32_DUCKDB", "FAST32_DUCKDB_TRACE"]}
    db = None
    records = []
    started = time.monotonic()
    try:
        os.environ["FAST32_DUCKDB"] = "0"
        db = run.Connection(":memory:")
        cases = [
            case
            for threads in args.threads
            for case in cases_for(threads)
            if not args.case_filter or args.case_filter in case["dataset"]
        ]
        if not cases:
            raise ValueError("No validation cases matched")
        metadata = dict(
            created_utc=datetime.now(timezone.utc).isoformat(),
            library=str(run.LIB.resolve()),
            version=db.query("SELECT version()"),
            cases=cases,
            purpose="Exact fixed-record correctness and adapter coverage; not a timing benchmark",
            comparison="One patched binary, sequential FAST32_DUCKDB=0 and =1 queries",
        )
        (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        for number, case in enumerate(cases):
            record = exercise(db, case, number)
            records.append(record)
            results_file.write(json.dumps(record) + "\n")
            results_file.flush()
            print(
                f"{number + 1}/{len(cases)} t={case['threads']} {case['dataset']} "
                f"rows={case['rows']} payload={case['payload']} "
                f"{case['direction']} NULLS {case['nulls']}: {'PASS' if record['ok'] else 'FAIL'}",
                flush=True,
            )
    finally:
        results_file.close()
        if db is not None:
            db.close()
        for name, original in saved_env.items():
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original
    summary = dict(
        cases=len(records),
        passed=sum(record["ok"] for record in records),
        failed=sum(not record["ok"] for record in records),
        elapsed_seconds=time.monotonic() - started,
        output=str(output),
        failed_cases=[record["case_number"] for record in records if not record["ok"]],
    )
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary), flush=True)
    return 0 if records and summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
