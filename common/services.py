"""Wire up the app: real services (EE / GCS / Redis / Cloud Tasks) or the local mock stack."""
from dataclasses import dataclass

from . import validation
from .config import Settings, load_settings


@dataclass
class Services:
    settings: Settings
    store: object
    storage: object
    queue: object
    info_fn: object        # (country, level, name, parent, pre, post) -> preflight info dict
    regions: dict
    driver: object = None
    roads_fn: object = None


_services = None


def build_services(settings=None):
    s = settings or load_settings()
    regions = validation.load_regions()
    if s.mode == "mock":
        from . import fakes
        from .queue import InlineQueue
        from .storage import LocalStorage
        from .store import MemoryStore
        storage = LocalStorage(s.mock_dir)
        svc = Services(s, MemoryStore(), storage, None, fakes.FakeInfo(regions), regions,
                       driver=fakes.FakeDriver(storage), roads_fn=fakes.fake_roads)
        svc.queue = InlineQueue(lambda job_id: __import__("worker.runner", fromlist=["step"]).step(
            job_id, worker_deps(svc)), delay_scale=0.02)   # mock polls run ~50x faster
        return svc
    from . import google_auth
    from .queue import CloudTasksQueue
    from .storage import GcsStorage
    from .store import UpstashStore
    creds = google_auth.credentials(s.ee_service_account_json)
    svc = Services(s, UpstashStore(s.redis_url, s.redis_token),
                   GcsStorage(s.bucket, creds, s.signed_urls), CloudTasksQueue(s, creds),
                   _real_info_fn(s), regions)
    return svc


def _real_info_fn(s):
    def info(country, level, name, parent, pre, post):
        from . import google_auth
        from pipeline import flood_pipeline as fp
        google_auth.init_ee(s)
        return fp.preflight_info(country, level, name, parent, pre, post)
    return info


def get_services():
    global _services
    if _services is None:
        _services = build_services()
    return _services


def set_services(svc):
    global _services
    _services = svc


def worker_deps(svc):
    from worker.runner import Deps
    driver, roads_fn = svc.driver, svc.roads_fn
    if driver is None:                       # real worker: built lazily so the API never imports geo libs
        from worker.driver import EEDriver
        driver = svc.driver = EEDriver(svc.settings)
    if roads_fn is None:
        from pipeline.roads_exposure import compute_exposure
        roads_fn = svc.roads_fn = compute_exposure
    return Deps(svc.store, svc.storage, driver, svc.settings, roads_fn)
