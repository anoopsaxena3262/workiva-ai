# Decisions — 2026-10-07

Record of choices made while reviewing and changing the local ER Patient Status Board. Dates are the day of the conversation. The living rule catalog is `rule-decision-log.md`. This file is the session record of why each choice was made.

The starting point was a review of the repo against `README.md` and `er_status_board_data.json`, written as `cursor-feedback.md`. The review scored the build, named inferred checks, and listed gaps. The choices below are what we then decided to build, keep, or leave off.

## 1. The team board shows complete charts only

The wall display is for the team. A chart is **complete** when no enabled rule holds it, and **incomplete** when one does. Complete charts appear on the public board. Incomplete charts stay on the admin page so a person can verify them.

Review status is computed when a page is drawn. Saving a chart that no longer matches a hold moves it onto the board. Nothing in the database stores a complete/incomplete column.

## 2. The room list is a check, and verify updates it

The JSON stores the patient on the room as `occupied_by`. Two conditions hold a chart:

- the bed is not a room in the room list
- the room’s `occupied_by` is not this patient’s id

Import stores `occupied_by` as it appears in the file. A verify save writes the patient into the selected room and clears the previous room when that room listed them. A discharged chart does not stay assigned as the occupant.

## 3. Admin has three tabs

- **Complete** — charts with no remaining hold. These are the ones on the team board.
- **Incomplete** — charts a person still needs to verify.
- **Removed** — charts taken off the board, with the reason, who entered it, and when.

Remove requires a reason. The chart stays in the database (`patients.active = 0`). The reason is the `archive` detail on `audit_log`. Restore puts the chart back on Complete or Incomplete from its current flags. Replace import still wipes the live tables after copying them into `board_backups`.

## 4. The admin tabs have to be readable

The first tab bar used the dark header style, so the labels were dark on dark. The tab bar is a light strip with dark labels. The selected tab is white with a blue top edge. The rest of the admin look was left as it was.

## 5. There is no operator-initials box

The initials field had no job. Every audit row is stamped `local-admin`. An optional `ADMIN_PIN` still gates `/admin` when it is set. Login is a PIN, not a named account.

## 6. Patient ID is on screen

The source id (for example `P-1062`) is a column on the admin lists and is printed under the bed on the team board. The verify and edit pages show it too.

## 7. Patient ID can be corrected only while verifying

Many imported ids are wrong, and cleaning them is part of verification. The field is an input only on an incomplete chart’s verify form. Saving it updates `patients.source_id` and the room’s `occupied_by`: the old token is released and the new token is claimed.

A complete chart’s edit form shows the id as text. A posted id on that form is ignored. An empty id is rejected when the chart already has one. A duplicate id is saved, and the duplicate-id rule keeps the chart incomplete.

## 8. Triage level and status text are not a rule

`cursor-feedback.md` recommended holding two combinations:

- status text contains `critical` while triage is 4 or 5
- status text says not yet triaged while a triage level is set

Nothing in the instructions says those fields must agree. Treating the words as a broken import was inference. Xander Mbeki (`P-1062`, triage 5, status `critical - MD at bedside`) was the sample: that pair was his only hold, and the JSON itself is consistent.

An override button on verify was considered, so an expert could accept the pair and clear the flag. That was set aside. An override would leave the invented rule in the path and ask a person to dismiss it.

Both fields stay stored and shown as imported. The team board still prints a status that contains `critical` in red. That is a display cue.

The same day, both checks were added to `import_rules.yaml` and left **disabled** (`enabled: false`):

- `status_critical_low_triage`
- `status_not_yet_triaged`

They appear on the import page as Ignore. They do not hold a chart unless someone sets them to Apply. The charts that matched while the checks were active are listed in `rule-decision-log.md`. With the checks off, Xander Mbeki is complete.

## 9. An incomplete verify page explains each rule

A person verifying a chart sees, for each hold, the rule name, the flag text, and what to change. A complete chart’s edit page does not show that list.

## 10. Each rule can be applied or ignored, from a YAML file

A person cannot open every chart when a file is large. Each rule has a setting:

- **Apply** (`enabled: true`) holds a matching chart.
- **Ignore** (`enabled: false`) does not. The imported value stays as it was.

The first version stored that setting in SQLite. It was moved to `import_rules.yaml` so `rules.py` reads a file on every check, and so a new rule can be added by editing the file. The JSON import page writes the same file. Import never rewrites a value to make a rule pass.

A built-in id uses the check already in the app. A new id needs `flag`, `why`, and `check`. A check can require every condition in an `all` list. `{field}` in a flag is filled from the chart.

## 11. “Waiting for bed” while a bed is set stays a hold

Status text that says waiting for bed, on a chart that already has a bed, still holds the chart. That compares status with the bed field. It was not in an instruction. It stays until a separate decision removes it or keeps it. It is `waiting_for_bed` in the yaml, currently enabled.

## What stayed from the original build

These were already in the app and were kept:

- The laptop is the only client. The server binds to `127.0.0.1`. `/admin` refuses any other address.
- The JSON is archived as imported, with a SHA-256. Unknown keys stay in `extra_json`.
- Flags are computed at display time. Values are not rewritten to clear a flag.
- Import is one transaction. Replace copies the live tables into `board_backups` before it wipes them. Merge does not delete.
- Photos are a person typing from an image. The app does not read the image.
- SpO2 under 92 is red on the team board and does not hold the chart. Vitals later than the snapshot time are a count on the admin page, not a per-chart hold.
- Plausibility windows (heart rate, SpO2, temperature in °F, blood pressure, age 0–130, adult at 18 in a pediatric room) are policy in the app, not facts from the file.
