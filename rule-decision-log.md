# Rule and decision log

The session record of choices made together is `decisions.md`. Setup, how to run, and how the app is built are in `developer-guide.md`.

This file is the record of rules the board enforces and of choices made while building it. A rule holds a chart off the team board until a person verifies it. A decision is a choice to enforce a rule, or not to.

Dates are the day the choice was made.

## Decision — 2026-10-07 — Triage level and status text are not a rule

**Callout.** The review in `cursor-feedback.md` recommended flagging two combinations:

- status text contains `critical` while triage is 4 or 5
- status text says not yet triaged while a triage level is set

Nothing in the instructions says those two fields must agree. The check was inference: it treats a clinical reading of the words as a broken import. This project does not make that judgment.

**Not chosen.** An override button on the verify form, so an expert could accept the pair and clear the flag. That would leave the invented rule in the path and ask a person to dismiss it.

**What the app does.** Both fields are stored and shown as imported. Neither combination holds a chart unless `import_rules.yaml` turns it on. Both checks are in that file with `enabled: false`:

- `status_critical_low_triage`
- `status_not_yet_triaged`

The team board still prints a status that contains `critical` in red. That is a display cue, not a data hold.

In the current snapshot, the removed flags were on these charts:

| Patient ID | Name | Triage | Status | Other holds |
| --- | --- | --- | --- | --- |
| P-1062 | Xander Mbeki | 5 | critical - MD at bedside | none — this chart is complete |
| P-1053 | Nate Petrov | 4 | critical - MD at bedside | shared bed, vitals time before arrival |
| P-1055 | Dana Jimenez | 5 | critical - MD at bedside | duplicate id, shared bed, vitals time before arrival |
| P-1089 | Hassan Eriksson | 4 | critical - MD at bedside | shared bed, room assignment |
| P-1046 | Diane Kowalski | 3 | waiting room - not yet triaged | no bed, no vitals |
| P-1055 | Rosa Delgado | 3 | waiting room - not yet triaged | duplicate id, room assignment |
| P-1090 | Katya Weiss | 3 | waiting room - not yet triaged | shared bed, vitals time, room assignment |
| P-1095 | Umar Ortega | 3 | waiting room - not yet triaged | vitals time before arrival |

## Rules in force

These hold a chart for verification. Values are never rewritten to make a flag go away.

| Rule | What it catches |
| --- | --- |
| Duplicate patient ID | The same source id on more than one active chart. Both rows are kept. |
| Bed shared | More than one active, non-discharged patient on the same bed. Rooms whose type contains "Overflow" are exempt. |
| No bed | An active, non-discharged chart with an empty bed. |
| Age | Missing, or not a whole number from 0 to 130. The imported value is stored as given. |
| Pediatric room | Age 18 or older in a room whose type contains "pediatric", unless the chart is discharged. 18+ is a policy line, not a fact from the file. |
| Triage value | A triage level is present and is not recognizable as 1–5. An empty triage level is not this flag. |
| Waiting for bed | Status text says "waiting for bed" and a bed is set. See the open item below. |
| Arrival time | A value is present and is not ISO-8601. |
| Vitals time | A vitals timestamp is earlier than arrival. |
| Vitals values | A vital is missing, `N/A`, or outside the policy window: HR 20–250, SpO2 50–100, temp 90–110 °F, systolic 50–260, diastolic below systolic. |
| Room not in master | The bed is not a room in the room list. |
| Not assigned in the room | The room's `occupied_by` is not this patient's id. |

These are notes, not holds:

- Vitals later than the snapshot time are counted on the admin page.
- SpO2 under 92 is shown in red on the team board. It does not hold the chart.

## Decisions made in this build

1. **Complete charts are the team board.** A chart with any rule above is incomplete and stays on admin until a person verifies it and every flag is gone. Reason and accepted cost: see the decision "Wrong or doubtful data stays off the team board" below.
2. **Verify updates the room master.** Saving a chart writes that patient into `occupied_by` for the selected room and releases the previous room when it listed them. Import does not rewrite `occupied_by`.
3. **Admin tabs are Complete, Incomplete, and Removed.** Removing a chart requires a reason. Removed shows that reason, who entered it, and when. Restore puts the chart back on Complete or Incomplete.
4. **No operator initials box.** Audit rows are stamped `local-admin`.
5. **Patient ID is visible** on the chart lists and under the bed on the team board.
6. **Patient ID is editable only while verifying** an incomplete chart. Saving a corrected id updates the chart and the room record that listed the previous id. A complete chart keeps its id. An empty id is rejected when the chart already has one. A duplicate id is saved and the chart stays incomplete under the duplicate-id rule.

## Decision — 2026-10-07 — Each rule can apply or be ignored

A person cannot open every chart when the file is large. On JSON import, each rule in force has a setting:

- **Apply** holds a chart that matches, for a person to verify.
- **Ignore** does not hold the chart. The imported value stays as it was.

The default is Apply (`enabled: true`). The list lives in `import_rules.yaml`, and `rules.py` reads it on every check. The JSON import page writes the same file. A new rule is a new block: a built-in id uses the check already in the app, and a new id needs `flag`, `why`, and `check`. Import does not rewrite a value to make a rule pass. A rule set to Ignore is also omitted from the import warnings that say a chart is held.

## Decision — 2026-10-07 — Wrong or doubtful data stays off the team board

**Callout.** A review noted that the team board shows only complete charts. In the current snapshot that is 9 of 60 patients on the board and 51 held on `/admin`, including some triage 1 and "critical - MD at bedside" charts.

**Decision.** Keep it. A chart that fails any rule in force is not shown on the team board. It is verified and corrected on `/admin` by the appropriate team member first.

**Why.** The board is read across the room by people who act on it. Another patient's data, or a wrong value, shown as if it were current can cause harm. A chart that is missing from the board gets noticed and chased. A wrong chart on the board does not. A "needs verification" tag on a normal-looking card can be missed at a glance, so a tag is not enough protection.

**Not chosen.** Showing every chart with a visible warning tag. Rejected because the warning can go unseen while the wrong value is read and used.

**Accepted cost.** Until charts are verified, patients who are really in the department are not on the board. The board says how many charts are held, and a held count above zero is a work item for the verifying team, not a normal state. How fast charts are verified is a staffing matter, not something the app solves.

**What the app does.**
- Complete charts only on the team board; incomplete charts stay on `/admin` until a person verifies them and every flag is gone.
- The board header and a banner show the number of held charts.
- Values are never rewritten to make a flag go away. A person changes them, and the change is in the audit log.

**Follow-up.** Rules stay open to review. A rule that holds charts for a reason nobody can defend, such as "waiting for bed" (see Open), takes patients off the board for no safety gain, so each hold rule should have a stated reason.

## Open

**Status "waiting for bed" while a bed is set** is still a hold. It compares status text with the bed field, not with triage. It was also not in an instruction. It stays until a separate decision removes it or keeps it.
