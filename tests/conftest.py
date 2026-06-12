"""Suite-wide guards."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_store_backup(monkeypatch):
    """On a box where the operator armed HLBOT_STORE_BACKUP_S3 (B-STOREBKP),
    CLI tests that exercise `harvest-candles` would otherwise attempt a REAL
    S3 upload. Tests opt in by setting the env explicitly."""
    monkeypatch.delenv("HLBOT_STORE_BACKUP_S3", raising=False)
