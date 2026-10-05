"""Environment-driven configuration.

Every tunable is an environment variable so that the same container image can
run as the *baseline* or the *fault-tolerant* system (FT_MODE). No secrets are
hard-coded: DATABASE_URL must be supplied by the environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _urls(name: str) -> list[str]:
    return [u.strip().rstrip("/") for u in os.getenv(name, "").split(",") if u.strip()]


@dataclass(frozen=True)
class Settings:
    service_name: str
    mode: str                      # "baseline" | "ft"
    database_url: str
    instance_id: str
    enable_fault_injection: bool
    student_urls: list[str] = field(default_factory=list)

    # --- resilience parameters (only used when mode == "ft") ---
    http_timeout_s: float = 1.0          # per-attempt timeout for service-to-service calls
    retry_attempts: int = 3              # total attempts (1 original + 2 retries)
    retry_base_delay_s: float = 0.1      # first backoff delay; doubles each retry
    retry_max_delay_s: float = 1.0
    downstream_deadline_s: float = 2.0   # overall time budget (all attempts) for one service-to-service call
    breaker_failure_threshold: int = 5   # consecutive failures that open the breaker
    breaker_reset_timeout_s: float = 5.0 # OPEN -> HALF_OPEN after this long
    db_retry_deadline_s: float = 6.0     # total time budget for DB retries on one request
    db_pool_size: int = 10
    db_pool_timeout_s: float = 2.0
    max_inflight: int = 60               # hard load-shedding limit (ft only)
    degrade_inflight: int = 30           # soft limit: optional work is skipped (ft only)
    payment_lease_s: float = 1.5         # lease on a PENDING payment attempt
    payment_rollback_after_s: float = 6.0  # stale PENDING payments are rolled back after this
    processor_latency_ms: float = 20.0   # simulated external payment processor latency
    cache_max_entries: int = 20000

    @property
    def is_ft(self) -> bool:
        return self.mode == "ft"


def load_settings(service_name: str) -> Settings:
    mode = os.getenv("FT_MODE", "baseline").strip().lower()
    if mode not in ("baseline", "ft"):
        raise ValueError(f"FT_MODE must be 'baseline' or 'ft', got {mode!r}")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return Settings(
        service_name=service_name,
        mode=mode,
        database_url=dsn,
        instance_id=os.getenv("INSTANCE_ID", f"{service_name}-{os.getpid()}"),
        enable_fault_injection=_bool("ENABLE_FAULT_INJECTION", False),
        student_urls=_urls("STUDENT_SERVICE_URLS"),
        http_timeout_s=_float("HTTP_TIMEOUT_S", 1.0),
        retry_attempts=_int("RETRY_ATTEMPTS", 3),
        retry_base_delay_s=_float("RETRY_BASE_DELAY_S", 0.1),
        retry_max_delay_s=_float("RETRY_MAX_DELAY_S", 1.0),
        downstream_deadline_s=_float("DOWNSTREAM_DEADLINE_S", 2.0),
        breaker_failure_threshold=_int("BREAKER_FAILURE_THRESHOLD", 5),
        breaker_reset_timeout_s=_float("BREAKER_RESET_TIMEOUT_S", 5.0),
        db_retry_deadline_s=_float("DB_RETRY_DEADLINE_S", 6.0),
        db_pool_size=_int("DB_POOL_SIZE", 10),
        db_pool_timeout_s=_float("DB_POOL_TIMEOUT_S", 2.0),
        max_inflight=_int("MAX_INFLIGHT", 60),
        degrade_inflight=_int("DEGRADE_INFLIGHT", 30),
        payment_lease_s=_float("PAYMENT_LEASE_S", 1.5),
        payment_rollback_after_s=_float("PAYMENT_ROLLBACK_AFTER_S", 6.0),
        processor_latency_ms=_float("PROCESSOR_LATENCY_MS", 20.0),
        cache_max_entries=_int("CACHE_MAX_ENTRIES", 20000),
    )
