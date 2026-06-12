"""Off-host S3 backup of the rolling candle store (B-STOREBKP).

B-STORESYNC made the two same-host store clones redundant against either
harvester dying, but both clones share ONE host: losing it permanently
invalidates the multi-week 1m sample every P0 experiment clock is waiting on
(1m API retention is ~3.5d — the history cannot be refetched). Litestream
replicates the SQLite DBs off-host but not these gzipped JSON files.

This module uploads a tarball of the store to S3 using ONLY the stdlib —
the box has neither boto3 nor the aws CLI — via SigV4 request signing.
Credentials come from ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` env or
the EC2 IMDSv2 instance role (the same no-static-keys path Litestream uses).

Opt-in and inert by default: set ``HLBOT_STORE_BACKUP_S3=bucket[/prefix]``
(deploy boxes: in /etc/hl-bot/env, which every hlbot-*.service loads). Each
run overwrites a stable ``candle_store.tar`` object and, once per ISO week,
writes a dated ``weekly/candle_store.<YYYY>W<WW>.tar`` restore point so a
corrupted store synced over the stable object can't destroy the only copy.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import tarfile
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .store import store_dir

ENV_BUCKET = "HLBOT_STORE_BACKUP_S3"
ENV_MIN_MINUTES = "HLBOT_STORE_BACKUP_MIN_MINUTES"
# Just under the hourly harvest timer, so the timer itself never skips while
# the ralph loop's per-iteration step (minutes apart) doesn't hammer S3.
DEFAULT_MIN_MINUTES = 55.0
STABLE_NAME = "candle_store.tar"
STATE_NAME = ".candle_backup_state.json"
IMDS_BASE = "http://169.254.169.254/latest"


@dataclass
class BackupResult:
    skipped: str | None = None  # "disabled" / "store empty" / "last backup …"
    keys: list[str] = field(default_factory=list)
    bytes_uploaded: int = 0
    error: str | None = None


@dataclass
class AwsCreds:
    access_key: str
    secret_key: str
    session_token: str | None = None


# --- SigV4 (https://docs.aws.amazon.com/IAM/latest/UserGuide/create-signed-request.html)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def derive_signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret_key).encode(), date)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return key


def sigv4_put_headers(
    *,
    host: str,
    path: str,
    payload_hash: str,
    region: str,
    creds: AwsCreds,
    now: datetime,
) -> dict[str, str]:
    """Headers (incl. Authorization) for a single S3 PUT with no query string."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if creds.session_token:
        headers["x-amz-security-token"] = creds.session_token
    signed = ";".join(sorted(headers))
    canonical = "\n".join(
        ["PUT", path, "", *(f"{k}:{headers[k]}" for k in sorted(headers)), "", signed,
         payload_hash]
    )
    scope = f"{date}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope,
         hashlib.sha256(canonical.encode()).hexdigest()]
    )
    signature = hmac.new(
        derive_signing_key(creds.secret_key, date, region, "s3"),
        string_to_sign.encode(), hashlib.sha256,
    ).hexdigest()
    out = {k: v for k, v in headers.items() if k != "host"}  # urllib sets Host
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={creds.access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return out


# --- credentials / region (env first, then the IMDSv2 instance role)


def _imds_get(path: str, timeout: float = 2.0) -> str:
    req = urllib.request.Request(
        f"{IMDS_BASE}/api/token", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    token = urllib.request.urlopen(req, timeout=timeout).read().decode()
    req = urllib.request.Request(
        f"{IMDS_BASE}/{path}", headers={"X-aws-ec2-metadata-token": token}
    )
    return urllib.request.urlopen(req, timeout=timeout).read().decode()


def resolve_aws_creds(
    env: Mapping[str, str], imds_get: Callable[[str], str] | None = None
) -> AwsCreds | None:
    access, secret = env.get("AWS_ACCESS_KEY_ID"), env.get("AWS_SECRET_ACCESS_KEY")
    if access and secret:
        return AwsCreds(access, secret, env.get("AWS_SESSION_TOKEN") or None)
    imds_get = imds_get or _imds_get
    try:
        role = imds_get("meta-data/iam/security-credentials/").strip().splitlines()[0]
        doc = json.loads(imds_get(f"meta-data/iam/security-credentials/{role}"))
        return AwsCreds(doc["AccessKeyId"], doc["SecretAccessKey"], doc.get("Token"))
    except Exception:  # noqa: BLE001 — no role / no metadata service = no creds
        return None


def resolve_region(
    env: Mapping[str, str], imds_get: Callable[[str], str] | None = None
) -> str:
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        if env.get(var):
            return env[var]
    imds_get = imds_get or _imds_get
    try:
        return imds_get("meta-data/placement/region").strip()
    except Exception:  # noqa: BLE001
        return "us-east-1"


# --- payload + local state


def tar_store(root: Path) -> tuple[bytes, int]:
    """One tar of every store file, sorted by name; returns (bytes, file count)."""
    files = sorted(root.glob("*.json.gz"))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for p in files:
            tf.add(p, arcname=p.name)
    return buf.getvalue(), len(files)


def state_path(root: Path) -> Path:
    # Beside the store, not inside it, so the marker never lands in the tar.
    return root.parent / STATE_NAME


def _load_state(path: Path) -> dict[str, Any]:
    try:
        out = json.loads(path.read_text())
        return out if isinstance(out, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt marker just re-uploads
        return {}


def _http_put(url: str, data: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    urllib.request.urlopen(req, timeout=120).read()  # non-2xx raises HTTPError


def backup_store(
    root: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    http_put: Callable[[str, bytes, dict[str, str]], None] | None = None,
    imds_get: Callable[[str], str] | None = None,
) -> BackupResult:
    """Upload the store tarball to S3 if configured and due. Raises only on
    upload/signing failures — the CLI wrapper turns those into warnings."""
    env = os.environ if env is None else env
    target = (env.get(ENV_BUCKET) or "").strip().strip("/")
    if not target:
        return BackupResult(skipped="disabled")
    root_dir = store_dir(root)
    now = now or datetime.now(UTC)

    min_minutes = float(env.get(ENV_MIN_MINUTES) or DEFAULT_MIN_MINUTES)
    state_file = state_path(root_dir)
    state = _load_state(state_file)
    last = state.get("last_success_utc")
    if last:
        try:
            age_min = (now - datetime.fromisoformat(last)).total_seconds() / 60.0
        except ValueError:
            age_min = None
        if age_min is not None and 0 <= age_min < min_minutes:
            return BackupResult(
                skipped=f"last backup {age_min:.0f}m ago < {min_minutes:g}m"
            )

    payload, n_files = tar_store(root_dir)
    if n_files == 0:
        return BackupResult(skipped="store empty")

    creds = resolve_aws_creds(env, imds_get)
    if creds is None:
        return BackupResult(error="no AWS credentials (env keys or instance role)")
    region = resolve_region(env, imds_get)
    bucket, _, prefix = target.partition("/")

    iso_year, iso_week, _ = now.isocalendar()
    week_stamp = f"{iso_year}W{iso_week:02d}"
    keys = [f"{prefix}/{STABLE_NAME}" if prefix else STABLE_NAME]
    if state.get("last_weekly") != week_stamp:
        base = f"{prefix}/" if prefix else ""
        keys.append(f"{base}weekly/candle_store.{week_stamp}.tar")

    host = f"{bucket}.s3.{region}.amazonaws.com"
    payload_hash = hashlib.sha256(payload).hexdigest()
    put = http_put or _http_put
    for key in keys:
        path = "/" + quote(key, safe="/")
        headers = sigv4_put_headers(
            host=host, path=path, payload_hash=payload_hash,
            region=region, creds=creds, now=now,
        )
        headers["content-type"] = "application/x-tar"
        put(f"https://{host}{path}", payload, headers)

    state_file.write_text(json.dumps({
        "last_success_utc": now.isoformat(),
        "last_weekly": week_stamp,
        "bucket": bucket,
        "keys": keys,
        "files": n_files,
        "bytes": len(payload),
    }))
    return BackupResult(keys=keys, bytes_uploaded=len(payload) * len(keys))
