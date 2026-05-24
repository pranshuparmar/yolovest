-- Honest-edge metric: the walk-forward Sharpe of the model trading its
-- raw argmax decisions, with no threshold gate. Unlike the headline
-- (threshold-tuned) Sharpe — which can be inflated by a cherry-picked
-- high-probability tail — this captures whether the model has a genuine
-- edge on its natural decisions. Promotion is gated on this so a
-- net-losing model can't reach production via a flattering tuned number.
ALTER TABLE model_versions ADD COLUMN argmax_sharpe REAL;
