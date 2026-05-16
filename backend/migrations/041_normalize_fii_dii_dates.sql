-- Normalise fii_dii_daily.date to ISO YYYY-MM-DD.
--
-- The NSE fii_dii feed returns dates as "15-May-2026" (display format).
-- upsert_fii_dii used to write that verbatim, which broke every
-- date-comparison lookup against `date('now', ...)` (ISO format).
-- Result: the Institutional Flows dashboard read empty even after
-- successful ingestion, and risk-check's get_latest_fii_dii returned
-- the wrong "latest" row due to string-DESC ordering.
--
-- Convert rows matching the "DD-Mon-YYYY" pattern to ISO. Other rows
-- (already ISO, or a future NSE format) are left alone.
--
-- SQLite has no native month-name parser, so do it via a giant CASE.

UPDATE fii_dii_daily
SET date =
    substr(date, 8, 4)                      -- YYYY
    || '-' || CASE substr(date, 4, 3)
        WHEN 'Jan' THEN '01' WHEN 'Feb' THEN '02' WHEN 'Mar' THEN '03'
        WHEN 'Apr' THEN '04' WHEN 'May' THEN '05' WHEN 'Jun' THEN '06'
        WHEN 'Jul' THEN '07' WHEN 'Aug' THEN '08' WHEN 'Sep' THEN '09'
        WHEN 'Oct' THEN '10' WHEN 'Nov' THEN '11' WHEN 'Dec' THEN '12'
    END
    || '-' || substr(date, 1, 2)            -- DD
WHERE length(date) = 11
  AND substr(date, 3, 1) = '-'
  AND substr(date, 7, 1) = '-';
