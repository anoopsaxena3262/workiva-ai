# Cursor feedback — ER Patient Status Board

Review of `workiva-ai` as a local, offline ER wall board (Flask + SQLite). The spec under review is `README.md`. The fixture is `er_status_board_data.json`. Behavior below was checked by loading that file through `import_snapshot` and `board_data()` on 2026-10-07.

## Verdict

The app is a coherent local tool, and it does the job it describes: bind to loopback, keep the raw JSON, import in one transaction, and paint a read-only wall board that flags the dirty rows the README lists. On the supplied snapshot that path is real, not theatrical. Import yields 1 facility, 10 doctors, 28 rooms, 60 patients, 59 vitals, and duplicate id `P-1055`. The board groups them 4 / 15 / 15 / 16 / 8 / 1 / 1 by triage rank, marks the snapshot `NOT LIVE DATA`, and raises 49 flagged cards.

The same run shows the limits of a spec written next to the code. The README’s examples (`triage 0`, `temp_f`, `N/A`, shared beds, duplicate ids, discharged) are exactly the defects this file was built to contain, and those fire. Other defects in the same file do not: age `-9`, status that contradicts triage or bed, vitals timed before arrival, and room `occupied_by` that does not match the patients in the room. A few inferred rules are simply wrong (`non-urgent` sorts as urgent; doctor `role` is overwritten by `specialty`).

Use this as a wall display for the known snapshot. Do not treat the flag bar, the doctor model, or the “nothing is invented / nothing is hard-deleted” sentences as complete.

## Scores

Judgment scores, 0–10. Higher is better except hallucination, where higher means more invented or self-contradictory behavior.

| Lens | Score | Reading |
| --- | ---: | --- |
| Requirements as a spec | 5 | `README.md` is an accurate brochure for this build. It is not an independent requirements document, and it does not list the contradictions that are actually in the JSON. |
| Implementation vs that README | 8 | Loopback, refresh, replace-confirm, verbatim archive, CSRF, audit, and the named flags all exist and work on the fixture. |
| Design | 7 | Small, offline, display-time validation, append-only audit. The entity diagram over-claims the doctor link, and `extra_json` never reaches the UI. |
| Logic on the supplied snapshot | 8 | Every flag the README names is produced, on the right patients, without rewriting the source values on the card. |
| Logic of the inferred rules | 4 | Triage words, doctor role, age, discharged-without-a-bed, and “merge” each do something other than what a reader of the code comments would expect. |
| Hallucination | 3 | Localized. The core ingest of this file is grounded. The false parts are specific and listed below. |
| Inference load | 6 | About a third of the behavior is assumed: alias lists, ESI names, vital ranges, °F for every temp, a doctor–patient relationship the file does not contain, a human photo workflow. |
| Implementation craft | 7 | Readable, transactional, appropriately small. No tests, no `.gitignore`, two validation policies for age, one UI sentence that the save path does not honor. |

**Hallucination score: 3/10.** The board is not a made-up clinical system. It reads this JSON. The hallucinations are local: a field mapped to the wrong meaning, a sort key that misfires, timestamps and ages quietly rewritten, and a relationship drawn in the README that the snapshot never populates.

**Inference score: 6/10.** Alias maps, ranges, and ESI labels are reasonable engineering guesses. They are guesses. The file only forces a narrower contract: `patient_id`, `bed`, `triage_level` as a number, vitals as one object with `bp_systolic` / `bp_diastolic` / `temp_f` / `last_updated`, rooms with `room_id` + `occupied_by`, doctors with both `role` and `specialty`, and no doctor on any patient.

## Requirements

What the README promises, and what the run shows.

| Requirement | Result | Evidence |
| --- | --- | --- |
| Loopback only, no CDN | Met | `app.run(host="127.0.0.1")`. Admin aborts when `remote_addr` is not `127.0.0.1` or `::1`. |
| Wall auto-refresh ~10s, loud failure if the server stops | Met | `setInterval(refresh, 10000)`. `#stale` appears after 45s without a successful fragment. |
| Optional `ADMIN_PIN`, operator initials | Met | Empty PIN leaves `/admin` open on the laptop, as documented. Operator is a session string, not an account. |
| Raw JSON + SHA-256 archived | Mostly met | `imports.raw_json` stores the decoded text. `utf-8-sig` plus `errors="replace"` means a BOM or bad byte is not byte-exact. |
| Unknown keys kept | Stored, not shown | `extra_json` is written. No template renders it. An operator cannot see preserved keys. |
| Do not correct data; flag it at display time | Partial | Named flags are display-time and the card still shows `998.6`, `N/A`, `0`, `70/118`. Age `54.7` would be stored as `54`. A missing vital time is stored as `now()`. |
| Flag duplicate ids, shared beds, no bed, bad triage, bad vitals; separate discharged | Met on this file | Counts below. |
| Import is one transaction | Met | Route uses `with conn`. A raised `ImportError_` leaves the previous commit in place. |
| Patients are never hard-deleted | Broken by Replace | “Remove” sets `active=0`. Replace runs `DELETE FROM patients` (and doctors, rooms, vitals, facilities), including `origin='manual'`. Verified: a manual row count went from 1 to 0 across Replace. |
| Board read-only; admin CSRF; audit on create / edit / archive / import / photo | Met | Those actions call `db.audit` inside the same transaction. Operator changes and login are not audited; the README does not claim they are. |
| `OFFLINE…` shows a red not-live bar; temps are °F | Met for this file | `system_status` classifies as `bad`. Temp is read from `temp_f` first and the card header says `TEMP °F`. |

The requirements gap that matters: the spec was fitted to the fixture. It names the dirt a reviewer would notice in five minutes, and the code catches that dirt. It is silent on the rest of the dirt in the same 60 rows (next section). A later editor who trusts the README will think the flag bar is a complete integrity check.

## Design

The shape is the right one for a single laptop.

- Display-time flags keep the database faithful to the file. That is the correct place to be suspicious of vitals.
- `source_id` is intentionally not unique. Both `P-1055` rows survive as separate patients. That matches a snapshot that repeats an id.
- Latest vital is chosen with `ORDER BY recorded_at DESC, id DESC`. Tied timestamps stay deterministic.
- Photos are a human transcription queue, not an OCR or model call. That is the honest design. Nothing on the image is invented into a chart.
- Audit rows commit or roll back with the change. The log has no update/delete API.

Design claims that the schema diagram makes and the data does not:

- `patients >───1 doctors` is drawn as a fact. After import, `doctor_id` is null on all 60 patients. The wall template never prints a doctor. `static/board.css` still has a `.doc` rule, so the link was assumed and then left unused on the board. Doctors are editable only after someone types them in admin.
- `rooms.occupied_by` is stored and then ignored when the chip color is computed. Chips count patients by `room_id`. That count is the better signal. The source claim is never diffed against it, so the room table and the board can disagree with no specific flag.
- `extra_json` is an archive with no reader in the UI. “Kept” and “visible” are different requirements; only the first is implemented.
- Replace and “never hard-delete” describe two different retention models. The code comment in `importer.py` chooses the snapshot as the new truth and points at `imports.raw_json` for recovery. The README still says patients are never hard-deleted. One of those sentences has to go.
- There is no migration story. `schema.sql` is `CREATE TABLE IF NOT EXISTS` at startup. A changed column will not alter an existing `er_board.db`.

## Hallucination and inference

Grounded in this file (safe to trust):

- Facility string `"Whitaker Memorial ER"` plus top-level `system_status` and `snapshot_generated_at`.
- Patient vitals as a single object, blood pressure split into `bp_systolic` / `bp_diastolic`, temperature in `temp_f`.
- Rooms keyed by `room_id` with `type`, `status`, `occupied_by`. Bed strings match those ids, so `room_id` linkage is real.
- Duplicate `P-1055`, null bed, null vitals, triage `0`, SpO2 `"N/A"`, SpO2 `104`, temp `998.6`, BP `70/118`, status `discharged`.

Inferred, and should be labeled as policy rather than as facts from the file:

- ESI names on group headers (`RESUSCITATION`, `EMERGENT`, …). The file only has integers. The card also prints the raw number, which keeps the inference visible.
- Plausibility windows: HR 20–250, SpO2 50–100, temp 90–110 °F, systolic 50–260, diastolic must be lower than systolic. SpO2 under 92 is painted red and is not a flag. Clinical alarm and data-quality flag are different channels; the UI does not say so.
- A long alias table (`heart_rate`, `esi`, `mrn`, `gender`, …). This file uses one shape. The aliases are defensive. They also create the role bug below.
- Any temperature key (`temp`, `temperature`, `temp_f`) is labeled °F. For this file that is correct because the key is `temp_f`. A later Celsius `temp` of `37` would be called implausible °F, which fails safe, and would still be captioned °F.
- “Live” means the status text starts with normal / ok / operational / online / live. `OFFLINE - fallback data snapshot` correctly falls through to the red bar.

False or in conflict with the code’s own comments:

1. **Doctor role is the specialty.** `ALIASES["role"]` checks `specialty` before `role`. All 10 doctors store `Emergency Medicine` (or similar) in `doctors.role`. The real role (`Attending`, `Resident (PGY-2)`, …) sits in `extra_json`. The admin dropdown shows that swapped value (`Dr. Alan Whitfield — Emergency Medicine`).
2. **“Nothing is invented” is false for time.** `parse_vitals` sets `recorded_at` to `now()` when no timestamp alias is present. This file’s vitals all have `last_updated`, so the fixture hides it.
3. **Non-integer age is rewritten.** `54.7` is stored as `54` and the original is dropped from `extra_json` because the age key is marked used. `"about 50"` becomes SQL `NULL` and the text survives only in `extra_json`, which the board does not read, so the card says `age ?`.
4. **Photo save is a full overwrite.** `templates/photo_transcribe.html` says only changed fields are written. `update_patient` assigns every patient column from the form. Vitals are the field that is actually optional. If the prefill request fails, saving an existing patient writes blanks over the chart.
5. **The ER diagram’s doctor cardinality.** Drawn as required structure. Populated by nobody in the snapshot.

## Logic validation

Fixture after Replace. Flag totals from `board_data()`:

| Flag | Cards | On the right rows? |
| --- | ---: | --- |
| `BED SHARED` | 45 | Yes. 13 rooms have more than one active, non-discharged patient. A12 and HALLWAY1 are ×8. |
| `DUPLICATE ID P-1055` | 2 | Yes. Dana Jimenez (A4) and Rosa Delgado (HALLWAY1) both kept. |
| `NO BED ASSIGNED` | 2 | Diane Kowalski (no bed, no vitals) and Ravi Ferraro. |
| `NO VITALS` | 1 | Diane Kowalski only. `vitals: null` inserts no row. |
| `TRIAGE '0' INVALID` | 1 | Derek Ortega, group “TRIAGE NOT SET / INVALID”. |
| `TEMP 998.6 IMPLAUSIBLE` | 1 | Sana Okoro. Value still shown. |
| `SpO2 104 IMPLAUSIBLE` | 1 | Aaron Grant. |
| `SpO2 'N/A'` | 1 | Grace Muthoni. SQLite stores it as `text` in a `REAL` column, and the card shows `N/A` with no percent sign. |
| `BP 70/118 CHECK` | 1 | Petra Fischer. Diastolic ≥ systolic. |
| Discharged group | 1 | Ravi Ferraro, rank 10, dimmed. |
| Patients with a doctor | 0 | The file has no such field. |
| `NOT LIVE DATA` | board | Status does not match the live-word list. |

49 of 60 cards are flagged. 45 of those flags are “this bed has company.” HALLWAY1 / HALLWAY2 are typed `Hallway Overflow` and are scored with the same rule as a private exam room, so overflow and true double-booking look identical. The rare rows (age, triage 0, temp, SpO2, inverted BP) sit on cards that often already say `BED SHARED`. The yellow summary bar will train people to stop reading it.

Defects in the same file that produce no flag:

| Pattern | Count | What the board does |
| --- | ---: | --- |
| Age &lt; 0 | 1 | Victor Vasquez is shown as `-9y`. His only flag is `BED SHARED x3`. Manual entry rejects age outside 0–130; import accepts it. |
| Status contains `critical` while triage rank ≥ 4 | 4 | Nate Petrov and Hassan Eriksson (triage 4), Dana Jimenez and Xander Mbeki (triage 5) sort with the low-acuity groups. The red status pill is the only cue. |
| Status says not yet triaged, triage level is set | 4 | Shown as a normal triaged patient. |
| Status says waiting for bed, bed is set | 12 | Bed and status both print. No conflict flag. |
| `rooms.occupied_by` is not the single occupant | 13 of 28 | Chip turns yellow from the patient count. The claimed id is not mentioned. A4 claims `P-1055`, which is also a duplicated id, so the claim is ambiguous. |
| Vitals timestamp before `arrival_time` | 15 | Arrival is stored and not drawn on the card, so the inversion is invisible. |
| Vitals timestamp after `snapshot_generated_at` (`2026-07-10T08:45:00Z`) | 38 | Both clocks can be seen if you look. Nothing says the snapshot disagrees with itself. |
| Adult in a pediatric room | 3 | Kwame Lindgren 62 and Aaron Chen 24 in PEDS1, Lena Nilsson 85 in PEDS3. Room type is only a chip tooltip. |
| Discharged with a null bed | 1 | Ravi Ferraro is flagged `NO BED ASSIGNED` in the discharged group. Discharge is the reason the bed is empty. |

Triage word ranking is wrong for the labels the group headers use. `triage_rank` returns on the first substring hit, and `"urgent"` is tested before `"less"` and `"non-urgent"`:

| Input | Rank returned | Rank the words imply |
| --- | ---: | ---: |
| `urgent` | 3 | 3 |
| `less urgent`, `less-urgent` | 3 | 4 |
| `non-urgent`, `nonurgent` | 3 | 5 |
| `ESI-4`, `5`, `minor` | 4 or 5 | correct |
| `helpless` | 4 | coincidence: it contains `less` |

This fixture uses numeric triage, so the wall order for these 60 people is right. Any snapshot that says `non-urgent` will file that patient under urgent.

Other logic notes, from reading the save path:

- Merge updates a patient only when `source_id` matches exactly one existing row. A repeated id inserts another row. A second merge of an already-duplicated id grows the board.
- Merge inserts another vitals row every time. The board shows the latest, so the screen stays stable while history duplicates.
- Replace deletes manual and photo patients. Recovery is “replay an old `imports.raw_json`,” which does not contain edits made after that import.
- `room_for_bed` links a typed bed to a room id or name. A free-text bed still displays and simply does not color a chip.
- Public HTML is escaped by Jinja, and the 10s refresh assigns that HTML to `innerHTML`. That is safe while autoescape stays on.
- The Remove button puts the patient name inside `confirm('…')`. A name containing an apostrophe breaks the handler after HTML decoding.

## Implementation

What is in good shape:

- One database transaction per import and per patient write, with the audit insert inside it.
- Foreign keys on. Replace deletes children before parents.
- CSRF on admin POSTs, compared with `secrets.compare_digest`.
- Loopback guard in `before_request`, independent of the bind address.
- Secret file created at `0600` if missing.
- Upload names are content hashes; `send_from_directory` serves them.
- `N/A` survives SQLite’s numeric affinity and is flagged as text. That part of `schema.sql` is accurate.
- Debug server is off. Content length capped at 25 MB.

What is thin:

- No tests. The regressions that matter (role alias, `non-urgent`, age truncation, Replace deleting manual rows, BP inversion, duplicate ids) are all small and would have been a one-file test against the fixture.
- No `.gitignore`. `.secret_key`, `.venv/`, `er_board.db`, and `uploads/` are eligible to be committed. The secret signs the admin session.
- Admin patient list does not show the flags the wall computes. The person fixing data and the board the room is looking at do not share a checklist.
- `db.init_db()` runs at import. Importing `app` creates a database file.
- PIN comparison is unbounded. On loopback that is a small issue; a 4-digit PIN is still a 4-digit PIN.
- HEIC is accepted and often will not render in the transcription view.

## Improvements

In the order they change trust in the board.

1. **Flag the defects this file already has.** At least: age outside 0–130, status `critical` with triage ≥ 4, “not yet triaged” while a level is set, “waiting for bed” while a bed is set, vitals time before arrival, and `occupied_by` not equal to the set of source ids in that room. Show age `-9` as a flag, not only as `-9y`.
2. **Stop treating overflow like a double-booked exam room.** Use `room_type` so Hallway Overflow can hold many people, and keep `BED SHARED` for rooms whose source status assumes one occupant. Put a single “13 rooms disagree with occupied_by” line on the board so 45 cards are not each shouting the same fact.
3. **Fix `triage_rank` word order.** Match `non-urgent` and `less urgent` as whole phrases before the substring `urgent`. Add those three strings to a test.
4. **Stop preferring `specialty` over `role`.** Store both. The dropdown should read `Attending — Emergency Medicine`.
5. **Make the retention sentence true.** Either Replace archives current patients (including manual and photo) into a snapshot table before delete, or the README should say Replace discards the live board and only the raw JSON import is kept.
6. **Stop rewriting values.** Keep non-integer and non-numeric age verbatim and flag them. Leave `recorded_at` null when the file has no time, and let the card say the time is missing.
7. **Align the photo form with the UPDATE.** Prefill server-side when an existing patient is chosen, and update only posted fields. Change the help text only after that is true.
8. **Surface `extra_json` somewhere in admin** (patient edit and the import result), or stop claiming unknown keys are part of the operator workflow.
9. **Show arrival on the card**, in the source string, so the one US-format timestamp and the 15 inverted vital times are visible.
10. **Add a fixture test** that imports `er_status_board_data.json` and asserts the flag set, both `P-1055` rows, doctor role text, facility status class, and that Replace removes a manual patient only if that remains the chosen policy.
11. **Add a `.gitignore`** for `.secret_key`, `*.db`, `uploads/`, `.venv/`.

## What to leave alone

- Display-time flags instead of “cleaning” the JSON on import.
- Non-unique `source_id`, with both bodies kept.
- All-or-nothing import and the `REPLACE` confirmation.
- Human transcription of photos.
- Loopback admin, CSRF, and audit in the same transaction as the write.
- The red bar for this snapshot’s `system_status`. That call is correct.
