-- ER Board schema (SQLite). Applied idempotently by db.init_db().
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS facilities (
  id            TEXT PRIMARY KEY,           -- source id from JSON (or generated)
  name          TEXT NOT NULL,
  system_status TEXT,                       -- verbatim, e.g. "OFFLINE - fallback data snapshot"
  snapshot_at   TEXT,                       -- snapshot_generated_at from the source
  extra_json    TEXT,                       -- any other source fields, verbatim
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS doctors (
  id          TEXT PRIMARY KEY,
  facility_id TEXT REFERENCES facilities(id) ON DELETE SET NULL,
  name        TEXT NOT NULL,
  role        TEXT,                         -- e.g. Attending / Resident
  specialty   TEXT,
  extra_json  TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
  id          TEXT PRIMARY KEY,
  facility_id TEXT REFERENCES facilities(id) ON DELETE SET NULL,
  name        TEXT NOT NULL,                -- "Bed 4", "Trauma 1"
  room_type   TEXT,
  status      TEXT,                         -- available / occupied ...
  occupied_by TEXT,                         -- patient source id; import keeps the file value, verify updates it
  extra_json  TEXT
);

CREATE TABLE IF NOT EXISTS patients (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id      TEXT,                      -- id in the JSON (NOT unique: source may repeat ids); NULL for manual
  facility_id    TEXT REFERENCES facilities(id) ON DELETE SET NULL,
  name           TEXT NOT NULL,
  age            ,                          -- no type affinity: a number or text is stored exactly as given
  sex            TEXT,
  room_id        TEXT REFERENCES rooms(id) ON DELETE SET NULL,
  bed            TEXT,                      -- display label (room name or free text)
  chief_complaint TEXT,
  triage_level   TEXT,                      -- verbatim ("2", "ESI-2", "Critical")
  status         TEXT,                      -- current clinical status
  doctor_id      TEXT REFERENCES doctors(id) ON DELETE SET NULL,
  notes          TEXT,
  arrival_time   TEXT,                      -- verbatim; source formats are inconsistent
  origin         TEXT NOT NULL DEFAULT 'import' CHECK (origin IN ('import','manual','photo')),
  active         INTEGER NOT NULL DEFAULT 1, -- 0 = removed from board (never hard-deleted)
  extra_json     TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_patients_source ON patients(source_id);

CREATE TABLE IF NOT EXISTS vitals (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_id  INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
  recorded_at TEXT,                         -- NULL when the source gave no time (never invented)
  hr          REAL,                         -- REAL affinity: non-numeric source values (e.g. "N/A")
  bp          TEXT,                         -- "120/80"   are kept verbatim as text
  spo2        REAL,
  temp        REAL,                         -- degrees F
  origin      TEXT NOT NULL DEFAULT 'import' CHECK (origin IN ('import','manual','photo')),
  extra_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_vitals_patient ON vitals(patient_id, recorded_at);

CREATE TABLE IF NOT EXISTS photos (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  filename      TEXT NOT NULL,              -- stored name in uploads/
  original_name TEXT,
  sha256        TEXT,
  uploaded_at   TEXT NOT NULL,
  actor         TEXT,
  transcribed   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS imports (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  actor      TEXT,
  mode       TEXT NOT NULL,                 -- 'replace' | 'merge'
  sha256     TEXT NOT NULL,
  raw_json   TEXT NOT NULL,                 -- exact payload, kept for traceability
  summary    TEXT
);

-- Full copy of live tables taken just before a Replace import wipes them.
CREATE TABLE IF NOT EXISTS board_backups (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,
  actor     TEXT,
  reason    TEXT,
  dump_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,
  actor     TEXT NOT NULL,
  action    TEXT NOT NULL,                  -- import / create / update / archive / restore / photo_upload ...
  entity    TEXT NOT NULL,
  entity_id TEXT,
  detail    TEXT                            -- JSON: before/after diff
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- Import rules are not stored here. They are read from import_rules.yaml.
