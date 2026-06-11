# The Moonshot Sleeve (Path B)

A hard-capped, loss-bounded, high-leverage convex sleeve — the explicit
lottery ticket from [`ROADMAP_TO_1M.md`](ROADMAP_TO_1M.md) Path B. Expected
value is flat-to-negative; the point is the fat right tail. Everything about
it is designed so that the worst case (sleeve → $0) cannot touch the core.

## Why a sub-account, not a notional partition

A 5–10x leveraged book sharing cross-margin with the core means one gap move
can consume core collateral before stops execute. A notional partition is
accounting; a Hyperliquid **sub-account** is a collateral wall: the sleeve can
literally go to zero and the core never feels it.

## Isolation stack (every layer independent of the core)

| Layer | Core | Moonshot |
|---|---|---|
| HL account | main trader address | dedicated sub-account |
| Process | `hlbot-run.service` | `hlbot-moonshot.service` |
| DB / data dir | `data/hlbot.sqlite` | `data/moonshot/hlbot.sqlite` |
| Kill file | `data/KILL` | `data/moonshot/KILL` |
| Configs | `configs/*.yaml` | `configs/moonshot/*.yaml` |
| API wallet | core wallet env | own wallet env (`HL_BOT_API_WALLET_ENV`) |
| Backups | Litestream replica 1 | Litestream replica 2 |

The profile mechanics live in `config.py::Settings` (`HLBOT_PROFILE`): the
profile derives its own data dir, so the sticky kill switch, equity-floor
backstop, scorecards and promotion ladder all operate per-profile with zero
extra code.

## Funding rules (enforced by human transfer, written here so they're checkable)

1. **Fund with ≤5% of total capital.** At $5k capital that's a $250 sleeve.
2. **Never auto-refill.** No code path tops the sleeve up; transfers are manual.
3. **Top-up at most monthly**, and only if core equity is at or above its own
   30d high-water-mark (the core earns the right to gamble, not the reverse).
4. **Sweep profits**: when sleeve equity exceeds 2× funded amount, move the
   excess back to the core. Lock in the right tail when it happens.
5. **Per-bet risk**: isolated margin, 5–10x, margin per bet ≈ 1% of *total*
   capital, hard SL/TP/max-hold from `LiqCascadeConfig`.

## Sleeve-level guardrails (in `configs/moonshot/liq_cascade_v1.yaml`)

- 24h realized loss ≥ ~30% of funded → supervisor pauses the agent.
- 7d realized loss ≥ ~50% of funded → demote.
- Equity floor: the runner's standard 75%-of-30d-HWM check runs against the
  sleeve's own DB → sleeve-local sticky `data/moonshot/KILL`.
- The sleeve agent earns live the same way core agents do: its own paper soak
  and promotion ladder. Leverage doesn't skip gates.

## Strategy

`liq_cascade_v1` (liquidation-cascade momentum) — the WS feed flags
liquidation trades; clustered forced selling on a high-volume coin tends to
overshoot, then continue briefly. The `hlbot ws` service now persists every
liquidation event to `data/liq_log.jsonl`; that accumulating dataset is what
calibrates `min_liq_notional_usd` and validates the edge (candle history has
no liquidation flags, so `hlbot confirm` can't cover this strategy yet —
hence the longer paper soak in its ladder instead of a G0 stamp).

## Host setup

```bash
# 1. Create the sub-account in the HL UI; transfer the sleeve stake (≤5%).
# 2. Approve an API wallet for it; store creds:
cat > /etc/hl-bot/moonshot.env <<EOF
HL_TRADER_ADDRESS=0x<subaccount>
HL_BOT_API_WALLET_ENV=/home/hlbot/.config/hermes/hl-bot-moonshot-wallet.env
HLBOT_MOONSHOT_ARGS=                      # empty = paper soak first
EOF
chmod 640 /etc/hl-bot/moonshot.env && chown root:hlbot /etc/hl-bot/moonshot.env
# 3. Enable (paper):
systemctl enable --now hlbot-moonshot
# 4. After the paper soak promotes it, flip:
#    HLBOT_MOONSHOT_ARGS="--live --execution taker"   (cascades are taker by nature)
```
