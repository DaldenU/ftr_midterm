# Reliability campaign results

Observation window T = 240 s per system version; every application instance is killed with exponential inter-failure times (injected mean 15.0 s per instance, accelerated life test). Failures/outages below are what a user sees through the gateway (>= 2 consecutive failed 10 Hz probes).

## Component level (application instances)

| Metric | Baseline (1 replica/service) | Fault-tolerant (2 replicas/service) |
|---|---:|---:|
| Instances | 3 | 6 |
| Failures injected | 31 | 95 |
| MTTF_c (observed, s) | 12.6 | 13.4 |
| MTTR_c (observed kill->ready, s) | 10.62 | 1.72 |
| Failure rate lambda_c (1/s) | 0.0793 | 0.0744 |
| A_c = MTTF/(MTTF+MTTR) | 54.28 % | 88.65 % |

## Service level (user-visible, per service)

### student

| Metric | Baseline | Fault-tolerant |
|---|---:|---:|
| Outages (failures) | 9 | 7 |
| Downtime (s) | 94.77 | 5.62 |
| MTTF (s) | 16.1 | 33.5 |
| MTTR (s) | 10.53 | 0.80 |
| MTBF = T/n (s) | 26.7 | 34.3 |
| Failure rate (1/s of uptime) | 0.0620 | 0.0299 |
| Availability observed | 60.51 % | 97.66 % |
| Availability theory (A_c, ideal redundancy) | 54.28 % | 98.71 % |

### payment

| Metric | Baseline | Fault-tolerant |
|---|---:|---:|
| Outages (failures) | 13 | 5 |
| Downtime (s) | 138.04 | 3.61 |
| MTTF (s) | 7.8 | 47.3 |
| MTTR (s) | 10.62 | 0.72 |
| MTBF = T/n (s) | 18.5 | 48.0 |
| Failure rate (1/s of uptime) | 0.1275 | 0.0212 |
| Availability observed | 42.48 % | 98.50 % |
| Availability theory (A_c, ideal redundancy) | 54.28 % | 98.71 % |

### academic

| Metric | Baseline | Fault-tolerant |
|---|---:|---:|
| Outages (failures) | 11 | 3 |
| Downtime (s) | 148.17 | 2.51 |
| MTTF (s) | 8.3 | 79.2 |
| MTTR (s) | 13.47 | 0.84 |
| MTBF = T/n (s) | 21.8 | 80.0 |
| Failure rate (1/s of uptime) | 0.1198 | 0.0126 |
| Availability observed | 38.26 % | 98.95 % |
| Availability theory (A_c, ideal redundancy) | 54.28 % | 98.71 % |

## Whole system (any service down)

| Metric | Baseline | Fault-tolerant |
|---|---:|---:|
| Failures | 10 | 15 |
| Downtime (s) | 205.01 | 11.74 |
| MTTF (s) | 3.5 | 15.2 |
| MTTR (s) | 20.50 | 0.78 |
| Availability observed | 14.58 % | 95.11 % |
| Availability theory (3 services in series) | 16.00 % | 96.18 % |
