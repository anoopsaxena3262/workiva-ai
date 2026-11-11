"""Run: .venv/bin/python -m unittest discover tests -v
Imports the supplied er_status_board_data.json into a throwaway DB and asserts that
data problems are surfaced (flagged) and the source values are never altered."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["ER_DB"] = str(Path(tempfile.mkdtemp()) / "t.db")
sys.path.insert(0, str(ROOT))

import app as A  # noqa: E402
import db  # noqa: E402
from importer import import_snapshot  # noqa: E402

RAW = (ROOT / "er_status_board_data.json").read_text()
SRC = json.loads(RAW)


def load(raw=RAW, mode="replace"):
    db.init_db()
    conn = db.connect()
    with conn:
        counts = import_snapshot(conn, raw, mode, "test")
    conn.close()
    return counts


def flags_for():
    conn = db.connect()
    pts, rooms, fac, notes = A.annotate(conn)
    conn.close()
    return {p["name"]: p for p in pts}, rooms, fac, notes


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.remove(os.environ["ER_DB"]) if os.path.exists(os.environ["ER_DB"]) else None
        cls.counts = load()
        cls.p, cls.rooms, cls.fac, cls.notes = flags_for()

    def has(self, name, text):
        self.assertTrue(any(text in f for f in self.p[name]["flags"]), (name, text, self.p[name]["flags"]))

    def test_counts_and_duplicate_id_kept(self):
        self.assertEqual((self.counts["patients"], self.counts["vitals"]), (60, 59))
        self.assertEqual(self.counts["duplicate_patient_ids"], ["P-1055"])
        self.has("Dana Jimenez", "DUPLICATE ID")
        self.has("Rosa Delgado", "DUPLICATE ID")

    def test_values_not_altered(self):
        for sp in SRC["patients"]:
            row = self.p[sp["name"]]
            self.assertEqual(row["age"], sp["age"])
            self.assertEqual(row["bed"], sp["bed"])
            self.assertEqual(row["arrival_time"], sp["arrival_time"])
            if sp["vitals"]:
                self.assertEqual(row["spo2"], sp["vitals"]["spo2"])  # incl. "N/A" and 104
                self.assertEqual(row["temp"], sp["vitals"]["temp_f"])  # incl. 998.6

    def test_known_defects_flagged(self):
        self.has("Derek Ortega", "TRIAGE '0' INVALID")
        self.has("Diane Kowalski", "NO VITALS")
        self.has("Diane Kowalski", "NO BED")
        self.has("Petra Fischer", "BP 70/118")
        self.has("Victor Vasquez", "AGE '-9' INVALID")
        self.has("Grace Muthoni", "SpO2 'N/A'")
        self.has("Aaron Grant", "SpO2 104")
        self.has("Sana Okoro", "TEMP 998.6")
        self.has("Tomas Reyes", "ARRIVAL")
        self.has("Kwame Lindgren", "PEDIATRIC")

    def test_yaml_enables_disables_and_adds_a_rule(self):
        import os
        import shutil
        import rules as R
        tmp = Path(tempfile.mkdtemp()) / "import_rules.yaml"
        shutil.copy(R.BASE / "import_rules.yaml", tmp)
        old = os.environ.get("ER_RULES")
        os.environ["ER_RULES"] = str(tmp)
        try:
            text = tmp.read_text().replace(
                "  - id: bed_shared\n    enabled: true\n",
                "  - id: bed_shared\n    enabled: false\n", 1)
            tmp.write_text(text + (
                "\n  - id: status_says_critical\n"
                "    enabled: true\n"
                "    name: Status says critical\n"
                "    catches: Status text contains critical.\n"
                "    flag: STATUS TEXT CRITICAL\n"
                "    why: The status text contains the word critical.\n"
                "    check:\n"
                "      field: status\n"
                "      op: contains\n"
                "      value: critical\n"))
            conn = db.connect()
            pts, _, _, notes = A.annotate(conn)
            conn.close()
            self.assertNotIn("bed_shared", notes["applied"])
            self.assertFalse(any(f.startswith("BED SHARED") for p in pts for f in p["flags"]))
            self.assertTrue(any(f == "STATUS TEXT CRITICAL" for p in pts for f in p["flags"]))
            xander = next(p for p in pts if p["name"] == "Xander Mbeki")
            self.assertIn("STATUS TEXT CRITICAL", xander["flags"])
            self.assertEqual(A.rule_for_flag("STATUS TEXT CRITICAL")["rule"], "Status says critical")
            with A.app.test_client() as c:
                html = c.get("/admin/import").get_data(as_text=True)
                self.assertIn("import_rules.yaml", html)
                self.assertIn("Status says critical", html)
                token = html.split('name="csrf" value="', 1)[1].split('"', 1)[0]
                data = {"csrf": token, "action": "save_rules"}
                for rule in R.load_rules():
                    data["rule_" + rule["id"]] = "ignore" if rule["id"] == "waiting_for_bed" else "apply"
                saved = c.post("/admin/import", data=data, follow_redirects=True)
                self.assertIn("Ignored: Waiting for bed", saved.get_data(as_text=True))
            saved_text = tmp.read_text()
            self.assertIn("id: status_says_critical", saved_text)
            self.assertIn("enabled: false", saved_text.split("id: waiting_for_bed", 1)[1][:80])
            conn = db.connect()
            pts, *_ = A.annotate(conn)
            conn.close()
            self.assertFalse(any("WAITING FOR BED" in f for p in pts for f in p["flags"]))
            self.assertTrue(any(f.startswith("BED SHARED") for p in pts for f in p["flags"]))
        finally:
            if old is None:
                os.environ.pop("ER_RULES", None)
            else:
                os.environ["ER_RULES"] = old

    def test_verify_page_explains_each_rule(self):
        conn = db.connect()
        pts, *_ = A.annotate(conn)
        conn.close()
        chen = next(p for p in pts if p["name"] == "Aaron Chen")
        self.assertTrue(chen["flags"])
        with A.app.test_client() as c:
            html = c.get(f"/admin/patients/{chen['id']}/edit").get_data(as_text=True)
        self.assertIn("Verify chart", html)
        for flag in chen["flags"]:
            info = A.rule_for_flag(flag)
            self.assertIn(info["rule"], html)
            self.assertIn(flag, html)
            self.assertTrue(info["why"])
        unknown = A.rule_for_flag("SOMETHING NEW")
        self.assertEqual(unknown["rule"], "Data check")

    def test_triage_and_status_are_not_compared(self):
        # Decision: these checks are in import_rules.yaml with enabled false.
        # They do not hold a chart until that file turns them on.
        flagged = [f for p in self.p.values() for f in p["flags"]]
        self.assertFalse(any("STATUS CRITICAL" in f or "NOT TRIAGED" in f for f in flagged))
        self.assertEqual(self.p["Xander Mbeki"]["review"], "complete")
        self.assertEqual(self.p["Xander Mbeki"]["flags"], [])
        self.assertTrue(any("WAITING FOR BED, BED SET" in f for f in flagged))

    def test_triage_status_rules_hold_only_when_yaml_enables_them(self):
        import os
        import shutil
        import rules as R
        tmp = Path(tempfile.mkdtemp()) / "import_rules.yaml"
        shutil.copy(R.BASE / "import_rules.yaml", tmp)
        text = tmp.read_text()
        for rid in ("status_critical_low_triage", "status_not_yet_triaged"):
            text = text.replace(
                f"  - id: {rid}\n    enabled: false\n",
                f"  - id: {rid}\n    enabled: true\n", 1)
        tmp.write_text(text)
        old = os.environ.get("ER_RULES")
        os.environ["ER_RULES"] = str(tmp)
        try:
            conn = db.connect()
            pts, *_ = A.annotate(conn)
            conn.close()
            by_name = {p["name"]: p for p in pts}
            self.assertIn("STATUS CRITICAL, TRIAGE 5", by_name["Xander Mbeki"]["flags"])
            self.assertEqual(by_name["Xander Mbeki"]["review"], "incomplete")
            self.assertIn("STATUS SAYS NOT TRIAGED, LEVEL SET", by_name["Diane Kowalski"]["flags"])
            info = A.rule_for_flag("STATUS CRITICAL, TRIAGE 5")
            self.assertEqual(info["rule"], "Status critical, triage 4 or 5")
        finally:
            if old is None:
                os.environ.pop("ER_RULES", None)
            else:
                os.environ["ER_RULES"] = old

    def test_discharged_separate_and_no_false_nobed(self):
        r = self.p["Ravi Ferraro"]
        self.assertEqual(r["rank"], 10)
        self.assertFalse(any("NO BED" in f for f in r["flags"]))

    def test_room_master_marks_unlisted_and_verify_updates_room(self):
        vitals = {"hr": 80, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temp_f": 98.6,
                  "last_updated": "2026-07-10T08:00:00Z"}
        raw = json.dumps({"facility": "T", "rooms": [
            {"room_id": "A1", "type": "Standard Exam", "status": "occupied", "occupied_by": "P1"},
            {"room_id": "B1", "type": "Standard Exam", "status": "available", "occupied_by": None},
        ], "patients": [
            {"patient_id": "P1", "name": "Official", "age": 40, "bed": "A1", "triage_level": 3,
             "status": "in treatment", "arrival_time": "2026-07-10T07:00:00Z", "vitals": vitals},
            {"patient_id": "P2", "name": "Extra", "age": 41, "bed": "A1", "triage_level": 3,
             "status": "in treatment", "arrival_time": "2026-07-10T07:10:00Z", "vitals": vitals},
            {"patient_id": "P3", "name": "Nowhere", "age": 42, "bed": "Z9", "triage_level": 3,
             "status": "in treatment", "arrival_time": "2026-07-10T07:20:00Z", "vitals": vitals},
        ]})
        try:
            counts = load(raw)
            self.assertEqual(counts["room_not_in_master"], 1)
            self.assertEqual(counts["not_assigned_in_room"], 1)
            p, *_ = flags_for()
            self.assertTrue(any("NOT IN MASTER" in f for f in p["Nowhere"]["flags"]))
            self.assertEqual(p["Nowhere"]["review"], "incomplete")
            self.assertTrue(any("NOT ASSIGNED IN ROOM A1" in f for f in p["Extra"]["flags"]))
            self.assertFalse(any("NOT ASSIGNED" in f or "NOT IN MASTER" in f for f in p["Official"]["flags"]))
            with A.app.test_request_context():
                conn = db.connect()
                with conn:
                    A.update_patient(conn, p["Extra"]["id"], {
                        "name": "Extra", "age": 41, "sex": None, "bed": "B1",
                        "chief_complaint": None, "triage_level": "3", "status": "in treatment",
                        "doctor_id": None, "notes": None,
                    }, {}, "manual")
                occ = {r["id"]: r["occupied_by"] for r in conn.execute("SELECT id, occupied_by FROM rooms")}
                moved = conn.execute("SELECT room_id, bed FROM patients WHERE id=?", (p["Extra"]["id"],)).fetchone()
                conn.close()
            self.assertEqual(moved["room_id"], "B1")
            self.assertEqual(occ["B1"], "P2")
            self.assertEqual(occ["A1"], "P1")
        finally:
            load()

    def test_room_disagreement_and_overflow(self):
        self.assertGreater(self.notes["rooms_disagree"], 0)
        # overflow rooms are not reported as "BED SHARED" per card, but are in the room disagreement count
        self.assertFalse(any("BED SHARED" in f for f in self.p["Petra Eriksson"]["flags"]))
        self.assertTrue(any("BED SHARED" in f for f in self.p["Petra Fischer"]["flags"]))  # A12 (exam room)

    def test_doctor_role_not_overwritten(self):
        conn = db.connect()
        d = conn.execute("SELECT role, specialty FROM doctors WHERE id='D-01'").fetchone()
        conn.close()
        self.assertEqual((d["role"], d["specialty"]), ("Attending", "Emergency Medicine"))

    def test_system_status_not_live(self):
        self.assertEqual(A.board_data()["sys_class"], "bad")

    def test_removed_tab_keeps_reason(self):
        try:
            with A.app.test_client() as c:
                page = c.get("/admin?tab=complete")
                self.assertEqual(page.status_code, 200)
                html = page.get_data(as_text=True)
                self.assertIn("Incomplete", html)
                token = html.split('name="csrf" value="', 1)[1].split('"', 1)[0]
                conn = db.connect()
                pts, *_ = A.annotate(conn)
                conn.close()
                target = next(p for p in pts if p["review"] == "complete" and p["active"])
                removed = c.post(f"/admin/patients/{target['id']}/active", data={
                    "csrf": token, "active": "0", "tab": "complete",
                    "reason": "duplicate of another chart",
                }, follow_redirects=True)
                body = removed.get_data(as_text=True)
                self.assertIn("duplicate of another chart", body)
                self.assertIn(target["name"], body)
                self.assertNotIn(target["name"], c.get("/admin?tab=complete").get_data(as_text=True))
                self.assertNotIn(target["name"], c.get("/admin?tab=incomplete").get_data(as_text=True))
        finally:
            load()

    def test_team_board_shows_complete_only(self):
        data = A.board_data()
        shown = [p for _, ps in data["groups"] for p in ps]
        self.assertEqual(len(shown), data["total"])
        self.assertTrue(shown)
        self.assertTrue(all(p["review"] == "complete" and not p["flags"] for p in shown))
        self.assertEqual(data["held"] + data["total"], len(self.p))
        self.assertGreater(data["held"], 0)
        shown_names = {p["name"] for p in shown}
        self.assertNotIn("Victor Vasquez", shown_names)
        self.assertNotIn("Diane Kowalski", shown_names)
        self.assertNotIn("Derek Ortega", shown_names)

    def test_triage_words(self):
        for text, rank in (("non-urgent", 5), ("Non Urgent", 5), ("less urgent", 4), ("urgent", 3),
                           ("ESI-4", 4), ("helpless", 9), ("0", 9), (None, 9), ("3", 3)):
            self.assertEqual(A.triage_rank(text), rank, text)

    def test_replace_backs_up_manual_patient(self):
        conn = db.connect()
        with conn:
            conn.execute("INSERT INTO patients (name,origin,active,created_at,updated_at) VALUES ('Manual X','manual',1,'t','t')")
        conn.close()
        load()
        conn = db.connect()
        n = conn.execute("SELECT COUNT(*) FROM patients WHERE name='Manual X'").fetchone()[0]
        dump = json.loads(conn.execute("SELECT dump_json FROM board_backups ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.close()
        self.assertEqual(n, 0)
        self.assertIn("Manual X", [x["name"] for x in dump["patients"]])

    def test_missing_vitals_time_not_invented_and_age_verbatim(self):
        raw = json.dumps({"facility": "T", "patients": [
            {"patient_id": "X1", "name": "A", "age": 54.7, "bed": "Z", "vitals": {"hr": 80}},
            {"patient_id": "X2", "name": "B", "age": "about 50"}]})
        load(raw)
        p, *_ = flags_for()
        self.assertEqual(p["A"]["age"], 54.7)
        self.assertIsNone(p["A"]["vitals_at"])
        self.assertEqual(p["B"]["age"], "about 50")
        self.has_flag = lambda n, t: any(t in f for f in p[n]["flags"])
        self.assertTrue(self.has_flag("A", "VITALS TIME MISSING"))
        self.assertTrue(self.has_flag("A", "AGE '54.7' INVALID"))
        self.assertTrue(self.has_flag("B", "AGE 'about 50' INVALID"))
        load()  # restore

    def test_verify_can_correct_patient_id(self):
        vitals = {"hr": 80, "bp_systolic": 120, "bp_diastolic": 80, "spo2": 98, "temp_f": 98.6,
                  "last_updated": "2026-07-10T08:00:00Z"}
        raw = json.dumps({"facility": "T", "snapshot_generated_at": "2026-07-10T09:00:00Z", "rooms": [
            {"room_id": "B5", "type": "Standard Exam", "status": "occupied", "occupied_by": "P-OLD"},
            {"room_id": "A1", "type": "Standard Exam", "status": "occupied", "occupied_by": "P-OK"},
        ], "patients": [
            {"patient_id": "P-OLD", "name": "Wrong Id", "age": 66, "bed": "B5", "triage_level": 0,
             "status": "in treatment", "chief_complaint": "flu",
             "arrival_time": "2026-07-10T07:00:00Z", "vitals": vitals},
            {"patient_id": "P-OK", "name": "Clean Chart", "age": 40, "bed": "A1", "triage_level": 3,
             "status": "in treatment", "chief_complaint": "cough",
             "arrival_time": "2026-07-10T07:00:00Z", "vitals": vitals},
        ]})
        try:
            load(raw)
            conn = db.connect()
            pts, *_ = A.annotate(conn)
            conn.close()
            by_name = {p["name"]: p for p in pts}
            wrong, clean = by_name["Wrong Id"], by_name["Clean Chart"]
            self.assertEqual(wrong["review"], "incomplete")
            self.assertEqual(clean["review"], "complete")
            with A.app.test_client() as c:
                verify = c.get(f"/admin/patients/{wrong['id']}/edit").get_data(as_text=True)
                self.assertIn('name="source_id"', verify)
                self.assertIn("P-OLD", verify)
                edit = c.get(f"/admin/patients/{clean['id']}/edit").get_data(as_text=True)
                self.assertNotIn('name="source_id"', edit)
                token = verify.split('name="csrf" value="', 1)[1].split('"', 1)[0]
                fields = {"csrf": token, "name": "Wrong Id", "age": "66", "bed": "B5",
                          "triage_level": "0", "status": "in treatment", "chief_complaint": "flu",
                          "source_id": "P-RIGHT"}
                saved = c.post(f"/admin/patients/{wrong['id']}/edit", data=fields, follow_redirects=True)
                self.assertEqual(saved.status_code, 200)
                blocked = c.post(f"/admin/patients/{clean['id']}/edit", data={
                    "csrf": token, "name": "Clean Chart", "age": "40", "bed": "A1",
                    "triage_level": "3", "status": "in treatment", "chief_complaint": "cough",
                    "source_id": "P-HACK",
                }, follow_redirects=True)
                self.assertEqual(blocked.status_code, 200)
            conn = db.connect()
            ids = {r["name"]: r["source_id"] for r in conn.execute("SELECT name, source_id FROM patients")}
            occ = {r["id"]: r["occupied_by"] for r in conn.execute("SELECT id, occupied_by FROM rooms")}
            conn.close()
            self.assertEqual(ids["Wrong Id"], "P-RIGHT")
            self.assertEqual(occ["B5"], "P-RIGHT")
            self.assertEqual(ids["Clean Chart"], "P-OK")
            self.assertEqual(occ["A1"], "P-OK")
        finally:
            load()


if __name__ == "__main__":
    unittest.main()
