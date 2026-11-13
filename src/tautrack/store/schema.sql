-- tautrack storage schema (Q3): SQLite with application-side rollups, following the
-- Zabbix history/trends model.
--
--   samples          raw scalar observations, retained ~14 days
--   trends           per-bucket min/avg/max/count, hourly and daily, retained years
--   sat_samples      raw per-satellite observations, retained ~14 days
--   sat_trends       per-bucket per-PRN aggregates
--   screens          occasional verbatim status screens, for forensics and fixtures
--
-- Aggregates keep MIN and MAX, not only the mean (FR-10). Averaging alone would hide
-- a ten-minute holdover inside a one-year view, which is exactly the event an operator
-- most needs to see.

PRAGMA journal_mode = WAL;        -- one writer plus UI readers without blocking
PRAGMA synchronous = NORMAL;      -- WAL makes this durable enough for a monitor
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per physical instrument. Single-device per instance for v1 (NFR-1), but the
-- schema does not preclude more.
CREATE TABLE IF NOT EXISTS devices (
    id           INTEGER PRIMARY KEY,
    manufacturer TEXT,
    model        TEXT,
    serial       TEXT UNIQUE,
    firmware     TEXT,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL
);

-- Metric names are interned so the sample table stores a small integer rather than a
-- repeated string. At ~58k rows/day that is a meaningful saving.
CREATE TABLE IF NOT EXISTS metrics (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    unit TEXT,
    kind TEXT NOT NULL DEFAULT 'gauge'   -- gauge | state | counter
);

-- Raw scalar samples. ts is unix seconds.
CREATE TABLE IF NOT EXISTS samples (
    device_id INTEGER NOT NULL REFERENCES devices(id),
    metric_id INTEGER NOT NULL REFERENCES metrics(id),
    ts        INTEGER NOT NULL,
    value     REAL,
    -- Text form for enum-valued metrics such as SmartClock mode, where the string is
    -- the datum and a numeric code alone would lose meaning.
    text      TEXT,
    PRIMARY KEY (device_id, metric_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);

-- Downsampled scalar aggregates. bucket is the period start in unix seconds and span
-- is its width ('hour' or 'day').
CREATE TABLE IF NOT EXISTS trends (
    device_id INTEGER NOT NULL REFERENCES devices(id),
    metric_id INTEGER NOT NULL REFERENCES metrics(id),
    span      TEXT NOT NULL,
    bucket    INTEGER NOT NULL,
    count     INTEGER NOT NULL,
    min_value REAL,
    avg_value REAL,
    max_value REAL,
    PRIMARY KEY (device_id, metric_id, span, bucket)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS trends_bucket ON trends(span, bucket);

-- Per-satellite raw observations. Sparse by nature: PRNs appear and disappear, so rows
-- exist only when a satellite was actually reported (FR-11).
CREATE TABLE IF NOT EXISTS sat_samples (
    device_id  INTEGER NOT NULL REFERENCES devices(id),
    ts         INTEGER NOT NULL,
    prn        INTEGER NOT NULL,
    elevation  INTEGER,
    azimuth    INTEGER,
    signal     INTEGER,
    tracked    INTEGER NOT NULL DEFAULT 0,
    acquiring  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, ts, prn)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS sat_samples_ts ON sat_samples(ts);
CREATE INDEX IF NOT EXISTS sat_samples_prn ON sat_samples(prn, ts);

-- Per-satellite aggregates. The appearance COUNT is kept deliberately alongside the
-- signal statistics: how often a PRN was present is as diagnostic as how strong it
-- was, and an average alone hides it (Q9).
CREATE TABLE IF NOT EXISTS sat_trends (
    device_id    INTEGER NOT NULL REFERENCES devices(id),
    span         TEXT NOT NULL,
    bucket       INTEGER NOT NULL,
    prn          INTEGER NOT NULL,
    count        INTEGER NOT NULL,   -- samples in which this PRN appeared at all
    tracked_count INTEGER NOT NULL,  -- of those, how many had it actually tracked
    min_signal   INTEGER,
    avg_signal   REAL,
    max_signal   INTEGER,
    min_elevation INTEGER,
    max_elevation INTEGER,
    PRIMARY KEY (device_id, span, bucket, prn)
) WITHOUT ROWID;

-- Verbatim screens, kept sparsely. Cheap forensics, and the raw material for new
-- replay fixtures when an unseen state variant turns up.
CREATE TABLE IF NOT EXISTS screens (
    id        INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    ts        INTEGER NOT NULL,
    signature TEXT,          -- state fingerprint; a new one is worth keeping
    body      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS screens_ts ON screens(ts);

-- Poller and parser health, distinct from the instrument's own telemetry (FR-22).
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    level     TEXT NOT NULL,     -- info | warning | error
    source    TEXT NOT NULL,     -- poller | transport | parser | rollup
    message   TEXT NOT NULL,
    detail    TEXT
);

CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
