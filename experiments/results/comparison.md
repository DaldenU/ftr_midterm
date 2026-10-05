# Baseline vs fault-tolerant: measured comparison

Repetitions per cell: S1/baseline: 3, S1/ft: 3, S2/baseline: 3, S2/ft: 3, S3/baseline: 3, S3/ft: 3, S4/baseline: 3, S4/ft: 3, S5/baseline: 3, S5/ft: 3, S6/baseline: 3, S6/ft: 3, S7/baseline: 3, S7/ft: 3. Cells show mean (min-max) over repetitions. Improvement = relative change of the mean (negative = fault-tolerant version worse). Hardware: 2 vCPU shared by all processes (see report, limitations).

## 1. Headline comparison

| Experiment | Metric | Baseline | Fault-tolerant | Improvement |
|---|---|---:|---:|---:|
| S1 Application crash | failed requests | 83 (76-87) | 0 (0-1) | +100 % |
| S1 Application crash | user-visible downtime of payment service (s) | 10.8 (10.7-10.8) | 0.0 | +100 % |
| S2 Database failure (restart) | failed requests | 333 | 75 (65-82) | +77 % |
| S2 Database failure (restart) | requests served degraded/recovered instead of failing | 0 | 318 (307-333) | n/a |
| S3 Network/service timeout (gray failure) | failed payment + transcript requests (dependants of the slow service) | 89 (79-95) | 0 (0-1) | +100 % |
| S3 Network/service timeout (gray failure) | p95 latency of payments (ms) | 4353 ms (3166-5885) | 1584 ms (1157-2298) | +64 % |
| S4 Node/hardware failure | failed requests | 640 (638-643) | 0 | +100 % |
| S4 Node/hardware failure | user-visible downtime, any service (s) | 16.1 (16.0-16.1) | 0.0 | +100 % |
| S5 Corrupted/lost transaction | duplicate payments | 24 (19-27) | 0 | +100 % |
| S5 Corrupted/lost transaction | ledger mismatches (students) | 17 (15-18) | 0 | +100 % |
| S5 Corrupted/lost transaction | duplicate academic records | 9 (6-12) | 0 | +100 % |
| S6 High load | p95 latency at peak load, 150 clients (ms) | 3295 ms (3038-3546) | 195 ms (190-199) | +94 % |
| S6 High load | goodput at peak load (successful req/s) | 117 (108-126) | 188 (181-194) | +61 % |
| S6 High load | hard (non-shed) error rate at peak | 0.86 % (0.48-1.45) | 0.00 % | +100 % |
| S7 Primary database loss (replication vs backup) | failed requests | 850 (848-852) | 4 (1-7) | +100 % |
| S7 Primary database loss (replication vs backup) | acknowledged writes lost (RPO) | 95 (91-99) | 0 | +100 % |

## 2. Experiment table (assignment format)

| Experiment | System | Failure | Detection time | Recovery time | Failed requests | Recovered | Consistent |
|---|---|---|---:|---:|---:|---:|:--:|
| S1 Application crash | baseline | SIGKILL of application instance payment-1 | 0.04 s (0.01-0.12) | 10.8 s (10.7-10.9) | 83 (76-87) / 1280 | 0 | 2/3 |
| S1 Application crash | fault-tolerant | SIGKILL of application instance payment-1 | 0.03 s (0.01-0.07) | 2.1 s (2.0-2.1) | 0 (0-1) / 1280 | 1 (0-1) | 3/3 |
| S2 Database failure (restart) | baseline | PostgreSQL crash-stop (pg_ctl -m immediate) for 8s, then restart | 0.02 s (0.01-0.03) | 8.4 s (8.4-8.5) | 333 / 1280 | 0 | 2/3 |
| S2 Database failure (restart) | fault-tolerant | PostgreSQL crash-stop (pg_ctl -m immediate) for 8s, then restart | 0.08 s (0.03-0.16) | 8.8 s (8.6-9.3) | 75 (65-82) / 1280 | 104 (97-108) | 3/3 |
| S3 Network/service timeout (gray failure) | baseline | student-service gray failure: +3000 ms latency and 30% HTTP 503 on all | 3.20 s (3.08-3.35) | 20.6 s (20.4-20.7) | 205 (203-208) / 1840 | 0 | 3/3 |
| S3 Network/service timeout (gray failure) | fault-tolerant | student-service gray failure: +3000 ms latency and 30% HTTP 503 on all | 2.16 s (2.13-2.17) | 25.4 s (25.1-25.6) | 89 (86-90) / 1840 | 42 (37-49) | 3/3 |
| S4 Node/hardware failure | baseline | Node A lost: SIGKILL of every instance placed on it | 0.01 s (0.01-0.01) | 16.1 s (16.1-16.2) | 640 (638-643) / 1600 | 0 | 3/3 |
| S4 Node/hardware failure | fault-tolerant | Node A lost: SIGKILL of every instance placed on it | 0.03 s (0.02-0.03) | 6.8 s (6.7-6.9) | 0 / 1600 | 45 (37-55) | 3/3 |
| S5 Corrupted/lost transaction | baseline | Interrupted transactions: payment interrupted after first write (30%), | 0.52 s (0.43-0.60) | 4.5 s (4.2-4.8) | 22 (19-26) / 109 (103-113) | 29 (23-33) | 0/3 |
| S5 Corrupted/lost transaction | fault-tolerant | Interrupted transactions: payment interrupted after first write (30%), | 0.45 s (0.34-0.57) | 8.1 s (7.2-8.6) | 43 (37-48) / 129 (118-135) | 33 (26-37) | 3/3 |
| S6 High load | baseline | Closed-loop load steps of [5, 25, 75, 150] concurrent clients (12s eac | 5.42 s (5.08-5.87) | 13.1 s (13.0-13.2) | 13 (8-21) / 7312 (6841-7574) | 0 | 3/3 |
| S6 High load | fault-tolerant | Closed-loop load steps of [5, 25, 75, 150] concurrent clients (12s eac | 0.07 s (0.06-0.10) | 13.0 s (13.0-13.0) | 2170 (2165-2175) / 15572 (14885-16146) | 1 (0-2) | 3/3 |
| S7 Primary database loss (replication vs backup) | baseline | Permanent loss of the primary database host (SIGKILL + data directory  | 0.01 s (0.01-0.01) | 21.3 s (21.3-21.4) | 850 (848-852) / 1920 | 0 | 0/3 |
| S7 Primary database loss (replication vs backup) | fault-tolerant | Permanent loss of the primary database host (SIGKILL + data directory  | 0.08 s (0.03-0.13) | 2.0 s (1.5-2.5) | 4 (1-7) / 1920 | 31 (21-48) | 3/3 |

## 3. Per-scenario detail

### S1 - Application crash

| Metric | Baseline | Fault-tolerant | Improvement |
|---|---:|---:|---:|
| Detection time, earliest signal (s) | 0.04 (0.01-0.12) | 0.03 (0.01-0.07) | +32 % |
|   - external /ready monitor (s) | 0.05 (0.01-0.12) | 0.03 (0.01-0.07) | +29 % |
|   - system-internal: gateway/breaker (s) | n/a | 0.04 (0.01-0.11) | n/a |
|   - first client-visible error (s) | 0.08 (0.01-0.23) | 0.02 | +70 % |
| Recovery duration, failure -> full service (s) | 10.8 (10.7-10.9) | 2.1 (2.0-2.1) | +81 % |
| Repair action duration (s) | 0.8 (0.7-0.9) | 1.0 (0.9-1.1) | -26 % |
| Total requests | 1280 | 1280 |  |
| Successful requests | 1197 (1193-1204) | 1280 (1279-1280) | +7 % |
| Failed requests | 83 (76-87) | 0 (0-1) | +100 % |
|   - deliberately shed (503 + Retry-After) | 0 | 0 |  |
| Failed requests inside failure window | 83 (75-87) | 0 | +100 % |
| Recovered requests (masked by retry/failover/client retry) | 0 | 1 (0-1) | n/a |
| Degraded responses (reduced functionality) | 0 | 0 |  |
| Duplicate requests sent | 0 | 0 |  |
| Duplicates answered from stored result | 0 | 0 |  |
| Latency p50 (ms) | 14 (12-16) | 8 (8-8) | +44 % |
| Latency p95 (ms) | 215 (45-551) | 41 (39-43) | +81 % |
| Latency p99 (ms) | 1011 (733-1564) | 628 (301-866) | +38 % |
| User-visible downtime of student (any non-OK probe, s) | 0.0 | 0.0 | no difference (both 0) |
| User-visible downtime of payment (any non-OK probe, s) | 10.8 (10.7-10.8) | 0.0 | +100 % |
| User-visible downtime of academic (any non-OK probe, s) | 0.0 | 0.0 | no difference (both 0) |
| Probe availability of student after failure start (%) | 100.00 | 100.00 | |
| Probe availability of payment after failure start (%) | 55.22 (54.81-55.42) | 100.00 | |
| Probe availability of academic after failure start (%) | 100.00 | 100.00 | |

Per-operation failures (count) and p95 latency (ms):

| Operation | Baseline failed | FT failed | Baseline p95 | FT p95 | FT degraded | FT recovered |
|---|---:|---:|---:|---:|---:|---:|
| get_account | 17 (15-19) | 0 | 81 (20-173) | 16 (14-20) | 0 | 0 |
| get_student | 0 | 0 | 217 (18-611) | 18 (17-21) | 0 | 0 |
| get_transcript | 0 | 0 | 124 (38-292) | 28 (20-42) | 0 | 0 |
| post_grade | 0 | 0 | 198 (22-542) | 24 (21-28) | 0 | 0 |
| post_payment | 67 (60-72) | 0 (0-1) | 309 (56-800) | 55 (48-62) | 0 | 1 (0-1) |

Data-consistency invariants checked directly in PostgreSQL after recovery:

| Invariant violation | Baseline | Fault-tolerant |
|---|---:|---:|
| duplicate_payments | 0 | 0 |
| ledger_mismatch_students | 0 (0-1) | 0 |
| ledger_mismatch_amount | 333 (0-1000) | 0 |
| stuck_pending_payments | 0 | 0 |
| rolled_back_payments | 0 | 0 (0-1) |
| duplicate_academic_records | 0 | 0 |
| lost_acknowledged_payments | 0 | 0 |
| lost_acknowledged_grades | 0 | 0 |
| partial_batches | 0 | 0 |
| batches_with_duplicates | 0 | 0 |
| **consistent (runs passing all invariants)** | 2/3 | 3/3 |

Observations (generated by the experiment, not edited):

* Gateway ejected the dead replica and routed to payment-2; supervisor restarted payment-1 automatically.
* No orchestrator: the dead instance stays down until an operator restarts it (assumed reaction time 10.0s).

### S2 - Database failure (restart)

| Metric | Baseline | Fault-tolerant | Improvement |
|---|---:|---:|---:|
| Detection time, earliest signal (s) | 0.02 (0.01-0.03) | 0.08 (0.03-0.16) | -275 % |
|   - external /ready monitor (s) | 0.07 (0.03-0.13) | 0.08 (0.03-0.16) | -4 % |
|   - system-internal: gateway/breaker (s) | n/a | 1.00 (0.95-1.03) | n/a |
|   - first client-visible error (s) | 0.02 (0.01-0.03) | 0.39 (0.22-0.57) | -1825 % |
| Recovery duration, failure -> full service (s) | 8.4 (8.4-8.5) | 8.8 (8.6-9.3) | -5 % |
| Repair action duration (s) | 0.4 (0.4-0.5) | 0.8 (0.6-1.3) | -87 % |
| Total requests | 1280 | 1280 |  |
| Successful requests | 947 | 1205 (1198-1215) | +27 % |
| Failed requests | 333 | 75 (65-82) | +77 % |
|   - deliberately shed (503 + Retry-After) | 0 | 0 |  |
| Failed requests inside failure window | 332 (332-333) | 74 (64-82) | +78 % |
| Recovered requests (masked by retry/failover/client retry) | 0 | 104 (97-108) | n/a |
| Degraded responses (reduced functionality) | 0 | 214 (206-227) |  |
| Duplicate requests sent | 0 | 0 |  |
| Duplicates answered from stored result | 0 | 2 (1-2) |  |
| Latency p50 (ms) | 15 (14-17) | 10 (9-11) | +36 % |
| Latency p95 (ms) | 263 (56-646) | 1418 (1006-1815) | -440 % |
| Latency p99 (ms) | 1444 (919-2382) | 4158 (3580-5290) | -188 % |
| User-visible downtime of student (any non-OK probe, s) | 8.3 (8.2-8.3) | 0.0 | +100 % |
| User-visible downtime of payment (any non-OK probe, s) | 8.3 | 8.1 (8.1-8.2) | +3 % |
| User-visible downtime of academic (any non-OK probe, s) | 8.3 (8.3-8.3) | 0.0 | +100 % |
| Probe availability of student after failure start (%) | 65.55 (65.27-65.69) | 100.00 | |
| Probe availability of payment after failure start (%) | 65.37 (65.27-65.42) | 94.86 (94.30-95.18) | |
| Probe availability of academic after failure start (%) | 65.37 (65.27-65.42) | 100.00 | |

Per-operation failures (count) and p95 latency (ms):

| Operation | Baseline failed | FT failed | Baseline p95 | FT p95 | FT degraded | FT recovered |
|---|---:|---:|---:|---:|---:|---:|
| get_account | 14 (12-15) | 8 (7-10) | 196 (18-482) | 1852 (1816-1906) | 0 | 5 (5-6) |
| get_student | 149 (142-155) | 2 (0-4) | 234 (35-628) | 664 (265-973) | 152 (139-159) | 21 (20-22) |
| get_transcript | 68 (66-71) | 16 (12-20) | 268 (41-620) | 374 (118-840) | 53 (48-56) | 23 (20-27) |
| post_grade | 49 (41-53) | 27 (20-31) | 250 (44-588) | 2833 (2510-3058) | 0 | 22 (21-24) |
| post_payment | 53 (48-59) | 22 (21-24) | 283 (82-646) | 3905 (2824-5344) | 9 (3-14) | 33 (27-38) |

Data-consistency invariants checked directly in PostgreSQL after recovery:

| Invariant violation | Baseline | Fault-tolerant |
|---|---:|---:|
| duplicate_payments | 0 | 0 |
| ledger_mismatch_students | 0 (0-1) | 0 |
| ledger_mismatch_amount | 333 (0-1000) | 0 |
| stuck_pending_payments | 0 | 0 |
| rolled_back_payments | 0 | 0 |
| duplicate_academic_records | 0 | 0 |
| lost_acknowledged_payments | 0 | 0 |
| lost_acknowledged_grades | 0 | 0 |
| partial_batches | 0 | 0 |
| batches_with_duplicates | 0 | 0 |
| **consistent (runs passing all invariants)** | 2/3 | 3/3 |

Observations (generated by the experiment, not edited):

* Baseline opens a new connection per request and has no retry: every request during the outage fails; it recovers by itself once the DB is back.
* Reads fell back to the last-known-good cache (degraded); writes were retried with backoff inside a 6 s budget.

### S3 - Network/service timeout (gray failure)

| Metric | Baseline | Fault-tolerant | Improvement |
|---|---:|---:|---:|
| Detection time, earliest signal (s) | 3.20 (3.08-3.35) | 2.16 (2.13-2.17) | +33 % |
|   - external /ready monitor (s) | 10.17 | n/a | n/a |
|   - system-internal: gateway/breaker (s) | n/a | 2.16 (2.13-2.17) | n/a |
|   - first client-visible error (s) | 3.20 (3.08-3.35) | 5.29 (5.07-5.62) | -65 % |
| Recovery duration, failure -> full service (s) | 20.6 (20.4-20.7) | 25.4 (25.1-25.6) | -23 % |
| Repair action duration (s) | 0.6 (0.4-0.7) | 5.4 (5.1-5.6) | -838 % |
| Total requests | 1840 | 1840 |  |
| Successful requests | 1635 (1632-1637) | 1751 (1750-1754) | +7 % |
| Failed requests | 205 (203-208) | 89 (86-90) | +57 % |
|   - deliberately shed (503 + Retry-After) | 0 | 1 (0-2) |  |
| Failed requests inside failure window | 205 (203-208) | 89 (86-90) | +57 % |
| Recovered requests (masked by retry/failover/client retry) | 0 | 42 (37-49) | n/a |
| Degraded responses (reduced functionality) | 0 | 329 (283-355) |  |
| Duplicate requests sent | 0 | 0 |  |
| Duplicates answered from stored result | 0 | 0 |  |
| Latency p50 (ms) | 35 (29-43) | 11 (11-12) | +68 % |
| Latency p95 (ms) | 3888 (3100-5132) | 3018 (3013-3026) | +22 % |
| Latency p99 (ms) | 5570 (4198-7071) | 4019 (3442-4870) | +28 % |
| User-visible downtime of student (any non-OK probe, s) | 20.4 (20.3-20.6) | 20.3 (20.1-20.5) | +0 % |
| User-visible downtime of payment (any non-OK probe, s) | 0.0 | 0.0 | no difference (both 0) |
| User-visible downtime of academic (any non-OK probe, s) | 20.5 (20.3-20.7) | 2.9 (2.1-3.4) | +86 % |
| Probe availability of student after failure start (%) | 90.07 (89.85-90.40) | 89.93 (89.85-90.00) | |
| Probe availability of payment after failure start (%) | 100.00 | 100.00 | |
| Probe availability of academic after failure start (%) | 90.00 (89.85-90.21) | 98.24 (98.09-98.48) | |

Per-operation failures (count) and p95 latency (ms):

| Operation | Baseline failed | FT failed | Baseline p95 | FT p95 | FT degraded | FT recovered |
|---|---:|---:|---:|---:|---:|---:|
| get_account | 0 | 0 | 1383 (536-2304) | 411 (128-892) | 0 | 0 |
| get_student | 116 (108-129) | 88 (86-90) | 4055 (3190-5271) | 3180 (3043-3381) | 0 | 17 (13-19) |
| get_transcript | 53 (40-62) | 0 | 4239 (3100-5851) | 1257 (1123-1524) | 199 (174-212) | 10 (9-12) |
| post_grade | 0 | 0 | 1760 (905-2807) | 389 (209-635) | 0 | 0 |
| post_payment | 35 (33-39) | 0 (0-1) | 4353 (3166-5885) | 1584 (1157-2298) | 130 (109-144) | 15 (12-18) |

Data-consistency invariants checked directly in PostgreSQL after recovery:

| Invariant violation | Baseline | Fault-tolerant |
|---|---:|---:|
| duplicate_payments | 0 | 0 |
| ledger_mismatch_students | 0 | 0 |
| ledger_mismatch_amount | 0 | 0 |
| stuck_pending_payments | 0 | 0 |
| rolled_back_payments | 0 | 0 |
| duplicate_academic_records | 0 | 0 |
| lost_acknowledged_payments | 0 | 0 |
| lost_acknowledged_grades | 0 | 0 |
| partial_batches | 0 | 0 |
| batches_with_duplicates | 0 | 0 |
| **consistent (runs passing all invariants)** | 3/3 | 3/3 |

Observations (generated by the experiment, not edited):

* Baseline waits on the slow dependency (30 s timeout): worker threads pile up and latency propagates to callers.
* Circuit breakers on payment/academic opened after consecutive timeouts, then fast-failed into degraded mode (payment accepted unverified, transcript without student profile).

### S4 - Node/hardware failure

| Metric | Baseline | Fault-tolerant | Improvement |
|---|---:|---:|---:|
| Detection time, earliest signal (s) | 0.01 (0.01-0.01) | 0.03 (0.02-0.03) | -161 % |
|   - external /ready monitor (s) | 0.09 (0.01-0.15) | 0.07 (0.03-0.12) | +16 % |
|   - system-internal: gateway/breaker (s) | n/a | 0.03 (0.02-0.03) | n/a |
|   - first client-visible error (s) | 0.01 (0.01-0.01) | n/a | n/a |
| Recovery duration, failure -> full service (s) | 16.1 (16.1-16.2) | 6.8 (6.7-6.9) | +58 % |
| Repair action duration (s) | 1.1 (1.1-1.2) | 1.7 (1.7-1.8) | -54 % |
| Total requests | 1600 | 1600 |  |
| Successful requests | 960 (957-962) | 1600 | +67 % |
| Failed requests | 640 (638-643) | 0 | +100 % |
|   - deliberately shed (503 + Retry-After) | 0 | 0 |  |
| Failed requests inside failure window | 639 (637-642) | 0 | +100 % |
| Recovered requests (masked by retry/failover/client retry) | 0 | 45 (37-55) | n/a |
| Degraded responses (reduced functionality) | 0 | 0 |  |
| Duplicate requests sent | 0 | 0 |  |
| Duplicates answered from stored result | 0 | 0 |  |
| Latency p50 (ms) | 14 (13-15) | 8 (8-8) | +43 % |
| Latency p95 (ms) | 71 (51-85) | 128 (55-249) | -80 % |
| Latency p99 (ms) | 1067 (772-1231) | 1038 (467-2154) | +3 % |
| User-visible downtime of student (any non-OK probe, s) | 15.9 (15.8-16.1) | 0.0 | +100 % |
| User-visible downtime of payment (any non-OK probe, s) | 16.0 (15.9-16.1) | 0.0 | +100 % |
| User-visible downtime of academic (any non-OK probe, s) | 16.0 | 0.0 | +100 % |
| Probe availability of student after failure start (%) | 50.16 (49.84-50.47) | 100.00 | |
| Probe availability of payment after failure start (%) | 50.05 (49.84-50.31) | 100.00 | |
| Probe availability of academic after failure start (%) | 49.89 (49.84-50.00) | 100.00 | |

Per-operation failures (count) and p95 latency (ms):

| Operation | Baseline failed | FT failed | Baseline p95 | FT p95 | FT degraded | FT recovered |
|---|---:|---:|---:|---:|---:|---:|
| get_account | 33 (30-36) | 0 | 43 (25-54) | 326 (10-952) | 0 | 0 |
| get_student | 281 (267-296) | 0 | 43 (21-78) | 73 (16-185) | 0 | 0 (0-1) |
| get_transcript | 134 (127-141) | 0 | 82 (35-140) | 157 (120-223) | 0 | 25 (20-30) |
| post_grade | 98 (92-105) | 0 | 79 (24-151) | 109 (21-284) | 0 | 0 |
| post_payment | 95 (90-102) | 0 | 150 (66-299) | 202 (146-306) | 0 | 20 (17-25) |

Data-consistency invariants checked directly in PostgreSQL after recovery:

| Invariant violation | Baseline | Fault-tolerant |
|---|---:|---:|
| duplicate_payments | 0 | 0 |
| ledger_mismatch_students | 0 | 0 |
| ledger_mismatch_amount | 0 | 0 |
| stuck_pending_payments | 0 | 0 |
| rolled_back_payments | 0 | 0 |
| duplicate_academic_records | 0 | 0 |
| lost_acknowledged_payments | 0 | 0 |
| lost_acknowledged_grades | 0 | 0 |
| partial_batches | 0 | 0 |
| batches_with_duplicates | 0 | 0 |
| **consistent (runs passing all invariants)** | 3/3 | 3/3 |

Observations (generated by the experiment, not edited):

* All services live on the single node: total outage until an operator restores it (assumed 15.0s).
* Node A (student-1, payment-1, academic-1) was lost; node B replicas kept serving through the gateway.

### S5 - Corrupted/lost transaction

| Metric | Baseline | Fault-tolerant | Improvement |
|---|---:|---:|---:|
| Detection time, earliest signal (s) | 0.52 (0.43-0.60) | 0.45 (0.34-0.57) | +14 % |
|   - external /ready monitor (s) | n/a | n/a | n/a |
|   - system-internal: gateway/breaker (s) | n/a | n/a | n/a |
|   - first client-visible error (s) | 0.52 (0.43-0.60) | 0.45 (0.34-0.57) | +14 % |
| Recovery duration, failure -> full service (s) | 4.5 (4.2-4.8) | 8.1 (7.2-8.6) | -80 % |
| Repair action duration (s) | 4.5 (4.2-4.8) | 8.1 (7.2-8.6) | -80 % |
| Total requests | 109 (103-113) | 129 (118-135) |  |
| Successful requests | 86 (81-91) | 86 (81-91) | +0 % |
| Failed requests | 22 (19-26) | 43 (37-48) | -91 % |
|   - deliberately shed (503 + Retry-After) | 0 | 0 |  |
| Failed requests inside failure window | 22 (19-26) | 43 (37-48) | -91 % |
| Recovered requests (masked by retry/failover/client retry) | 29 (23-33) | 33 (26-37) | +14 % |
| Degraded responses (reduced functionality) | 0 | 0 |  |
| Duplicate requests sent | 29 (23-33) | 49 (38-55) |  |
| Duplicates answered from stored result | 0 | 12 (8-15) |  |
| Latency p50 (ms) | 73 (62-85) | 39 (38-41) | +46 % |
| Latency p95 (ms) | 487 (391-588) | 404 (334-504) | +17 % |
| Latency p99 (ms) | 655 (516-727) | 492 (395-608) | +25 % |
| User-visible downtime of student (any non-OK probe, s) | 0.0 | 0.0 | no difference (both 0) |
| User-visible downtime of payment (any non-OK probe, s) | 0.0 | 0.0 | no difference (both 0) |
| User-visible downtime of academic (any non-OK probe, s) | 0.0 | 0.0 | no difference (both 0) |
| Probe availability of student after failure start (%) | 100.00 | 100.00 | |
| Probe availability of payment after failure start (%) | 100.00 | 100.00 | |
| Probe availability of academic after failure start (%) | 100.00 | 100.00 | |

Per-operation failures (count) and p95 latency (ms):

| Operation | Baseline failed | FT failed | Baseline p95 | FT p95 | FT degraded | FT recovered |
|---|---:|---:|---:|---:|---:|---:|
| post_batch | 4 (3-6) | 4 (3-6) | 359 (335-379) | 339 (337-343) | 0 | 4 (3-6) |
| post_payment | 18 (16-20) | 38 (33-42) | 579 (495-643) | 394 (255-548) | 0 | 28 (22-34) |

Data-consistency invariants checked directly in PostgreSQL after recovery:

| Invariant violation | Baseline | Fault-tolerant |
|---|---:|---:|
| duplicate_payments | 24 (19-27) | 0 |
| ledger_mismatch_students | 17 (15-18) | 0 |
| ledger_mismatch_amount | 18000 (16000-20000) | 0 |
| stuck_pending_payments | 0 | 0 |
| rolled_back_payments | 0 | 5 (4-7) |
| duplicate_academic_records | 9 (6-12) | 0 |
| lost_acknowledged_payments | 0 | 0 |
| lost_acknowledged_grades | 0 | 0 |
| partial_batches | 0 | 0 |
| batches_with_duplicates | 4 (3-6) | 0 |
| **consistent (runs passing all invariants)** | 0/3 | 3/3 |

Transaction-level outcome:

| Metric | Baseline | Fault-tolerant |
|---|---:|---:|
| logical payments planned | 60 | 60 |
| logical payments acknowledged | 55 (53-56) | 55 (53-56) |
| interrupted payments | 18 (16-20) | 18 (16-20) |
| abandoned payments (never retried) | 5 (4-7) | 5 (4-7) |
| double-submitted payments | 12 (8-15) | 12 (8-15) |
| interrupted batches | 4 (3-6) | 4 (3-6) |
| mean client-side recovery time (s) | 0.65 (0.64-0.67) | 1.33 (1.23-1.42) |
| p95 client-side recovery time (s) | 0.79 (0.78-0.80) | 2.16 (2.15-2.17) |

Observations (generated by the experiment, not edited):

* Baseline commits payment row and ledger update separately and has no idempotency check: an interruption corrupts the ledger and the client's retry creates a second payment.
* Interrupted payments stayed PENDING (checkpoint 1); a retry with the same Idempotency-Key resumed them, abandoned ones were rolled back by the reconciler; batches were rolled back atomically.

### S6 - High load

| Metric | Baseline | Fault-tolerant | Improvement |
|---|---:|---:|---:|
| Detection time, earliest signal (s) | 5.42 (5.08-5.87) | 0.07 (0.06-0.10) | +99 % |
|   - external /ready monitor (s) | n/a | n/a | n/a |
|   - system-internal: gateway/breaker (s) | n/a | n/a | n/a |
|   - first client-visible error (s) | 5.42 (5.08-5.87) | 0.07 (0.06-0.10) | +99 % |
| Recovery duration, failure -> full service (s) | 13.1 (13.0-13.2) | 13.0 (13.0-13.0) | +1 % |
| Repair action duration (s) | 0.0 | 0.0 | no difference (both 0) |
| Total requests | 7312 (6841-7574) | 15572 (14885-16146) |  |
| Successful requests | 7299 (6820-7564) | 13402 (12720-13971) | +84 % |
| Failed requests | 13 (8-21) | 2170 (2165-2175) | -16595 % |
|   - deliberately shed (503 + Retry-After) | 0 | 2170 (2165-2175) |  |
| Failed requests inside failure window | 13 (8-21) | 1520 (1517-1521) | -11590 % |
| Recovered requests (masked by retry/failover/client retry) | 0 | 1 (0-2) | n/a |
| Degraded responses (reduced functionality) | 0 | 0 |  |
| Duplicate requests sent | 0 | 0 |  |
| Duplicates answered from stored result | 0 | 0 |  |
| Latency p50 (ms) | 178 (164-190) | 68 (66-71) | +62 % |
| Latency p95 (ms) | 1756 (1676-1855) | 176 (170-184) | +90 % |
| Latency p99 (ms) | 3204 (3067-3328) | 264 (256-279) | +92 % |
| User-visible downtime of student (any non-OK probe, s) | 6.7 (5.0-8.0) | 1.3 (1.0-1.5) | +81 % |
| User-visible downtime of payment (any non-OK probe, s) | 0.7 (0.0-2.0) | 1.4 (1.0-1.8) | -102 % |
| User-visible downtime of academic (any non-OK probe, s) | 7.0 (6.0-8.1) | 0.7 (0.4-0.8) | +90 % |
| Probe availability of student after failure start (%) | 93.92 (92.65-95.68) | 91.39 (89.66-92.61) | |
| Probe availability of payment after failure start (%) | 98.66 (97.95-99.34) | 91.97 (90.20-93.03) | |
| Probe availability of academic after failure start (%) | 94.00 (92.75-94.89) | 94.03 (93.53-94.67) | |

Per-operation failures (count) and p95 latency (ms):

| Operation | Baseline failed | FT failed | Baseline p95 | FT p95 | FT degraded | FT recovered |
|---|---:|---:|---:|---:|---:|---:|
| get_account | 0 | 96 (86-107) | 998 (975-1010) | 115 (108-120) | 0 | 0 |
| get_student | 13 (8-20) | 976 (961-998) | 2037 (1951-2185) | 189 (182-200) | 0 | 0 |
| get_transcript | 0 | 448 (440-454) | 1723 (1648-1774) | 171 (168-177) | 0 | 0 |
| post_grade | 0 (0-1) | 330 (317-340) | 1765 (1751-1775) | 168 (159-175) | 0 | 0 |
| post_payment | 0 | 321 (312-335) | 1023 (942-1085) | 169 (163-179) | 0 | 1 (0-2) |

Data-consistency invariants checked directly in PostgreSQL after recovery:

| Invariant violation | Baseline | Fault-tolerant |
|---|---:|---:|
| duplicate_payments | 0 | 0 |
| ledger_mismatch_students | 0 | 0 |
| ledger_mismatch_amount | 0 | 0 |
| stuck_pending_payments | 0 | 0 |
| rolled_back_payments | 0 | 0 |
| duplicate_academic_records | 0 | 0 |
| lost_acknowledged_payments | 0 | 0 |
| lost_acknowledged_grades | 0 | 0 |
| partial_batches | 0 | 0 |
| batches_with_duplicates | 0 | 0 |
| **consistent (runs passing all invariants)** | 3/3 | 3/3 |

Per load step (closed loop, N concurrent clients):

| Clients | Baseline goodput (req/s) | FT goodput | Baseline p50 / p95 / p99 (ms) | FT p50 / p95 / p99 (ms) | Baseline error % | FT hard error % | FT shed % |
|---:|---:|---:|---|---|---:|---:|---:|
| c5 | 115 (104-127) | 251 (233-264) | 37 (34-40) / 85 (79-90) / 104 (90-118) | 15 (14-16) / 52 (50-55) / 66 (62-71) | 0.0 | 0.0 | 0.0 |
| c25 | 119 (112-124) | 211 (209-214) | 172 (166-184) / 440 (423-461) / 699 (677-715) | 93 (91-94) / 178 (175-180) / 257 (252-261) | 0.0 | 0.0 | 1.1 (1.0-1.2) |
| c75 | 125 (116-130) | 193 (179-203) | 446 (432-471) / 1470 (1417-1575) / 2236 (2184-2334) | 105 (99-112) / 209 (197-229) / 309 (290-340) | 0.0 | 0.0 | 19.9 (19.0-21.1) |
| c150 | 117 (108-126) | 188 (181-194) | 912 (844-949) / 3143 (2978-3273) / 4450 (4292-4684) | 106 (103-111) / 224 (216-234) / 326 (317-338) | 0.9 (0.5-1.5) | 0.0 | 38.4 (37.6-39.3) |
| cooldown | 113 (100-120) | 231 (218-248) | 39 (36-44) / 87 (82-95) / 106 (100-115) | 17 (16-17) / 54 (51-57) / 67 (62-72) | 0.0 | 0.0 | 0.0 |

Observations (generated by the experiment, not edited):

* Baseline accepts all work and queues it: latency grows with concurrency instead of requests being rejected.
* FT gateway/services shed excess load (HTTP 503 + Retry-After) above the in-flight limits and skip optional downstream calls above DEGRADE_INFLIGHT.

### S7 - Primary database loss (replication vs backup)

| Metric | Baseline | Fault-tolerant | Improvement |
|---|---:|---:|---:|
| Detection time, earliest signal (s) | 0.01 (0.01-0.01) | 0.08 (0.03-0.13) | -737 % |
|   - external /ready monitor (s) | 0.07 (0.03-0.10) | 0.08 (0.03-0.13) | -20 % |
|   - system-internal: gateway/breaker (s) | n/a | 1.20 (1.07-1.41) | n/a |
|   - first client-visible error (s) | 0.01 (0.01-0.01) | 0.51 (0.36-0.60) | -5040 % |
| Recovery duration, failure -> full service (s) | 21.3 (21.3-21.4) | 2.0 (1.5-2.5) | +90 % |
| Repair action duration (s) | 1.0 (1.0-1.1) | 0.8 (0.4-1.1) | +19 % |
| Total requests | 1920 | 1920 |  |
| Successful requests | 1070 (1068-1072) | 1916 (1913-1919) | +79 % |
| Failed requests | 850 (848-852) | 4 (1-7) | +100 % |
|   - deliberately shed (503 + Retry-After) | 0 | 0 |  |
| Failed requests inside failure window | 849 (847-851) | 4 (1-7) | +100 % |
| Recovered requests (masked by retry/failover/client retry) | 0 | 31 (21-48) | n/a |
| Degraded responses (reduced functionality) | 0 | 37 (20-55) |  |
| Duplicate requests sent | 0 | 0 |  |
| Duplicates answered from stored result | 0 | 0 |  |
| Latency p50 (ms) | 14 (13-16) | 8 (8-8) | +44 % |
| Latency p95 (ms) | 48 (46-49) | 42 (39-43) | +13 % |
| Latency p99 (ms) | 156 (64-333) | 870 (275-1411) | -458 % |
| User-visible downtime of student (any non-OK probe, s) | 21.2 (21.1-21.2) | 0.0 | +100 % |
| User-visible downtime of payment (any non-OK probe, s) | 21.2 (21.1-21.3) | 0.7 (0.0-2.0) | +97 % |
| User-visible downtime of academic (any non-OK probe, s) | 21.2 (21.1-21.2) | 0.0 | +100 % |
| Probe availability of student after failure start (%) | 46.95 (46.73-47.12) | 100.00 | |
| Probe availability of payment after failure start (%) | 46.96 (46.75-47.12) | 99.65 (99.47-99.74) | |
| Probe availability of academic after failure start (%) | 46.95 (46.87-47.12) | 100.00 | |

Per-operation failures (count) and p95 latency (ms):

| Operation | Baseline failed | FT failed | Baseline p95 | FT p95 | FT degraded | FT recovered |
|---|---:|---:|---:|---:|---:|---:|
| get_account | 44 (43-45) | 0 | 25 (17-40) | 21 (14-27) | 0 | 2 (1-3) |
| get_student | 379 (363-399) | 0 | 20 (19-22) | 18 (16-20) | 30 (16-43) | 6 (4-9) |
| get_transcript | 175 (166-181) | 4 (1-7) | 36 (34-38) | 24 (22-27) | 7 (4-12) | 4 (2-6) |
| post_grade | 132 (130-136) | 0 | 23 (21-25) | 69 (20-156) | 0 | 9 (4-15) |
| post_payment | 119 (112-127) | 0 | 64 (57-79) | 174 (65-386) | 0 | 11 (4-15) |

Data-consistency invariants checked directly in PostgreSQL after recovery:

| Invariant violation | Baseline | Fault-tolerant |
|---|---:|---:|
| duplicate_payments | 0 | 0 |
| ledger_mismatch_students | 0 | 0 |
| ledger_mismatch_amount | 0 | 0 |
| stuck_pending_payments | 0 | 0 |
| rolled_back_payments | 0 | 0 |
| duplicate_academic_records | 0 | 0 |
| lost_acknowledged_payments | 46 (41-53) | 0 |
| lost_acknowledged_grades | 50 (43-58) | 0 |
| partial_batches | 0 | 0 |
| batches_with_duplicates | 0 | 0 |
| **consistent (runs passing all invariants)** | 0/3 | 3/3 |

| Metric | Baseline (restore from backup) | Fault-tolerant (streaming standby + failover) |
|---|---:|---:|
| DB available again after (s) | 21.2 (21.2-21.3) | 1.4 (1.2-1.6) |
| acknowledged writes lost (RPO) | 95 (91-99) | 0 |

Observations (generated by the experiment, not edited):

* Backup-only strategy: pg_dump taken at start of the run; everything written afterwards is at risk.
* Standby streaming from primary, caught up before test: True.
