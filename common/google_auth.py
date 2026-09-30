"""Service-account credentials from the environment. The key is never written to disk, logged or returned."""
import base64
import json

EE_SCOPES = ["https://www.googleapis.com/auth/earthengine",
             "https://www.googleapis.com/auth/cloud-platform"]


def service_account_info(raw):
    if not raw:
        raise RuntimeError("EE_SERVICE_ACCOUNT_JSON is not set.")
    raw = raw.strip()
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode()
    info = json.loads(raw)
    if info.get("type") != "service_account":
        raise RuntimeError("EE_SERVICE_ACCOUNT_JSON is not a service-account key.")
    return info


def credentials(raw, scopes=None):
    from google.oauth2 import service_account
    return service_account.Credentials.from_service_account_info(
        service_account_info(raw), scopes=scopes or EE_SCOPES)


_ee_ready = False


def init_ee(settings):
    global _ee_ready
    if _ee_ready:
        return
    import ee
    ee.Initialize(credentials(settings.ee_service_account_json), project=settings.ee_project)
    _ee_ready = True
