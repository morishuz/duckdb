# Experimental fast32 DuckDB fork

This branch packages the paged fast32 sorter inside DuckDB, with no dependency on
another checkout. It is based on upstream commit
`bd77495e4b98772224948c57320bc1aa25ad9bfc`. It is an experimental independent fork,
not an official DuckDB build. The implementation was developed with AI assistance.
The upstream MIT license applies to the added code as well.

## Build and run

Requires CMake, Python 3, and a C++20 compiler and standard library. The clean build
has been tested on Apple M1/macOS; x86 and Windows have not been validated for this
fork. From the repository root:

```sh
cmake -S . -B build/fast32 -DCMAKE_BUILD_TYPE=Release -DBUILD_UNITTESTS=OFF
cmake --build build/fast32 --target duckdb shell --parallel 2
./build/fast32/duckdb
```

Eligible sorts use fast32 by default. To run the same executable with native local
sorting, set `FAST32_DUCKDB=0` before launching it:

```sh
FAST32_DUCKDB=0 ./build/fast32/duckdb
FAST32_DUCKDB_TRACE=1 ./build/fast32/duckdb
```

Tracing prints budget admissions and `FAST32_PAGED` or `STOCK` sort events to
stderr. Leave tracing off for timings. These are process-wide environment controls;
do not change them concurrently with running queries. The official `pip install
duckdb` package does not contain this modification.

## What changes

The local sorter alternates between pinned DuckDB pages and one managed scratch
buffer. It checks exact ascending/descending order, partitions a sampled dominant
value (with full exact classification), then sorts remaining regions using up to
11-bit radix digits. Constant digits are skipped. Key/payload records move together;
equal-key order is unspecified, as with native ORDER BY without a tie-breaker.

Eligibility requires a complete normalized key fitting in eight bytes, fixed-size
payloads, an in-memory local run of 4,096 through UINT32_MAX records, and sufficient
scratch budget. DuckDB handles signed ordering, descending order and NULL placement
before this kernel. SQL key width alone does not determine eligibility: BIGINT can
need more than eight normalized bytes. Strings, wider keys, variable-size payloads,
index sorting, small runs and external sorting retain the native path.

Scratch needs roughly one record array (8 or 16 bytes per local row), rounded by
BufferManager, plus 256 KiB kernel metadata. Admission also accounts for existing
run storage and four block allocations of headroom. If the budget or initial
allocation cannot support this, sorting falls back to native before mutating input.
DuckDB's memory limit is not a cap on total process RSS. This fork still uses more
memory than native sorting when the fast path is active.

## Historical measurements

These are measurements of the same kernel and adapter before packaging this fork,
explicitly enabled with `FAST32_DUCKDB=1`, on the same pinned upstream revision.
They are not fresh measurements of this branch. Apple M1, 16 GiB, Release C++20;
random INTEGER keys with row-id payload, ORDER BY retained beneath an aggregate,
2 GB memory limit. Times are full queries, not just the sorting kernel.

| Rows | Threads | Unmodified DuckDB | Paged fast32 | Speedup |
|---|---:|---:|---:|---:|
| 2M | 1 | 103.20 ms | 62.63 ms | 1.65x |
| 2M | 4 | 40.39 ms | 35.10 ms | 1.15x |
| 8M | 1 | 447.90 ms | 247.17 ms | 1.81x |
| 8M | 4 | 170.62 ms | 128.06 ms | 1.33x |

At 8M rows, peak RSS rose from 250 to 373 MiB (one thread) and 276 to 454 MiB
(four threads) versus native. Low-cardinality payloads at four threads were about
14% slower than native. Tight-memory and spilling workloads used native fallback
and showed no meaningful gain. These are selected workloads on one desktop, not a
claim that DuckDB generally becomes this much faster.

The [full 2M table](historical-results/final-table.md), CSV matrices and original
[metadata](historical-results/metadata.json) preserve the historical results. The
metadata identifies the original research checkout and artifacts; those paths are
provenance, not paths expected in this fork. Old `budget` columns refer to the
superseded two-buffer adapter, not another mode included here.

## Validate correctness

The kernel harness checks exact records, page boundaries, duplicates, misleading
samples and cancellation. Run the normal and sanitizer builds:

```sh
c++ -std=c++20 -O2 -Wall -Wextra -Werror -Isrc/include experiments/fast32/paged_radix_test.cpp -o build/fast32/kernel-test
./build/fast32/kernel-test
c++ -std=c++20 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer -Isrc/include experiments/fast32/paged_radix_test.cpp -o build/fast32/kernel-sanitize
./build/fast32/kernel-sanitize
python3 experiments/fast32/paged_validate.py --output build/fast32/sql-validation
python3 experiments/fast32/run.py validate
```

The Python harness loads `build/fast32/src/libduckdb.dylib` on macOS or `.so` on
Linux via the C API. Override `DUCKDB_LIBRARY` for a different build. SQL validation
compares exact order and record multisets, NULLs, limits, fixed key/payload widths,
threads and external fallback; traces prove the intended path actually ran. Use a
new output directory on each validation run. This targeted suite does not replace
DuckDB's full upstream test suite.

## Reproduce timings and memory-pressure checks

```sh
python3 experiments/fast32/run.py setup --rows 2000000
python3 experiments/fast32/run.py benchmark --case uniform:payload --repeats 5 --samples 5
python3 experiments/fast32/budget_test.py --case-filter 2m --trace
```

The benchmark randomizes fresh worker processes with native mode `0` and fast32
mode `1`, on the same compiled library, at one and four threads. It warms up once,
reports medians, saves checksums, plans and separate profiling runs, and records
process RSS and buffer/temp-storage peaks. Results default to `build/fast32/results`.
Use a fresh `DUCKDB_RESULTS` directory for each run. Dataset construction is outside
timing. Avoid other CPU-heavy work during measurements.

To compare a separately built unmodified upstream library instead, supply
`DUCKDB_STOCK_LIBRARY=/absolute/path/to/libduckdb.dylib`. The historical table used
that stronger separate-build comparator. The stress harness also accepts
`--stock-library` and `--data-8m`; its default comparator is native mode in this fork.
Always inspect failed cells and checksums before quoting speedups.

## Validation of this packaged fork

The clean Release library and CLI build passed on Apple M1. Both normal and
address/undefined-behavior sanitizer kernel runs passed 865 cases. SQL validation
passed 69 cases in all three modes (native, explicit fast32, default), plus 64
broader regression cases. Memory-pressure and shared-connection tests passed all
264 query checks across 2M/8M datasets, 1/4/8 threads, and tight/ample budgets.
Formatting checks passed. The full upstream DuckDB suite has not been run.

A small 3-by-3 randomized benchmark smoke test also completed with matching
checksums. See [source fingerprints and validation counts](packaging-validation/validation.json)
and [smoke-test records](packaging-validation/benchmark.jsonl). The historical
matrices above provide the broader distribution and memory comparison.
