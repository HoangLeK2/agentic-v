"""Reloadable hashed-token identity store."""

import json
from pathlib import Path

from services.buzz_adapter.auth import BuzzIdentity, BuzzIdentityStore


class FileIdentityProvider:
    def __init__(self, path: Path, pepper: str):
        self.path = path
        self.pepper = pepper
        self._mtime_ns: int | None = None
        self._store: BuzzIdentityStore | None = None

    def get(self) -> BuzzIdentityStore:
        stat = self.path.stat()
        if self._store is not None and stat.st_mtime_ns == self._mtime_ns:
            return self._store
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("BUZZ_IDENTITIES_FILE must contain a JSON array")
        identities = tuple(BuzzIdentity(**item) for item in payload)
        self._store = BuzzIdentityStore(identities=identities, pepper=self.pepper)
        self._mtime_ns = stat.st_mtime_ns
        return self._store
