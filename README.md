# Penny Scout

Daily scanner for US-listed stocks under $5 showing unusual trading activity.

**Not investment advice.** The score measures how *unusual* a day's trading was — not
whether a stock will go up. High activity is what both real breakouts and pump-and-dumps
look like on day one.

## How it works

`scripts/fetch_pennies.py` runs weekdays at 21:30 UTC (~90 min after the US close) via
GitHub Actions:

1. Pulls every NASDAQ / NYSE / AMEX ticker (~7,200) from Nasdaq's public screener API
2. Keeps those priced $0.10–$5.00 with ≥300K shares and ≥$1M traded
3. Shortlists the 60 biggest movers, pulls 30 days of history for each
4. Ranks by relative volume, move size and liquidity
5. Writes `data/scan.json`, and re-checks every past candidate into `data/history.json`

No API key required — both endpoints are public (they need a browser User-Agent header).

## Track record

Every name the scanner lists is followed for 30 days and the result is published in the
app, wins and losses alike. A scanner that only shows today's list is selling you
survivorship bias.

## Filters and why

| Filter | Reason |
|---|---|
| Price ≥ $0.10 | Sub-dime spreads cost 15–20% round trip |
| Price ≤ $5.00 | SEC penny-stock definition |
| Dollar volume ≥ $1M | Below this you may not find a buyer when you want to exit |
| No warrants / units / rights | They don't behave like common shares |
| Listed only, no OTC | OTC has no reporting requirements and no reliable exit |

## Local run

```bash
python3 scripts/fetch_pennies.py
```

Stdlib only, no dependencies.
