"""Vault retargeting (B16b): HL_VAULT_ADDRESS resolution + exchange passthrough.

The failure mode under test: the operator points the bot at a vault, account
reads follow the vault, but orders quietly execute on the personal account.
The resolver must therefore (a) take precedence over every other address env,
(b) reach the SDK Exchange as ``vault_address`` (it rides in the signature and
the /exchange payload — ``account_address`` alone only redirects reads), and
(c) refuse malformed values loudly rather than fall back.
"""

from __future__ import annotations

import pytest
from eth_account import Account

import hl_bot.exec.orders as orders
from hl_bot.config import Settings, resolve_vault_address
from hl_bot.ops.doctor import _check_vault

VAULT = "0x" + "ab" * 20
PERSONAL = "0x" + "11" * 20


@pytest.fixture(autouse=True)
def _clean_addr_env(monkeypatch):
    for k in ("HL_VAULT_ADDRESS", "HL_TRADER_ADDRESS", "HL_ADDRESS"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# resolve_vault_address
# ---------------------------------------------------------------------------


def test_unset_resolves_none():
    assert resolve_vault_address() is None


def test_blank_resolves_none(monkeypatch):
    monkeypatch.setenv("HL_VAULT_ADDRESS", "   ")
    assert resolve_vault_address() is None


def test_valid_address_resolves(monkeypatch):
    monkeypatch.setenv("HL_VAULT_ADDRESS", VAULT)
    assert resolve_vault_address() == VAULT


@pytest.mark.parametrize("bad", [
    "vault",                 # not an address at all
    "0x123",                 # too short
    "ab" * 21,               # right-ish length, no 0x prefix
    "0x" + "gg" * 20,        # non-hex chars
    "0x" + "ab" * 20 + "1",  # too long
])
def test_malformed_raises_never_falls_back(monkeypatch, bad):
    monkeypatch.setenv("HL_VAULT_ADDRESS", bad)
    with pytest.raises(ValueError, match="HL_VAULT_ADDRESS malformed"):
        resolve_vault_address()


# ---------------------------------------------------------------------------
# Trader-address resolution: every read must follow the vault
# ---------------------------------------------------------------------------


def test_trader_address_prefers_vault(monkeypatch):
    monkeypatch.setenv("HL_TRADER_ADDRESS", PERSONAL)
    monkeypatch.setenv("HL_VAULT_ADDRESS", VAULT)
    assert orders._resolve_trader_address() == VAULT


def test_trader_address_without_vault_unchanged(monkeypatch):
    monkeypatch.setenv("HL_TRADER_ADDRESS", PERSONAL)
    assert orders._resolve_trader_address() == PERSONAL


def test_trader_address_malformed_vault_raises(monkeypatch):
    # A typo'd vault must abort, not silently trade the personal account.
    monkeypatch.setenv("HL_TRADER_ADDRESS", PERSONAL)
    monkeypatch.setenv("HL_VAULT_ADDRESS", "0xnope")
    with pytest.raises(ValueError):
        orders._resolve_trader_address()


def test_settings_read_address_prefers_vault(monkeypatch):
    monkeypatch.setenv("HL_ADDRESS", PERSONAL)
    monkeypatch.setenv("HL_VAULT_ADDRESS", VAULT)
    assert Settings.from_env().hl_address == VAULT


def test_settings_read_address_without_vault(monkeypatch):
    monkeypatch.setenv("HL_ADDRESS", PERSONAL)
    assert Settings.from_env().hl_address == PERSONAL


# ---------------------------------------------------------------------------
# build_exchange: vault_address must reach the SDK Exchange
# ---------------------------------------------------------------------------


class _FakeExchange:
    def __init__(self, *, wallet, base_url, account_address, vault_address):
        self.wallet = wallet
        self.base_url = base_url
        self.account_address = account_address
        self.vault_address = vault_address


def _wallet_env(tmp_path):
    priv = "0x" + "11" * 32
    addr = Account.from_key(priv).address
    p = tmp_path / "wallet.env"
    p.write_text(f"HL_BOT_API_PRIVATE_KEY={priv}\nHL_BOT_API_WALLET_ADDRESS={addr}\n")
    p.chmod(0o600)
    return p


def test_build_exchange_passes_vault_address(monkeypatch, tmp_path):
    monkeypatch.setattr(orders, "Exchange", _FakeExchange)
    monkeypatch.setattr(orders, "Info", lambda *a, **k: object())
    monkeypatch.setattr(orders, "HL_VAULT_ADDRESS", VAULT)
    monkeypatch.setattr(orders, "HL_TRADER_ADDRESS", VAULT)
    ex, _info, wallet = orders.build_exchange(env_path=_wallet_env(tmp_path))
    assert ex.vault_address == VAULT
    assert ex.account_address == VAULT
    assert wallet.address == Account.from_key("0x" + "11" * 32).address


def test_build_exchange_no_vault_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(orders, "Exchange", _FakeExchange)
    monkeypatch.setattr(orders, "Info", lambda *a, **k: object())
    monkeypatch.setattr(orders, "HL_VAULT_ADDRESS", None)
    monkeypatch.setattr(orders, "HL_TRADER_ADDRESS", PERSONAL)
    ex, _info, _wallet = orders.build_exchange(env_path=_wallet_env(tmp_path))
    assert ex.vault_address is None
    assert ex.account_address == PERSONAL


# ---------------------------------------------------------------------------
# doctor: report state, never raise
# ---------------------------------------------------------------------------


def test_doctor_vault_unset_is_ok():
    c = _check_vault()
    assert c.level == "ok"
    assert "personal account" in c.detail


def test_doctor_vault_valid_shows_retarget(monkeypatch):
    monkeypatch.setenv("HL_VAULT_ADDRESS", VAULT)
    c = _check_vault()
    assert c.level == "ok"
    assert VAULT in c.detail


def test_doctor_vault_malformed_reports_crit(monkeypatch):
    monkeypatch.setenv("HL_VAULT_ADDRESS", "0xnope")
    c = _check_vault()  # must not raise
    assert c.level == "crit"
    assert "malformed" in c.detail
