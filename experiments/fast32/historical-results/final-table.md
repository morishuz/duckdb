# Full-query timings: final paged adapter

Two million rows, 2 GB memory limit; milliseconds. Median of five trial medians, five timed queries per trial. Ratios above 1 favor paged. All 390 trials succeeded with matching checksums.

| Case | Threads | Stock DuckDB | Previous adapter | Paged adapter | Stock / paged | Previous / paged |
|---|---:|---:|---:|---:|---:|---:|
| ascending:key | 1 | 24.20 | 26.35 | 22.01 | 1.10 | 1.20 |
| ascending:key | 4 | 12.80 | 14.48 | 12.41 | 1.03 | 1.17 |
| ascending:payload | 1 | 34.21 | 36.14 | 32.01 | 1.07 | 1.13 |
| ascending:payload | 4 | 16.99 | 20.05 | 16.75 | 1.01 | 1.20 |
| descending:key | 1 | 26.76 | 26.65 | 23.38 | 1.14 | 1.14 |
| descending:key | 4 | 14.82 | 15.87 | 13.28 | 1.12 | 1.19 |
| descending:payload | 1 | 36.69 | 37.92 | 34.56 | 1.06 | 1.10 |
| descending:payload | 4 | 17.59 | 21.12 | 18.12 | 0.97 | 1.17 |
| equal:key | 1 | 22.05 | 22.54 | 19.46 | 1.13 | 1.16 |
| equal:key | 4 | 13.32 | 13.84 | 11.41 | 1.17 | 1.21 |
| equal:payload | 1 | 31.03 | 33.32 | 29.31 | 1.06 | 1.14 |
| equal:payload | 4 | 15.86 | 18.71 | 15.80 | 1.00 | 1.18 |
| lowcard:key | 1 | 34.18 | 64.55 | 26.85 | 1.27 | 2.40 |
| lowcard:key | 4 | 19.47 | 26.49 | 18.49 | 1.05 | 1.43 |
| lowcard:payload | 1 | 50.04 | 83.43 | 45.71 | 1.09 | 1.83 |
| lowcard:payload | 4 | 25.63 | 34.66 | 29.23 | 0.88 | 1.19 |
| uniform:key | 1 | 81.25 | 38.86 | 33.55 | 2.42 | 1.16 |
| uniform:key | 4 | 33.07 | 22.99 | 21.24 | 1.56 | 1.08 |
| uniform:payload | 1 | 103.20 | 61.61 | 62.63 | 1.65 | 0.98 |
| uniform:payload | 4 | 40.39 | 38.96 | 35.10 | 1.15 | 1.11 |
| uniform:wide | 1 | 148.98 | 153.22 | 152.69 | 0.98 | 1.00 |
| uniform:wide | 4 | 53.07 | 56.55 | 54.10 | 0.98 | 1.05 |
| zeros:key | 1 | 53.71 | 26.83 | 24.34 | 2.21 | 1.10 |
| zeros:key | 4 | 21.25 | 14.68 | 13.29 | 1.60 | 1.10 |
| zeros:payload | 1 | 62.54 | 37.73 | 35.11 | 1.78 | 1.07 |
| zeros:payload | 4 | 30.58 | 19.80 | 19.62 | 1.56 | 1.01 |

Raw trial ranges are retained in the JSONL. The wide-key control follows native sorting in all three builds. Low-cardinality payloads at four threads remain slower than stock; small differences and control drift require caution.
