"""All configuration comes from environment variables (see .env.example). Nothing secret lives in code."""
import os
from dataclasses import dataclass


def _i(name, default):
    return int(os.environ.get(name, default))


def _b(name, default):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes")


@dataclass
class Settings:
    mode: str                       # "real" (EE + GCS + Redis + Cloud Tasks) or "mock" (fully local)
    ee_project: str
    ee_service_account_json: str    # raw JSON or base64 of the key file. NEVER logged or returned.
    bucket: str
    redis_url: str
    redis_token: str
    tasks_project: str
    tasks_location: str
    tasks_queue: str
    worker_url: str
    tasks_invoker_sa: str           # service account whose OIDC token Cloud Tasks presents to the worker
    ip_salt: str
    signed_urls: bool
    # limits
    max_area_km2: int
    pre_min_days: int
    pre_max_days: int
    post_min_days: int
    post_max_days: int
    max_gap_days: int
    data_lag_days: int
    min_images_warn: int
    rate_preflight_per_hour: int
    rate_jobs_per_hour: int
    global_jobs_per_day: int
    job_timeout_min: int
    poll_seconds: int
    region_allowlist: bool
    mock_dir: str
    roads_motorable_only: bool
    settlement_buffer_m: int


def load_settings() -> Settings:
    mode = os.environ.get("BACKEND_MODE", "real").lower()
    return Settings(
        mode=mode,
        ee_project=os.environ.get("EE_PROJECT", ""),
        ee_service_account_json=os.environ.get("EE_SERVICE_ACCOUNT_JSON", ""),
        bucket=os.environ.get("GCS_BUCKET", ""),
        redis_url=os.environ.get("UPSTASH_REDIS_REST_URL", ""),
        redis_token=os.environ.get("UPSTASH_REDIS_REST_TOKEN", ""),
        tasks_project=os.environ.get("TASKS_PROJECT", os.environ.get("EE_PROJECT", "")),
        tasks_location=os.environ.get("TASKS_LOCATION", "us-central1"),
        tasks_queue=os.environ.get("TASKS_QUEUE", "flood-jobs"),
        worker_url=os.environ.get("WORKER_URL", ""),
        tasks_invoker_sa=os.environ.get("TASKS_INVOKER_SA", ""),
        ip_salt=os.environ.get("IP_SALT", "dev-salt-change-me"),
        signed_urls=_b("SIGNED_URLS", False),
        max_area_km2=_i("MAX_AREA_KM2", 5000),
        pre_min_days=_i("PRE_MIN_DAYS", 7), pre_max_days=_i("PRE_MAX_DAYS", 30),
        post_min_days=_i("POST_MIN_DAYS", 7), post_max_days=_i("POST_MAX_DAYS", 14),
        max_gap_days=_i("MAX_GAP_DAYS", 365),
        data_lag_days=_i("DATA_LAG_DAYS", 2),
        min_images_warn=_i("MIN_IMAGES_WARN", 2),
        rate_preflight_per_hour=_i("RATE_PREFLIGHT_PER_HOUR", 20),
        rate_jobs_per_hour=_i("RATE_JOBS_PER_HOUR", 3),
        global_jobs_per_day=_i("GLOBAL_JOBS_PER_DAY", 30),
        job_timeout_min=_i("JOB_TIMEOUT_MIN", 90),
        poll_seconds=_i("POLL_SECONDS", 20),
        region_allowlist=_b("REGION_ALLOWLIST", mode == "real"),
        mock_dir=os.environ.get("MOCK_DIR", ".local_storage"),
        roads_motorable_only=_b("ROADS_MOTORABLE_ONLY", True),
        settlement_buffer_m=_i("SETTLEMENT_BUFFER_M", 250),
    )
