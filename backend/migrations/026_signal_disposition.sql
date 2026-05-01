-- Track per-signal disposition so users can see why a generated BUY/SELL
-- never became a trade (risk-rejected, llm-rejected, pending, executed).
ALTER TABLE signals ADD COLUMN disposition TEXT DEFAULT 'pending';
ALTER TABLE signals ADD COLUMN disposition_reason TEXT;

-- Disposition values: 'pending' (just generated), 'risk_rejected', 'llm_rejected',
-- 'awaiting_approval' (manual mode pending_trade), 'executed' (trade created),
-- 'recently_rejected_dedup' (manual mode user rejection cooldown).
CREATE INDEX IF NOT EXISTS idx_signals_disposition_created
  ON signals (disposition, created_at);
