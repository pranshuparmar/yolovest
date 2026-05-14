-- Cached GTT lifecycle state for trades that have a broker-side GTT
-- attached. Updated each heartbeat by position-monitor's reconciler
-- (Kite values: active, triggered, disabled, expired, cancelled,
-- rejected, deleted). Used to:
--   - clear gtt_id when the GTT is no longer protecting the position
--     (so client-side detection takes over)
--   - render a status badge in the trade detail UI

ALTER TABLE trades ADD COLUMN gtt_status TEXT;
