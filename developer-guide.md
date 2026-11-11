# Developer guide

Local setup, how to run, and how the ER Patient Status Board is built. Product choices from the 2026-10-07 review are in `decisions.md`. The rule catalog is `rule-decision-log.md`. Day-to-day behavior is also summarized in `README.md`.

## What it is

A Flask app and a SQLite file on one laptop. The public page is a wall board of emergency-department charts. The admin page imports a JSON snapshot, lists charts that need a person, and lets that person correct them. There is no network service, no login directory, and no model call.

The supplied snapshot is `er_status_board_data.json`. On that file the import is 1 facility, 10 doctors, 28 rooms, 60 patients, and 59 vitals. `system_status` is an offline fallback, so the board shows a red “NOT LIVE DATA” bar.

## Local setup

Python 3.12 is what this checkout uses. From the repo root:

```bash
cd workiva-ai
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` is Flask 3 and PyYAML. The install needs network once. After that the app runs offline.

The first request creates `er_board.db` from `schema.sql` and sets SQLite `user_version` to 2. An older database file is refused at startup and left untouched. Move it aside and import the JSON again.

These paths are created at runtime and are gitignored: `er_board.db`, `uploads/`, `.secret_key`.

## How to run

```bash
.venv/bin/python app.py
```

The process binds to `127.0.0.1` only. The default port is **5050** because macOS AirPlay often holds 5000.

| Page | URL |
| --- | --- |
| Team board | http://127.0.0.1:5050/ |
| Admin | http://127.0.0.1:5050/admin |
| JSON import and rules | http://127.0.0.1:5050/admin/import |

Load the snapshot from **JSON import**: choose `er_status_board_data.json`, mode **Replace**, type `REPLACE`, then Import. Replace resets the live tables to that file. Merge updates and adds by id and leaves other rows.

`debug` is off. A change to Python or a template is picked up only after the process is restarted. `import_rules.yaml` and CSS are read from disk on each check or page load.

Stop the server with Ctrl+C in that terminal. If the port is still taken, the previous Python process is still listening; stop that process and start again.

### Environment

| Variable | Effect |
| --- | --- |
| `PORT` | Listen port. Default `5050`. |
| `ADMIN_PIN` | When set, `/admin` asks for this PIN. Empty means admin is open on the laptop. |
| `ER_DB` | SQLite path. Default `er_board.db` next to `app.py`. Tests set this to a temp file. |
| `ER_RULES` | Rules file. Default `import_rules.yaml`. Tests point this at a copy so they do not rewrite the repo file. |

Example:

```bash
ADMIN_PIN=1234 PORT=5050 .venv/bin/python app.py
```

### Tests

```bash
.venv/bin/python -m unittest discover tests -q
```

`tests/test_fixture.py` imports `er_status_board_data.json` into a throwaway database and checks flags, the room record, verify, and the yaml rules. It sets `ER_DB` before importing the app. Rule tests that flip `enabled` copy the yaml to a temp path and set `ER_RULES`.

## Design

### Shape

One process, one database, two audiences.

```
browser on this laptop
    │
    ├──  /                  team board (read only, refreshes)
    └──  /admin/*           import, verify, remove, photos, audit
                │
                ▼
         app.py  annotate()
                │
        ┌───────┴────────┐
        ▼                ▼
   er_board.db     import_rules.yaml
   (imported rows)  (which checks hold a chart)
```

`app.py` is the routes and the display-time checks. `importer.py` turns JSON into rows and does not “fix” them. `db.py` opens SQLite, applies the schema, and appends audit rows inside the caller’s transaction. `rules.py` reads the yaml and explains a flag on the verify page.

Flags are not stored. `annotate()` builds them from the current rows and the current yaml every time a page is drawn. A chart with no remaining flag is complete. The team board (`board_data`) keeps only those charts.

### Data

`schema.sql` is the whole model. `patients.source_id` is the id from the file (such as `P-1062`) and is **not** unique, because the snapshot repeats an id. `patients.id` is the local integer key. `patients.age` has no declared type, so a JSON number stays a number and a bad age stays the text that was imported.

`rooms.occupied_by` is the patient token the room list claims. Import writes whatever the file says. A verify save is what changes it.

`vitals` is one row per reading. The board uses the latest row (`recorded_at`, then `id`). A missing time stays null.

`extra_json` keeps keys the importer did not map. `imports` keeps the raw JSON and its SHA-256. `board_backups` keeps a full dump taken in the same transaction as a Replace wipe. `audit_log` is append-only. `photos` stores an uploaded image; a person transcribes it into a chart. The image is not interpreted.

Schema version stays 2. Rule settings are not a table. They live in `import_rules.yaml`.

### Import

`import_snapshot` accepts a few aliases for each field (`triage` or `triage_level`, `temp_f` or `temp`, and so on). Unrecognized keys go to `extra_json`. Numbers that are not numbers are stored as text and flagged later.

The route runs the import in one transaction. A bad file leaves the previous commit in place. Replace deletes the live facility, doctor, room, patient, and vital rows after writing `board_backups`. That includes manual and photo charts. Merge upserts by source id and does not delete.

Import does not rewrite `occupied_by` to match the patients, and it does not rewrite a value to satisfy a rule. After a successful import, `annotate()` decides which charts are complete.

### Rules

Built-in checks live in `annotate()`, `vitals_flags()`, and `room_assignment_flags()`. Each one appends a flag string. `custom_flags()` then runs any yaml block that has a `check`.

`rules.modes()` maps `enabled: true` to Apply and `enabled: false` to Ignore. A flag is kept on the chart when its rule id is applied. An ignored rule still counts on the import page as “Charts matched”, because the flag is computed and then filtered out of the hold list.

Saving the import form rewrites only the `enabled:` line under each rule id, so comments and custom blocks stay. Adding a rule means adding a block. A new id needs `flag`, `why`, and `check`. `check` is one field comparison, or `all:` for every comparison in the list. Fields include the chart columns plus `rank` (the 1–5 triage rank already computed for display). Operators are `contains`, `equals`, `missing`, `present`, `gt`, `lt`, and `in`.

The two triage-and-status checks are custom blocks and ship with `enabled: false`. Turning one to Apply is what makes it hold a chart.

Policy numbers (vital windows, age 0–130, pediatric age 18) are constants in `app.py`. They are not read from the JSON.

### Verify and the room record

The incomplete form posts the chart fields, an optional new patient id, and a new vitals reading. `apply_room_master` sets `occupied_by` on the chosen room to the patient’s token (`source_id`, or `manual-<id>` when there is no source id) and clears that token from the previous room.

Patient id is accepted only when the chart was incomplete before the save. A blank id is rejected when one already exists. A duplicate id is stored and the duplicate-id rule holds the chart.

The verify page lists each remaining flag through `rule_for_flag`: the rule name, the flag, and the `why` text.

Remove posts a required reason and sets `active = 0`. The Removed tab reads the latest `archive` audit detail for that patient. Restore sets `active = 1`.

### Team board

`/` renders `board.html`. Every 10 seconds the page fetches `/board/fragment` and replaces the body. If that fetch fails for about 45 seconds, a red banner says the server stopped answering.

Charts are grouped by triage rank (1 most urgent through 5, then invalid, then discharged). A status containing `critical` is shown in red. SpO2 under 92 is shown in red. Neither of those colours is a hold by itself. The patient id is printed under the bed.

### Admin boundary

`before_request` allows `/admin` only from `127.0.0.1` or `::1`. When `ADMIN_PIN` is set, a session flag `authed` is required. Every admin POST must echo the session CSRF token. `actor()` is always `local-admin`, and that string is what `audit()` writes.

Photo upload accepts jpg, png, gif, and webp, stores the file under `uploads/`, and opens a form for a person to type the chart. Saving to an existing patient writes only the fields that were filled in.

## File map

| Path | Role |
| --- | --- |
| `app.py` | Routes, `annotate()`, room update, verify, import page |
| `importer.py` | JSON → SQLite, room-assignment flags |
| `rules.py` | Load, save, match, and explain `import_rules.yaml` |
| `db.py` | Connection, schema version, audit helper |
| `schema.sql` | Tables |
| `import_rules.yaml` | Which rules hold a chart, and any added rule |
| `templates/` | Board, admin, verify, import, photos, audit |
| `static/board.css`, `static/admin.css` | Board and admin styles, including the tab bar |
| `tests/test_fixture.py` | Fixture import and the decisions above |
| `er_status_board_data.json` | Sample snapshot |
| `decisions.md` | Choices made in review |
| `rule-decision-log.md` | Rule catalog and the open “waiting for bed” item |
| `cursor-feedback.md` | The review that started this pass |

## Changing a rule

To turn a rule off, set `enabled: false` in `import_rules.yaml`, or set that row to Ignore on the import page and save. Charts already loaded are rechecked on the next page.

To add a rule, copy a block, give it a new id, and set `flag`, `why`, and `check`. A built-in id (`duplicate_id`, `bed_shared`, `no_bed`, `age`, `pediatric_room`, `triage_value`, `waiting_for_bed`, `arrival_time`, `vitals_time`, `vitals_values`, `room_not_in_master`, `not_assigned_in_room`) uses the Python check. Changing what a built-in check means is a change in `app.py` or `importer.py`, then a process restart.
