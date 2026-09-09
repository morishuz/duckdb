#!/usr/bin/env python3
"""Pinned DuckDB C API experiment; no Python DuckDB package required."""
import argparse
import ctypes as C
import json
import os
from pathlib import Path
import random
import resource
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SUFFIX = ".dylib" if sys.platform == "darwin" else ".so"
LIB = Path(os.environ.get("DUCKDB_LIBRARY", str(ROOT / "build/fast32/src" / ("libduckdb" + SUFFIX))))
STOCK_LIB = Path(os.environ.get("DUCKDB_STOCK_LIBRARY", str(LIB)))
DB = Path(os.environ.get("DUCKDB_DATA", str(ROOT / "build/fast32/data.duckdb")))
OUT = Path(os.environ.get("DUCKDB_RESULTS", str(ROOT / "build/fast32/results")))


class Result(C.Structure):
    _fields_ = [
        ("columns", C.c_uint64),
        ("rows", C.c_uint64),
        ("changed", C.c_uint64),
        ("column_data", C.c_void_p),
        ("error", C.c_void_p),
        ("internal", C.c_void_p),
    ]


class Connection:
    def __init__(self, path):
        self.lib = C.CDLL(str(LIB))
        specs = {
            "duckdb_open": ([C.c_char_p, C.POINTER(C.c_void_p)], C.c_int),
            "duckdb_connect": ([C.c_void_p, C.POINTER(C.c_void_p)], C.c_int),
            "duckdb_query": ([C.c_void_p, C.c_char_p, C.POINTER(Result)], C.c_int),
            "duckdb_result_error": ([C.POINTER(Result)], C.c_char_p),
            "duckdb_destroy_result": ([C.POINTER(Result)], None),
            "duckdb_row_count": ([C.POINTER(Result)], C.c_uint64),
            "duckdb_column_count": ([C.POINTER(Result)], C.c_uint64),
            "duckdb_value_varchar": ([C.POINTER(Result), C.c_uint64, C.c_uint64], C.c_void_p),
            "duckdb_value_is_null": ([C.POINTER(Result), C.c_uint64, C.c_uint64], C.c_bool),
            "duckdb_free": ([C.c_void_p], None),
            "duckdb_disconnect": ([C.POINTER(C.c_void_p)], None),
            "duckdb_close": ([C.POINTER(C.c_void_p)], None),
        }
        for name, (args, ret) in specs.items():
            f = getattr(self.lib, name)
            f.argtypes, f.restype = args, ret
        self.db, self.conn = C.c_void_p(), C.c_void_p()
        assert self.lib.duckdb_open(str(path).encode(), C.byref(self.db)) == 0
        assert self.lib.duckdb_connect(self.db, C.byref(self.conn)) == 0

    def query(self, sql, fetch=True):
        result = Result()
        try:
            if self.lib.duckdb_query(self.conn, sql.encode(), C.byref(result)):
                raise RuntimeError(self.lib.duckdb_result_error(C.byref(result)).decode())
            rows = []
            if fetch:
                for r in range(self.lib.duckdb_row_count(C.byref(result))):
                    row = []
                    for c in range(self.lib.duckdb_column_count(C.byref(result))):
                        if self.lib.duckdb_value_is_null(C.byref(result), c, r):
                            row.append(None)
                        else:
                            ptr = self.lib.duckdb_value_varchar(C.byref(result), c, r)
                            row.append(C.string_at(ptr).decode())
                            self.lib.duckdb_free(ptr)
                    rows.append(row)
            return rows
        finally:
            self.lib.duckdb_destroy_result(C.byref(result))

    def close(self):
        self.lib.duckdb_disconnect(C.byref(self.conn))
        self.lib.duckdb_close(C.byref(self.db))


def setup(n, only=None):
    db = Connection(DB)
    db.query("SET threads=4")
    expressions = {
        "uniform": "(hash(i)%4294967296)::BIGINT-2147483648",
        "zeros": "CASE WHEN i%100=0 THEN (hash(i)%4294967296)::BIGINT-2147483648 ELSE 0 END",
        "lowcard": "(hash(i)%64)::BIGINT-32",
        "ascending": "i",
        "descending": f"{n}-i",
        "equal": "42",
    }
    for name, expr in expressions.items():
        if only and name != only:
            continue
        db.query(
            f"CREATE OR REPLACE TABLE {name} AS SELECT ({expr})::INTEGER k, i::BIGINT p, "
            f"hash(i)::UBIGINT v FROM range({n}) t(i)"
        )
    db.query("CHECKPOINT")
    db.close()


def sql_for(case):
    table, kind = case.split(":")
    if kind == "key":
        return f"SELECT sum(k) FROM (SELECT k FROM {table} ORDER BY k)"
    if kind == "payload":
        return f"SELECT sum(k), sum(p) FROM (SELECT k,p FROM {table} ORDER BY k)"
    if kind == "wide":
        return f"SELECT sum(v), sum(p) FROM (SELECT v,p FROM {table} ORDER BY v)"
    raise ValueError(case)


def worker(args):
    qos = None
    if sys.platform == "darwin":
        system = C.CDLL("/usr/lib/libSystem.B.dylib")
        system.pthread_set_qos_class_self_np.argtypes = [C.c_uint, C.c_int]
        system.pthread_set_qos_class_self_np.restype = C.c_int
        qos = system.pthread_set_qos_class_self_np(0x19, 0)
        assert qos == 0
    db = Connection(DB)
    db.query(f"SET threads={args.threads}")
    db.query(f"SET memory_limit='{args.memory}'")
    db.query("SET max_execution_time=120000")
    sql = sql_for(args.case)
    count = int(db.query(f"SELECT count(*) FROM {args.case.split(':')[0]}")[0][0])
    plan = db.query("EXPLAIN " + sql)
    assert "ORDER_BY" in str(plan).upper() or "ORDER BY" in str(plan).upper(), plan
    plan_name = f"{args.case.replace(':', '-')}-{count}-{args.threads}-{LIB.stem}.plan.txt"
    (OUT / plan_name).write_text(plan[0][1])
    # Warmup and measured query have identical SQL and consume all sorted rows.
    expected = db.query(sql)
    samples = []
    for _ in range(args.samples):
        start = time.perf_counter()
        actual = db.query(sql)
        samples.append(time.perf_counter() - start)
        assert actual == expected
    seconds = statistics.median(samples)
    # A separate execution provides profiling without contaminating wall timings.
    profile_path = OUT / f"profile-{os.getpid()}.json"
    db.query("SET enable_profiling='json'")
    db.query(f"SET profiling_output='{profile_path}'")
    assert db.query(sql) == expected
    db.query("SET enable_profiling='no_output'")
    profile = json.loads(profile_path.read_text())

    def sort_time(node):
        return (node.get("timing", 0) if node.get("type") == "ORDER_BY" else 0) + sum(
            sort_time(child) for child in node.get("children", [])
        )

    order_seconds = sum(sort_time(node) for node in profile["operator"])
    assert order_seconds > 0
    item = dict(
        case=args.case,
        threads=args.threads,
        memory=args.memory,
        rows=count,
        mode=os.environ.get("FAST32_DUCKDB", "1"),
        library=LIB.name,
        seconds=seconds,
        samples_seconds=samples,
        qos_result=qos,
        order_by_seconds=order_seconds,
        checksum=actual,
        peak_buffer_bytes=profile["system"]["peak_buffer_memory"],
        peak_temp_bytes=profile["system"]["peak_temp_dir_size"],
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        profile=profile_path.name,
    )
    db.close()
    print(json.dumps(item), flush=True)


def validate():
    db = Connection(":memory:")
    checks = 0
    for threads in [1, 4]:
        db.query(f"SET threads={threads}")
        db.query(
            "CREATE OR REPLACE TABLE t AS SELECT i::BIGINT p, "
            "CASE WHEN i%17=0 THEN NULL WHEN i%19=0 THEN -2147483648 "
            "WHEN i%23=0 THEN 2147483647 ELSE (hash(i)%10001)::BIGINT-5000 END::INTEGER k, "
            "hash(i)::UBIGINT v, ('prefix-' || (hash(i)%1000)::VARCHAR) s FROM range(50000) t(i)"
        )
        source = db.query("SELECT k,p,v,s FROM t ORDER BY p")
        for key, index in [("k", 0), ("v", 2), ("s", 3)]:
            for direction in ["ASC", "DESC"]:
                for nulls in ["FIRST", "LAST"]:
                    sql = f"SELECT k,p,v,s FROM t ORDER BY {key} {direction} NULLS {nulls}"
                    os.environ["FAST32_DUCKDB"] = "0"
                    stock = db.query(sql)
                    os.environ["FAST32_DUCKDB"] = "1"
                    fast = db.query(sql)
                    assert sorted(stock, key=lambda r: int(r[1])) == source
                    assert sorted(fast, key=lambda r: int(r[1])) == source
                    assert [r[index] for r in stock] == [r[index] for r in fast]
                    checks += 1
        for sql in ["SELECT k,p FROM t ORDER BY k,p DESC", "SELECT k FROM t ORDER BY k DESC"]:
            os.environ["FAST32_DUCKDB"] = "0"
            stock = db.query(sql)
            os.environ["FAST32_DUCKDB"] = "1"
            assert stock == db.query(sql)
            checks += 1
        for n, expression in [
            (0, "i"),
            (1, "i"),
            (4095, "-i"),
            (4096, "-i"),
            (50000, "42"),
            (50000, "i"),
            (50000, "-i"),
            (50000, "hash(i)%64"),
            (50000, "CASE WHEN i%100=0 THEN i ELSE 0 END"),
        ]:
            for payload in [False, True]:
                columns = "k,p" if payload else "k"
                sql = (
                    f"SELECT {columns} FROM (SELECT ({expression})::INTEGER k, i p " f"FROM range({n}) t(i)) ORDER BY k"
                )
                os.environ["FAST32_DUCKDB"] = "0"
                stock = db.query(sql)
                os.environ["FAST32_DUCKDB"] = "1"
                fast = db.query(sql)
                assert [r[0] for r in stock] == [r[0] for r in fast]
                assert sorted(stock) == sorted(fast)
                checks += 1
    db.close()
    print(json.dumps({"correctness_cases": checks, "max_rows_each": 50000}))


def benchmark(args):
    if (OUT / args.output).exists():
        raise FileExistsError("Use a fresh DUCKDB_RESULTS directory or --output filename")
    assert STOCK_LIB.exists(), "Native comparator library does not exist"
    cases = [
        f"{d}:{kind}" for d in ["uniform", "zeros", "lowcard", "ascending", "equal"] for kind in ["key", "payload"]
    ] + ["uniform:wide"]
    if args.case:
        cases = [args.case]
    rng = random.Random(20260909)
    records = []
    for repeat in range(args.repeats):
        jobs = [(c, t, mode) for c in cases for t in [1, 4] for mode in ["0", "1"]]
        rng.shuffle(jobs)
        for case, threads, mode in jobs:
            env = dict(os.environ, FAST32_DUCKDB=mode)
            env["DUCKDB_LIBRARY"] = str(STOCK_LIB if mode == "0" else LIB)
            env.pop("FAST32_DUCKDB_TRACE", None)
            command = [
                sys.executable,
                __file__,
                "worker",
                "--case",
                case,
                "--threads",
                str(threads),
                "--memory",
                args.memory,
                "--samples",
                str(args.samples),
            ]
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=300)
            if result.returncode:
                record = dict(
                    case=case,
                    threads=threads,
                    mode=mode,
                    memory=args.memory,
                    repeat=repeat,
                    error=result.stderr,
                    library=env["DUCKDB_LIBRARY"],
                )
                with (OUT / args.output).open("a") as f:
                    f.write(json.dumps(record) + "\n")
                print(f"{repeat} {case} t={threads} mode={mode} FAILED: {result.stderr[-400:]}", flush=True)
                continue
            record = json.loads(result.stdout)
            record["repeat"] = repeat
            records.append(record)
            with (OUT / args.output).open("a") as f:
                f.write(json.dumps(record) + "\n")
            print(f"{repeat} {case} t={threads} mode={mode} {record['seconds']*1000:.2f} ms", flush=True)
    for case in cases:
        for t in [1, 4]:
            groups = [
                [r["seconds"] for r in records if r["case"] == case and r["threads"] == t and r["mode"] == mode]
                for mode in ["0", "1"]
            ]
            cell = [r for r in records if r["case"] == case and r["threads"] == t]
            if any(len(g) != args.repeats for g in groups) or len({json.dumps(r["checksum"]) for r in cell}) != 1:
                raise RuntimeError(f"Incomplete or mismatched results for {case}, threads={t}; no ratio reported")
            med = [statistics.median(g) for g in groups]
            print(case, t, *(round(m * 1000, 3) for m in med), "speedup", round(med[0] / med[1], 3))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["setup", "worker", "validate", "benchmark"])
    parser.add_argument("--rows", type=int, default=2_000_000)
    parser.add_argument("--case")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--memory", default="2GB")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--output", default="timings.jsonl")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.action == "setup":
        setup(args.rows, args.case.split(":")[0] if args.case else None)
    elif args.action == "worker":
        worker(args)
    elif args.action == "validate":
        validate()
    else:
        benchmark(args)
