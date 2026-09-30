"""Object storage: Google Cloud Storage in production, a local folder in mock mode."""
import os
from datetime import timedelta


def _safe(path):
    if path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"unsafe path: {path}")
    return path


class LocalStorage:
    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _p(self, path):
        return os.path.join(self.root, _safe(path))

    def put_bytes(self, path, data, content_type=None):
        p = self._p(path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def get_bytes(self, path):
        with open(self._p(path), "rb") as f:
            return f.read()

    def exists(self, path):
        return os.path.exists(self._p(path))

    def download_to(self, path, local_path):
        with open(local_path, "wb") as f:
            f.write(self.get_bytes(path))

    def url(self, path):
        return f"/files/{_safe(path)}"


class GcsStorage:
    def __init__(self, bucket, credentials=None, signed=False):
        from google.cloud import storage   # imported lazily: not needed in mock mode
        self._client = storage.Client(credentials=credentials, project=getattr(credentials, "project_id", None))
        self.bucket_name, self.bucket = bucket, self._client.bucket(bucket)
        self.credentials, self.signed = credentials, signed

    def put_bytes(self, path, data, content_type=None):
        self.bucket.blob(_safe(path)).upload_from_string(data, content_type=content_type)

    def get_bytes(self, path):
        from google.api_core.exceptions import NotFound
        try:
            return self.bucket.blob(_safe(path)).download_as_bytes()
        except NotFound:
            raise FileNotFoundError(path)

    def exists(self, path):
        return self.bucket.blob(_safe(path)).exists()

    def download_to(self, path, local_path):
        self.bucket.blob(_safe(path)).download_to_filename(local_path)

    def url(self, path):
        if self.signed:
            return self.bucket.blob(_safe(path)).generate_signed_url(
                version="v4", expiration=timedelta(hours=1), credentials=self.credentials)
        return f"https://storage.googleapis.com/{self.bucket_name}/{_safe(path)}"
