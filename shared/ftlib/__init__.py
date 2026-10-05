"""ftlib - shared fault-tolerance library for the university information system.

Modules
-------
config      environment-driven settings (FT_MODE=baseline|ft)
logging     structured JSON logging + request-scoped state
metrics     Prometheus metrics
resilience  retry with exponential backoff, circuit breaker, timeout helper
db          database access (baseline: connection per call; ft: pool + retry)
client      inter-service HTTP client (baseline: naive; ft: timeout/retry/breaker)
app         FastAPI application factory (middleware, health, metrics, fault injection)
"""

__version__ = "1.0.0"
