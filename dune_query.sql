-- Dune Analytics query: pull all BTC 5m market trades since launch.
-- Paste into dune.com/queries/new and run on Polygon dataset.
-- Polymarket uses the CTF-Exchange contract on Polygon. Trade events hit
-- the OrderFilled log on 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E.
--
-- Output: one row per fill with market slug, side, price, size, timestamp.
-- Join with markets table to pull outcome / resolution.

WITH btc5m AS (
  SELECT
    m.condition_id,
    m.slug,
    m.question,
    m.created_at,
    m.token_ids
  FROM polymarket.markets m
  WHERE m.slug LIKE 'btc-updown-5m-%'
    AND m.created_at >= TIMESTAMP '2026-02-01'
),
fills AS (
  SELECT
    t.block_time,
    t.condition_id,
    t.asset_id,
    t.side,                    -- 'BUY' or 'SELL'
    t.price,                   -- in USDC per share, 0.00 - 1.00
    t.size,                    -- shares
    t.taker,
    t.tx_hash
  FROM polymarket.trades t
  WHERE t.block_time >= TIMESTAMP '2026-02-01'
)
SELECT
  b.slug,
  b.condition_id,
  f.block_time,
  f.asset_id,
  f.side,
  f.price,
  f.size,
  -- Window-relative second (0-300) if the slug timestamp is parseable:
  EXTRACT(EPOCH FROM f.block_time) - CAST(SUBSTRING(b.slug FROM 'btc-updown-5m-([0-9]+)') AS BIGINT) AS t_in_window
FROM fills f
JOIN btc5m b ON b.condition_id = f.condition_id
ORDER BY b.slug, f.block_time
;
-- Export to CSV from Dune and drop in ./pm_data/dune_trades.csv for the
-- backtest (backtest_polymarket.py auto-detects it).
