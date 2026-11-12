# ER Patient Status Board (local / offline)

Flask + SQLite. Runs on one laptop, binds to `127.0.0.1` only, no internet or CDN needed.

## Local setup

Python 3.12. The full walkthrough, including what each local file is, is in `developer-guide.md`.

```bash
git clone https://github.com/anoopsaxena3262/workiva-ai.git
cd workiva-ai
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once; needs network once
.venv/bin/python app.py
```

Starting the app creates three local files. They are gitignored. You do not create them, and there is no `.env` file.

| Path | Role |
| --- | --- |
| `er_board.db` | SQLite database, schema from `schema.sql` (version 2). Empty until the JSON import below. Override the path with `ER_DB`. |
| `uploads/` | Photo files from admin photo entry. |
| `.secret_key` | Signs the admin session. |

* Wall display: <http://127.0.0.1:5050/> (full-screen the browser, F11). Auto-refreshes every 10 s; shows a red banner if the server stops answering.
* Sample data is already in the repo, at the project root: `er_status_board_data.json` (next to `app.py`). Admin: <http://127.0.0.1:5050/admin> → **JSON import** → upload that file, mode *Replace*, type `REPLACE`. Rules are the other root file, `import_rules.yaml`.
* Optional shell variables, set on the start command: `ADMIN_PIN=1234` requires a PIN for /admin. `PORT=5050` is the default (5000 is taken by AirPlay on macOS). `ER_RULES` overrides `import_rules.yaml`. Audit entries are stamped `local-admin`.
* An `er_board.db` from an older schema version is refused at startup and left untouched. Move it aside, start again, and re-import.

## Data-integrity rules

* The JSON is stored verbatim (every import's exact text + SHA-256 is archived in `imports`). Unknown keys are kept in `extra_json`.
* Nothing is corrected silently. Each chart is **complete** or **incomplete**, computed at display time. The team board shows complete charts only. Incomplete charts stay on `/admin`, grouped for a person to verify and correct; saving a chart with no remaining issues moves it onto the board. Issues:
  duplicate patient IDs · beds shared by several patients (not for rooms typed "Overflow") · no bed · invalid/missing age · invalid triage (e.g. `0`) ·
  status "waiting for bed" while a bed is set ·
  missing / `N/A` / implausible vitals · vitals time before arrival · non-ISO arrival time · adult in a pediatric room ·
  a bed that is not a room in the room master · a room whose `occupied_by` is not this patient.
  Saving a chart during verify writes that patient into the room record (`occupied_by`, status occupied) and releases the previous room when it listed them.
  Patient ID is editable on that verify form only. Saving a corrected id updates the chart and the room record that listed the previous id. A complete chart keeps its id.
  Vitals later than the snapshot time are noted on the admin page, not as a per-chart hold. A discharged chart with no issues still appears in the discharged group on the team board.
* Triage level and status text are shown as imported and are not compared. That choice is recorded in `rule-decision-log.md`.
* Each rule is `enabled: true` or `false` in `import_rules.yaml`. True holds a chart for a person. False does not. The snapshot is stored either way. `rules.py` reads that file on every check. A new rule is a new block in the file; the example is in the header. The JSON import page edits the same file.
* The plausibility windows (HR 20-250, SpO2 50-100, temp 90-110 °F, systolic 50-260, diastolic < systolic, adult = 18+) and the triage group names are **policy choices in `app.py`**, not facts from the file. Flags only warn; values are never changed. SpO2 < 92 is coloured red as a clinical cue and is not a data flag.
* Everything unrecognised in the source is kept in `extra_json` and shown read-only on the admin patient page.
* Import is all-or-nothing (one transaction).
* "Remove" hides a patient from the board (kept in the database). **Replace import does wipe the live tables**, including manual and photo entries — but first copies the complete prior state into `board_backups` (same transaction). Merge import never deletes.
* Nothing is guessed or filled in: a missing vitals time stays empty (flagged), ages are stored exactly as given (flagged if not a whole number 0-130), source `role` and `specialty` are kept separately.
* Public board is read-only. `/admin` only answers requests from the laptop itself (loopback), has CSRF protection, and every create / edit / archive / import / photo upload is written to the append-only `audit_log` with before→after values.
* With the supplied file: system status is `OFFLINE - fallback data snapshot`, so the board shows a red "NOT LIVE DATA" bar. Temperatures are treated as °F (`temp_f`).

## ER diagram

```
facilities 1───< doctors   (patients.doctor_id is optional: the supplied file links no doctor to any patient)
    │ 1
    ├──────< rooms 1───< patients >───1 doctors
    │ 1                      │ 1
    └──────< patients        └───< vitals          (latest row per patient shown on board)

board_backups (full copy of live tables taken before each Replace)
photos  (uploaded images; transcribed → patients/vitals, linked via audit_log.detail)
imports (raw JSON archive)      audit_log (append-only trail of every admin action)

facilities(id PK, name, system_status, snapshot_at, extra_json, updated_at)
doctors   (id PK, facility_id FK, name, role, specialty, extra_json)
rooms     (id PK, facility_id FK, name, room_type, status, occupied_by, extra_json)
patients  (id PK, source_id [NOT unique], facility_id FK, name, age [verbatim], sex, room_id FK, bed,
           chief_complaint, triage_level, status, doctor_id FK, notes, arrival_time,
           origin import|manual|photo, active, extra_json, created_at, updated_at)
vitals    (id PK, patient_id FK, recorded_at [NULL if unknown], hr, bp, spo2, temp, origin, extra_json)
photos    (id PK, filename, original_name, sha256, uploaded_at, actor, transcribed)
imports   (id PK, ts, actor, mode, sha256, raw_json, summary)
audit_log (id PK, ts, actor, action, entity, entity_id, detail JSON)
```
Full DDL: `schema.sql` (applied automatically at startup).

## Tests

No server, and no use of the local `er_board.db` or `uploads/`. The suite points `ER_DB` at a temp file and imports `er_status_board_data.json` from the project root into it.

```bash
.venv/bin/python -m unittest discover tests -v
```

Photos: jpg/png/gif/webp only. Photo save to an existing patient writes only fields you typed; blanks keep the current value.

## Files
`app.py` routes · `db.py` connection + audit helper · `importer.py` JSON → SQLite · `rules.py` and `import_rules.yaml` · `templates/`, `static/` UI.

Setup, how to run, and the design are in `developer-guide.md`. Choices from the review are in `decisions.md`. The rule catalog is `rule-decision-log.md`.
