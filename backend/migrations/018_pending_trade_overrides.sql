ALTER TABLE pending_trades ADD COLUMN user_signal_type TEXT;
ALTER TABLE pending_trades ADD COLUMN user_entry_price REAL;
ALTER TABLE pending_trades ADD COLUMN user_target_price REAL;
ALTER TABLE pending_trades ADD COLUMN user_stop_loss_price REAL;
ALTER TABLE pending_trades ADD COLUMN user_product TEXT;
ALTER TABLE pending_trades ADD COLUMN user_notes TEXT;
ALTER TABLE pending_trades ADD COLUMN is_override INTEGER DEFAULT 0;
ALTER TABLE pending_trades ADD COLUMN is_manual INTEGER DEFAULT 0;
