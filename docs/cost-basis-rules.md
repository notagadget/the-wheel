# Cost basis rules

## Formula

```
cost_basis (per share) = assignment_price - (total_premium / 100)
```

`total_premium` is the running cumulative net credit for the entire cycle, in dollars (not per-share). Dividing by 100 converts to per-share (1 contract = 100 shares).

This is a VIRTUAL GENERATED column in SQLite. It is never set directly.

## What counts toward total_premium

| Event | Effect on total_premium |
|---|---|
| Sell CSP (open) | + net_credit |
| Buy CSP to close early | - abs(net_credit) |
| CSP expires worthless | + 0 (no change) |
| Roll CSP: buy to close | - abs(net_credit) of closing leg |
| Roll CSP: sell to open | + net_credit of opening leg |
| Stock assigned | no change (assignment_price is set separately) |
| Sell CC | + net_credit |
| Buy CC to close | - abs(net_credit) |
| CC expires worthless | + 0 |
| Roll CC: buy to close | - abs(net_credit) of closing leg |
| Roll CC: sell to open | + net_credit of opening leg |
| Stock called away | no change (triggers realized_pnl calc) |

## Worked example

**Setup**: 1 contract on XYZ, $50 strike CSP.

### Step 1 — Sell CSP

- Sold 1x $50 put for $1.50/share → `net_credit = $150`
- `total_premium = $150`
- `assignment_price = null` (not yet assigned)
- `cost_basis = null`

### Step 2 — Roll CSP (defensive, put is ITM)

- Bought back put for $2.00/share → debit $200
- Sold new put at $48 strike for $1.80/share → credit $180
- Net roll: -$20
- `total_premium = $150 - $200 + $180 = $130`

### Step 3 — Assigned at $48 strike

- Fill price: $48.00/share
- `assignment_price = $48.00`
- `shares_held = 100`
- `total_premium = $130` (unchanged)
- `cost_basis = $48.00 - ($130 / 100) = $48.00 - $1.30 = $46.70`

### Step 4 — Sell CC at $49 strike

- Sold 1x $49 call for $0.90/share → `net_credit = $90`
- `total_premium = $130 + $90 = $220`
- `cost_basis = $48.00 - ($220 / 100) = $48.00 - $2.20 = $45.80`

### Step 5 — CC expires worthless

- Synthetic close at $0 — no change to `total_premium`
- `cost_basis = $45.80` (unchanged)

### Step 6 — Sell another CC at $49 strike

- Sold 1x $49 call for $0.70/share → `net_credit = $70`
- `total_premium = $220 + $70 = $290`
- `cost_basis = $48.00 - ($290 / 100) = $48.00 - $2.90 = $45.10`

### Step 7 — Called away at $49 strike

- Fill: $49.00/share
- `realized_pnl = ($49.00 - $45.10) × 100 = $390`
- Cycle → `CLOSED`

## Common mistakes to avoid

**Do not** recalculate cost_basis from scratch at call time — the VIRTUAL column handles it.

**Do not** add the full assignment strike to total_premium — assignment price and premium are tracked separately and combined in the formula.

**Do not** exclude roll debits from total_premium — a roll that costs net $20 reduces the total credit and therefore increases cost basis. This is correct behavior.

**Do not** reset total_premium when transitioning from SHORT_PUT to LONG_STOCK — premium from the put phase reduces cost basis in the stock phase.
