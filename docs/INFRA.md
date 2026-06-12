# Infrastructure: 24/7 deployment, signal & execution

How to run hl-bot live around the clock, what infra it needs, and where money
actually buys an edge versus where it's premature optimization. Read alongside
[`GO_LIVE.md`](GO_LIVE.md) (the gated switch) and [`ROADMAP_TO_1M.md`](ROADMAP_TO_1M.md).

## The honest headline first

**At this bot's current strategy cadence (5-min cron, carry/mean-reversion), latency
and colocation are NOT the bottleneck — edge and capital are.** Do not spend on
low-latency/colo infra until you run a strategy that needs it (market-making,
liquidation sniping). A $10–40/mo VPS + WebSocket + solid monitoring runs the
current and proposed (carry) strategies perfectly well. Spend the infra budget in
the order below, and stop when the next tier doesn't change PnL.

## Current state (from the repo)

- A box (EC2) runs `hlbot tick/ingest/supervisor` on a 5-min cron + a daily
  Telegram scorecard via systemd; `auto_tuner.py` syncs the SQLite DB off-box via
  `scp` and proposes param tweaks. Hermes provides cron/Telegram.
- One funded trader account (`HL_TRADER_ADDRESS`), signed by a separate **API wallet** in
  `~/.config/hermes/hl-bot-api-wallet.env` (0600). Good separation already.
- Gaps: single box (no failover), SQLite synced by scp (fragile), polling REST
  (no WebSocket), no real metrics/alerting beyond Telegram, secrets + address
  partly hardcoded.

## Target deployment (24/7)

```
┌─ trade host (always-on VM) ───────────────────────────────┐
│  systemd:                                                  │
│   hlbot-tick.timer   every 1–5 min → tick/ingest/supervise │
│   hlbot-report.timer daily          → track-record/Telegram│
│   (later) hlbot-ws.service          → WebSocket market view │
│  process: one bot, restart=always, journald logs           │
│  storage: SQLite on local SSD (WAL) + nightly backup off-box│
│  secrets: API-wallet env 0600 (or a secrets manager)       │
└────────────────────────────────────────────────────────────┘
        │ outbound HTTPS/WSS to api.hyperliquid.xyz
        ▼  alerts → Telegram/PagerDuty;  metrics → Prometheus/Grafana
```

Key changes from today:
1. **systemd, not bare cron.** A `.timer` for the periodic tick + a long-running
   `.service` (Restart=always) for the future WebSocket loop. Survives reboots,
   logs to journald, auto-restarts on crash.
2. **Stop scp-syncing the DB.** Run the auto-tuner / track-record *on the trade
   host*, or replicate the DB with Litestream (continuous SQLite → S3) instead of
   periodic scp. Removes the main fragility.
3. **Real monitoring** (below) — a silent dead bot is the worst failure mode.
4. **Backups** — Litestream or a cron `VACUUM INTO` to object storage; the DB is
   your ground truth and track record.

A minimal hardened unit (sketch) — see `B14a` to codify these in-repo:
```ini
# /etc/systemd/system/hlbot-tick.service
[Service]
WorkingDirectory=/opt/hl-bot
ExecStart=/usr/bin/uv run hlbot femr_tick --live   # entries route per-agent (auto)
EnvironmentFile=/etc/hl-bot/env        # HL_ADDRESS, TG_*, HLBOT_DB
Restart=on-failure
# /etc/systemd/system/hlbot-tick.timer → OnUnitActiveSec=60
```

## Infra tiers (spend in this order; stop when PnL stops improving)

| Tier | What | ~Cost/mo | Buys you |
|---|---|---:|---|
| 0 | Cheap VPS (Hetzner/DO/Vultr), systemd, WSS, Litestream backups, Telegram alerts | **$5–40** | Reliable 24/7 for carry/MR. **Start here.** |
| 1 | + Prometheus/Grafana + heartbeat alerting (Healthchecks.io/PagerDuty) | +$0–20 | You find out *immediately* when it's down or bleeding |
| 2 | Region-pinned cloud VM near HL's API (measure RTT; many HL traders use Tokyo / `ap-northeast-1`) | $20–120 | Lower order latency — only matters for fast strategies |
| 3 | Dedicated/bare-metal low-latency box, tuned NIC/kernel, persistent WS | $100–500 | Real maker/market-making competitiveness |
| 4 | Colocation near HL infra, redundant feeds | $500–3k+ | HFT/MM/liquidation edges. **Only with a proven edge that needs it.** |

For now, **Tier 0 + Tier 1** is the right spend. Tier 2+ is justified only once a
latency-sensitive strategy confirms in the backtester *and* shows latency is the
binding constraint.

## High-quality / high-speed **signal** — what to invest in

In rough ROI order for *this* bot:
1. **WebSocket market data (free, high ROI).** Replace REST polling with
   `wss://api.hyperliquid.xyz/ws`: subscribe `l2Book`, `trades`, `allMids`,
   `activeAssetCtx` (funding/OI), and `userEvents`/`userFills` for your account.
   Gives sub-second mids, real order-book depth, and — crucially — **real
   liquidation data** (the `trades` feed flags liquidations), which fixes the
   dead `liq_cascade` agent (REVIEW C6). This is the single highest-value signal
   upgrade and it's free. (Backlog B10.)
2. **Order-book features.** With L2 depth you get spread, imbalance, microprice —
   essential for maker pricing and for any short-horizon edge. The current bot
   only sees mids.
3. **Funding/OI history at scale.** The backtester already pulls `fundingHistory`;
   for the carry strategies, widen the universe (50–100 coins) and store funding
   snapshots locally for cross-sectional ranking.
4. **Paid data — only if a strategy needs it.** CoinGlass/Amberdata/Kaiko for
   aggregated liquidations, cross-exchange funding/basis, or alt-signal. $100–2k/mo.
   Justify each subscription with a backtest that improves *after costs*.
5. **Compute for research.** A box that can run nightly walk-forward sweeps over
   the universe (the loop's job). Cheap; mostly your existing VM off-hours.

What NOT to buy yet: tick-level historical archives, ML feature stores, alt-data
feeds — premature until a confirmed edge demonstrably wants them.

## High-speed **execution** — what to invest in

1. **Maker (post-only) execution — built, highest ROI.** Earning the spread
   instead of paying it is worth more than any latency upgrade at this cadence.
   The default (`--execution auto`) routes carry agents maker, and quotes are
   priced at the touch from the WS book (`maker_price`), not a stale mid.
2. **API/agent wallet (done).** Sign with a dedicated API wallet, never the funded
   key; the bot already does this. Keep the env 0600 / in a secrets manager.
3. **Rate-limit awareness.** HL uses weight-based REST limits and per-address
   limits scaled by volume; a WS connection avoids most REST weight. Batch order
   actions, cache `meta`, and back off on 429s (the retry helper is a start).
4. **Latency (Tier 2+, only when it matters).** Measure RTT to the API from a few
   regions; pin the VM to the lowest. For MM you'd want persistent WS, connection
   warmth, and possibly colo — but confirm the edge needs it first.
5. **Resilience of execution.** Idempotent orders via `cloid` (done), fill
   verification (done), reconciliation against live positions (done), and the
   maker resting-order lifecycle (done). These matter more than raw speed for not
   losing money to bugs.

## Reliability & monitoring (non-negotiable for 24/7)

- **Process supervision:** systemd `Restart=always`, watchdog, journald.
- **Heartbeat:** the tick pings Healthchecks.io / a dead-man switch each run; you
  get paged if it misses (silent death is the worst outcome).
- **Metrics:** export equity, open notional vs cap, 24h PnL, rejection rate,
  reconciliation events → Prometheus/Grafana; alert on thresholds.
- **Alerts:** Telegram for info, PagerDuty/phone for "bot down" or guardrail trip
  (the `telegram_alert` hook exists; wire severities).
- **Backups:** Litestream (SQLite → S3) so the track record survives a host loss.
- **Kill switch:** documented in `GO_LIVE.md` (disable agent / stop timer).
- **Clock:** keep NTP synced — the bot timestamps everything in ms.

## Security

- API wallet only (no withdrawal rights if HL supports scoping); funded key never
  on the box. Env 0600 or a secrets manager (SOPS/age, Vault, cloud KMS).
- Least-privilege host, firewall outbound to HL + Telegram only, SSH keys only.
- Trader address comes from env only (`HL_TRADER_ADDRESS`/`HL_ADDRESS`); the bot
  refuses to touch the exchange when unset (M6/B13 resolved — no silent default).

## Bottom line / recommended spend now

1. **Tier 0 VPS + systemd + Litestream + heartbeat alerting** (~$10–40/mo total).
2. **WebSocket market view** (free) — sub-second mids, L2 depth, real liquidations.
3. **Maker execution** (built) — earn the spread.
4. Everything else (colo, paid data, bare-metal) waits until a confirmed,
   latency-/data-hungry edge proves it pays for itself. At this account size, an
   extra $500/mo of infra is a larger drag than the latency it removes is worth.
