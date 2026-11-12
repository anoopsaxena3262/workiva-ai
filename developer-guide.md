# Developer guide

Local setup, how to run, and how the ER Patient Status Board is built. Product choices from the 2026-10-07 review are in `decisions.md`. The rule catalog is `rule-decision-log.md`. Day-to-day behavior is also summarized in `README.md`.

## What it is

A Flask app and a SQLite file on one laptop. The public page is a wall board of emergency-department charts. The admin page imports a JSON snapshot, lists charts that need a person, and lets that person correct them. There is no network service, no login directory, and no model call.

The sample snapshot ships in the repo at `er_status_board_data.json` (the project root, next to `app.py`). On that file the import is 1 facility, 10 doctors, 28 rooms, 60 patients, and 59 vitals. `system_status` is an offline fallback, so the board shows a red “NOT LIVE DATA” bar.

## Local setup

A new checkout needs Python 3.12, a virtualenv, and one `pip install`. The database, the photo folder, and the session key are created when the app starts. There is no `.env` file to copy. Optional settings are shell variables on the command that starts the app.

### 1. Get the code

```bash
git clone https://github.com/anoopsaxena3262/workiva-ai.git
cd workiva-ai
```

### 2. Python environment

`requirements.txt` is Flask 3 and PyYAML. The install needs a network once. After that the app runs offline.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

Leave `.venv/` out of git. It is already in `.gitignore`.

### 3. Database, uploads, and session key

Do not create these by hand. Starting the app creates them in the repo root, and `.gitignore` already excludes them.

| Path | Created when | What it holds |
| --- | --- | --- |
| `er_board.db` | First start, in `db.init_db()` | SQLite database. `schema.sql` is applied and `user_version` is set to 2. The file is empty of charts until the JSON import below. |
| `uploads/` | `app.py` loads | Photo files from admin photo entry. Accepted types are jpg, png, gif, and webp, up to 25 MB. |
| `.secret_key` | `app.py` loads | Random key that signs the admin session. File mode `600`. |

Two files used at setup are already in the repo root. They are committed, and they are not created on startup.

| Path | Where to look | What it is |
| --- | --- | --- |
| `er_status_board_data.json` | Project root, next to `app.py` | Sample ER snapshot. This is the file to upload on JSON import, and the file the tests load. |
| `import_rules.yaml` | Project root, next to `app.py` | Rule catalog. The import page reads and writes this file. |

To put the database somewhere else, set `ER_DB` to a path before starting. The default is `er_board.db` next to `app.py`.

If startup stops with a schema-version message, the existing `er_board.db` is from an older build. Move that file aside, start again, and import the JSON. The old file is left as it was.

### 4. Environment variables

The process reads the shell. An empty value is the normal local setup: admin is open on this laptop, the database is `er_board.db`, and the rules file is `import_rules.yaml`.

| Variable | Default | Effect |
| --- | --- | --- |
| `PORT` | `5050` | Listen port. macOS AirPlay often holds 5000, so 5050 is the default. |
| `ADMIN_PIN` | empty | When set, `/admin` asks for this PIN. Empty means admin is open on the laptop. |
| `ER_DB` | `er_board.db` | SQLite file. Tests point this at a temp file so they leave the local database alone. |
| `ER_RULES` | `import_rules.yaml` | Rules file. Tests that change `enabled` copy the yaml and point this at the copy. |

Example with a PIN:

```bash
ADMIN_PIN=1234 PORT=5050 .venv/bin/python app.py
```

### 5. Start and load the snapshot

```bash
.venv/bin/python app.py
```

The process binds to `127.0.0.1` only.

| Page | URL |
| --- | --- |
| Team board | http://127.0.0.1:5050/ |
| Admin | http://127.0.0.1:5050/admin |
| JSON import and rules | http://127.0.0.1:5050/admin/import |

On **JSON import**, upload the sample from the project root: `er_status_board_data.json`. Mode **Replace**, type `REPLACE`, then Import. Replace resets the live tables to that file. Merge updates and adds by id and leaves other rows. Until that import, the board has no charts.

`debug` is off. A change to Python or a template is picked up after the process is restarted. `import_rules.yaml` and CSS are read from disk on each check or page load.

Stop the server with Ctrl+C in that terminal. If the port is still taken, the previous Python process is still listening; stop that process and start again.

## Tests

Tests do not need the server running, and they do not use `er_board.db` or `uploads/`. `tests/test_fixture.py` sets `ER_DB` to a temp file before it imports the app, then loads `er_status_board_data.json` from the project root into that file.

From the repo root, with the virtualenv from step 2:

```bash
.venv/bin/python -m unittest discover tests -v
```

The suite is 18 tests and takes about half a minute. A quiet run is `discover tests -q`. The tests check flags, the room record, verify, removal, and the yaml rules.

A test that turns a rule on or off copies `import_rules.yaml` to a temp path and sets `ER_RULES`. The copy in the repo stays as it was committed.

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
| `er_status_board_data.json` | Sample snapshot in the project root. Upload this file on JSON import. |
| `decisions.md` | Choices made in review |
| `rule-decision-log.md` | Rule catalog and the open “waiting for bed” item |
| `cursor-feedback.md` | The review that started this pass |

## Changing a rule

To turn a rule off, set `enabled: false` in `import_rules.yaml`, or set that row to Ignore on the import page and save. Charts already loaded are rechecked on the next page.

To add a rule, copy a block, give it a new id, and set `flag`, `why`, and `check`. A built-in id (`duplicate_id`, `bed_shared`, `no_bed`, `age`, `pediatric_room`, `triage_value`, `waiting_for_bed`, `arrival_time`, `vitals_time`, `vitals_values`, `room_not_in_master`, `not_assigned_in_room`) uses the Python check. Changing what a built-in check means is a change in `app.py` or `importer.py`, then a process restart.
