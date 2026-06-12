"""Off-host candle-store S3 backup (B-STOREBKP) — no network, fake uploads.

What makes this trustworthy as an unattended redundancy layer:
  1. SigV4 signing is pinned to the AWS-published key-derivation vector —
     a refactor that breaks signing fails here, not silently on the box.
  2. Inert by default: no HLBOT_STORE_BACKUP_S3 → no-op, no HTTP, no creds
     lookup (the feature ships OFF; arming it is the operator's call).
  3. Throttle + weekly restore points: the loop's minutes-apart iterations
     can't hammer S3, and a corrupted store overwriting the stable object
     can't destroy the only copy.
  4. A backup failure warns but never reddens the harvest timer.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta

import pytest

from hl_bot.backtest import store_backup as sb

NOW = datetime(2026, 6, 12, 18, 0, tzinfo=UTC)
ENV = {
    "HLBOT_STORE_BACKUP_S3": "bkt/hl-bot/candle_store",
    "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "SECRETEXAMPLE",
    "AWS_REGION": "ap-northeast-1",
}


def _store(tmp_path, n=2):
    root = tmp_path / "data" / "candle_store"
    root.mkdir(parents=True)
    for i in range(n):
        with gzip.open(root / f"C{i}_1m.json.gz", "wt", encoding="utf-8") as fh:
            json.dump([{"t": 60_000 * i, "c": 100.0}], fh)
    return root


def _recorder(puts):
    return lambda url, data, headers: puts.append((url, data, headers))


# --- signing


def test_signing_key_matches_aws_published_vector():
    # https://docs.aws.amazon.com/general/latest/gr/signature-v4-examples.html
    key = sb.derive_signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20120215", "us-east-1", "iam"
    )
    assert key.hex() == (
        "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"
    )


def test_put_headers_sign_temp_creds_and_carry_scope():
    creds = sb.AwsCreds("AK", "SK", session_token="TOK")
    headers = sb.sigv4_put_headers(
        host="b.s3.us-east-1.amazonaws.com", path="/p/candle_store.tar",
        payload_hash="ab" * 32, region="us-east-1", creds=creds, now=NOW,
    )
    auth = headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AK/20260612/us-east-1/s3/aws4_request")
    # The session token must be sent AND signed (temp creds are rejected otherwise).
    assert headers["x-amz-security-token"] == "TOK"
    assert (
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token"
        in auth
    )
    assert headers["x-amz-date"] == "20260612T180000Z"
    assert "host" not in headers  # urllib sets Host from the URL


# --- payload


def test_tar_round_trips_every_store_file(tmp_path):
    root = _store(tmp_path, n=3)
    payload, n = sb.tar_store(root)
    assert n == 3
    with tarfile.open(fileobj=io.BytesIO(payload)) as tf:
        names = tf.getnames()
        assert names == sorted(f"C{i}_1m.json.gz" for i in range(3))
        for name in names:
            assert tf.extractfile(name).read() == (root / name).read_bytes()


# --- orchestration


def test_disabled_without_env_no_http_no_creds(tmp_path):
    root = _store(tmp_path)
    res = sb.backup_store(
        root, env={}, now=NOW,
        http_put=lambda *a: pytest.fail("disabled backup must not PUT"),
        imds_get=lambda p: pytest.fail("disabled backup must not probe IMDS"),
    )
    assert res.skipped == "disabled" and not res.keys


def test_uploads_stable_plus_weekly_then_throttles(tmp_path):
    root = _store(tmp_path)
    puts: list = []
    res = sb.backup_store(root, env=ENV, now=NOW, http_put=_recorder(puts))
    y, w, _ = NOW.isocalendar()
    stamp = f"{y}W{w:02d}"
    assert res.keys == [
        "hl-bot/candle_store/candle_store.tar",
        f"hl-bot/candle_store/weekly/candle_store.{stamp}.tar",
    ]
    assert [u for u, _, _ in puts] == [
        f"https://bkt.s3.ap-northeast-1.amazonaws.com/{k}" for k in res.keys
    ]
    assert res.bytes_uploaded == 2 * len(puts[0][1])
    state = json.loads(sb.state_path(root).read_text())
    assert state["last_weekly"] == stamp and state["files"] == 2

    # Minutes later: throttled, no HTTP.
    res2 = sb.backup_store(
        root, env=ENV, now=NOW + timedelta(minutes=10),
        http_put=lambda *a: pytest.fail("throttled backup must not PUT"),
    )
    assert res2.skipped and "last backup" in res2.skipped

    # Next hour, same ISO week: only the stable object is rewritten.
    puts.clear()
    res3 = sb.backup_store(
        root, env=ENV, now=NOW + timedelta(hours=2), http_put=_recorder(puts)
    )
    assert res3.keys == ["hl-bot/candle_store/candle_store.tar"] and len(puts) == 1

    # A week on: a fresh dated restore point.
    puts.clear()
    res4 = sb.backup_store(
        root, env=ENV, now=NOW + timedelta(days=8), http_put=_recorder(puts)
    )
    assert any("/weekly/" in k for k in res4.keys) and len(puts) == 2


def test_bucket_without_prefix_uses_root_keys(tmp_path):
    root = _store(tmp_path)
    puts: list = []
    res = sb.backup_store(
        root, env={**ENV, "HLBOT_STORE_BACKUP_S3": "justbucket"},
        now=NOW, http_put=_recorder(puts),
    )
    assert res.keys[0] == "candle_store.tar"
    assert res.keys[1].startswith("weekly/candle_store.")


def test_empty_store_skips_before_creds(tmp_path):
    root = tmp_path / "data" / "candle_store"
    root.mkdir(parents=True)
    res = sb.backup_store(
        root, env={"HLBOT_STORE_BACKUP_S3": "bkt"}, now=NOW,
        http_put=lambda *a: pytest.fail("empty store must not PUT"),
        imds_get=lambda p: pytest.fail("empty store must not probe IMDS"),
    )
    assert res.skipped == "store empty"


def test_creds_fall_back_to_imds_instance_role(tmp_path):
    root = _store(tmp_path)
    imds = {
        "meta-data/iam/security-credentials/": "hlbot-ssm\n",
        "meta-data/iam/security-credentials/hlbot-ssm": json.dumps(
            {"AccessKeyId": "ROLEAK", "SecretAccessKey": "ROLESK", "Token": "ROLETOK"}
        ),
        "meta-data/placement/region": "us-east-1",
    }
    puts: list = []
    env = {"HLBOT_STORE_BACKUP_S3": "bkt/p"}  # no key envs, no region env
    res = sb.backup_store(
        root, env=env, now=NOW, http_put=_recorder(puts), imds_get=imds.__getitem__
    )
    assert not res.error and puts
    _, _, headers = puts[0]
    assert "Credential=ROLEAK/" in headers["Authorization"]
    assert headers["x-amz-security-token"] == "ROLETOK"
    assert puts[0][0].startswith("https://bkt.s3.us-east-1.amazonaws.com/")


def test_no_creds_anywhere_reports_error_without_raising(tmp_path):
    root = _store(tmp_path)

    def dead_imds(path):
        raise OSError("no metadata service")

    res = sb.backup_store(
        root, env={"HLBOT_STORE_BACKUP_S3": "bkt"}, now=NOW,
        http_put=lambda *a: pytest.fail("no creds, no PUT"), imds_get=dead_imds,
    )
    assert res.error and "credentials" in res.error


# --- CLI wiring (mirrors the sync-peer pins in test_candle_store.py)


def _fresh_store_cli(monkeypatch, tmp_path):
    import hl_bot.backtest.store as store_mod

    monkeypatch.setenv("HLBOT_DB", str(tmp_path / "t.sqlite"))
    monkeypatch.setattr(store_mod, "worst_store_lag", lambda pairs, **k: ("BTC_1m", 3.0))
    monkeypatch.setattr(
        store_mod, "harvest",
        lambda *a, **k: pytest.fail("harvest must not run on a fresh store"),
    )


def test_harvest_candles_backs_up_even_on_the_fresh_skip_path(monkeypatch, tmp_path):
    from hl_bot.cli.main import harvest_candles

    _fresh_store_cli(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(sb, "backup_store", lambda *a, **k: calls.append(True) or sb.BackupResult(skipped="disabled"))
    harvest_candles(if_stale_minutes=30.0)
    assert calls == [True]


def test_harvest_candles_backup_failure_never_turns_the_timer_red(monkeypatch, tmp_path):
    from hl_bot.cli.main import harvest_candles

    _fresh_store_cli(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise OSError("s3 unreachable")

    monkeypatch.setattr(sb, "backup_store", boom)
    harvest_candles(if_stale_minutes=30.0)  # must not raise / Exit
