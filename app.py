"""ER Patient Status Board - local, offline Flask app.

Public board:  http://127.0.0.1:5050/
Admin console: http://127.0.0.1:5050/admin   (loopback only; optional ADMIN_PIN)
"""
import hashlib
import json
import os
import re
import secrets
import time
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_from_directory, session, url_for)

import db
from importer import ImportError_, import_snapshot, room_assignment_flags
from rules import custom_flags, flag_rule_id, load_rules, modes as rule_modes, rule_for_flag, save_modes

BASE = Path(__file__).parent
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)
ALLOWED_IMG = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

_key = BASE / ".secret_key"
if not _key.exists():
    _key.write_text(secrets.token_hex(32))
    _key.chmod(0o600)
app.secret_key = _key.read_text()
ADMIN_PIN = os.environ.get("ADMIN_PIN", "")

_ready = False

PATIENT_FIELDS = ["name", "age", "sex", "bed", "chief_complaint", "triage_level",
                  "status", "doctor_id", "notes"]


# ---------------------------------------------------------------- helpers
def triage_rank(level):
    """1 (most urgent) .. 5; 9 if missing/invalid. The display text is never altered."""
    if level is None:
        return 9
    t = str(level).strip().lower()
    m = re.fullmatch(r"(?:esi|level|l)?[\s-]*([1-5])", t)
    if m:
        return int(m.group(1))
    # Longest / most specific phrases first, whole words only.
    for rank, pat in ((5, r"\bnon[\s-]?urgent\b|\bminor\b"), (4, r"\bless[\s-]urgent\b"),
                      (1, r"\bresuscitat\w*|\bimmediate\b|\bcritical\b"), (2, r"\bemergent\b"),
                      (3, r"\burgent\b")):
        if re.search(pat, t):
            return rank
    return 9


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# Data-quality POLICY (not facts from the file): values outside these windows are flagged, never changed.
RANGES = {"hr": (20, 250), "spo2": (50, 100), "temp": (90, 110)}  # temp assumed degrees F
AGE_RANGE = (0, 130)


def parse_ts(s):
    """ISO-8601 only (with or without Z). Returns aware datetime or None."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else None


def vitals_flags(p):
    if p["vitals_at"] is None and all(p[k] is None for k in ("hr", "bp", "spo2", "temp")):
        return ["NO VITALS"]
    flags = []
    for k, label in (("hr", "HR"), ("spo2", "SpO2"), ("temp", "TEMP")):
        v = p[k]
        if v is None:
            flags.append(f"{label} MISSING")
        elif not is_num(v):
            flags.append(f"{label} '{v}'")
        elif not RANGES[k][0] <= v <= RANGES[k][1]:
            flags.append(f"{label} {v:g} IMPLAUSIBLE")
    m = re.fullmatch(r"(\d+)/(\d+)", p["bp"] or "")
    if not p["bp"]:
        flags.append("BP MISSING")
    elif not m or not 50 <= int(m.group(1)) <= 260 or int(m.group(2)) >= int(m.group(1)):
        flags.append(f"BP {p['bp']} CHECK")
    if p["vitals_at"] is None:
        flags.append("VITALS TIME MISSING")
    return flags


def annotate(conn, include_inactive=False):
    """Return (patients, rooms, facility, notes). Adds .flags to each patient. Nothing is modified."""
    fac = conn.execute("SELECT * FROM facilities ORDER BY rowid LIMIT 1").fetchone()
    rows = conn.execute("""
        SELECT p.*, d.name AS doctor_name,
               v.hr, v.bp, v.spo2, v.temp, v.recorded_at AS vitals_at
        FROM patients p
        LEFT JOIN doctors d ON d.id = p.doctor_id
        LEFT JOIN vitals v ON v.id = (SELECT id FROM vitals WHERE patient_id = p.id
                                      ORDER BY recorded_at DESC, id DESC LIMIT 1)
        """ + ("" if include_inactive else "WHERE p.active = 1")).fetchall()
    rooms = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM rooms ORDER BY rowid")}
    patients = [dict(r) for r in rows]
    for p in patients:
        p["discharged"] = (p["status"] or "").strip().lower().startswith("discharged")
        p["rank"] = 10 if p["discharged"] else triage_rank(p["triage_level"])
    live = [p for p in patients if p["active"]]
    id_n = Counter(p["source_id"] for p in live if p["source_id"])
    bed_n = Counter(p["bed"] for p in live if p["bed"] and not p["discharged"])
    snap = parse_ts(fac["snapshot_at"]) if fac else None
    after_snapshot = 0
    for p in patients:
        f = []
        st = (p["status"] or "").lower()
        room = rooms.get(p["room_id"])
        overflow = bool(room and "overflow" in (room["room_type"] or "").lower())
        if p["source_id"] and id_n[p["source_id"]] > 1:
            f.append(f"DUPLICATE ID {p['source_id']}")
        if not p["bed"] and not p["discharged"]:
            f.append("NO BED ASSIGNED")
        elif p["bed"] and not p["discharged"] and bed_n[p["bed"]] > 1 and not overflow:
            f.append(f"BED SHARED x{bed_n[p['bed']]}")
        age = p["age"]
        if age is None:
            f.append("AGE MISSING")
        elif not (isinstance(age, int) and AGE_RANGE[0] <= age <= AGE_RANGE[1]):
            f.append(f"AGE '{age}' INVALID")
        elif room and "pediatric" in (room["room_type"] or "").lower() and age >= 18 and not p["discharged"]:
            f.append(f"AGE {age} IN PEDIATRIC ROOM")
        if p["rank"] == 9 and p["triage_level"] is not None:
            f.append(f"TRIAGE '{p['triage_level']}' INVALID")
        # Triage level and status text are not compared. See rule-decision-log.md.
        if "waiting for bed" in st and p["bed"]:
            f.append("STATUS WAITING FOR BED, BED SET")
        if p["arrival_time"] and parse_ts(p["arrival_time"]) is None:
            f.append(f"ARRIVAL '{p['arrival_time']}' NON-ISO")
        va, ar = parse_ts(p["vitals_at"]), parse_ts(p["arrival_time"])
        if va and ar and va < ar:
            f.append("VITALS TIME BEFORE ARRIVAL")
        if va and snap and va > snap:
            after_snapshot += 1
        if not p["discharged"]:
            f += vitals_flags(p)
        f += room_assignment_flags(
            p["bed"], p["room_id"], p["source_id"],
            room["occupied_by"] if room else None,
            patient_id=p["id"], discharged=p["discharged"])
        f += custom_flags(p)
        p["all_flags"] = f
        p["spo2_low"] = is_num(p["spo2"]) and p["spo2"] < 92  # clinical colour only, not a data flag
    # rooms whose source occupied_by disagrees with who is actually assigned there
    by_room = {}
    for p in live:
        if p["room_id"] and not p["discharged"]:
            by_room.setdefault(p["room_id"], set()).add(p["source_id"])
    disagree = {rid for rid, r in rooms.items()
                if by_room.get(rid, set()) != ({r["occupied_by"]} if r["occupied_by"] else set())}
    applied = {rid for rid, mode in rule_modes().items() if mode == "apply"}
    for p in patients:
        p["flags"] = [fl for fl in p["all_flags"] if (flag_rule_id(fl) in applied or flag_rule_id(fl) is None)]
        p["review"] = "incomplete" if p["flags"] else "complete"
    notes = {"rooms_disagree": len(disagree), "disagree_ids": disagree, "vitals_after_snapshot": after_snapshot,
             "by_room": {k: len(v) for k, v in by_room.items()}, "applied": applied}
    return patients, rooms, fac, notes


def board_data():
    """Team wall: complete charts only. Incomplete charts stay on the admin review list."""
    conn = db.connect()
    patients, rooms, fac, notes = annotate(conn)
    last = conn.execute("SELECT MAX(ts) FROM audit_log").fetchone()[0]
    conn.close()
    clean = [p for p in patients if p["review"] == "complete"]
    clean.sort(key=lambda p: (p["rank"], p["bed"] or "~", p["name"]))
    groups = {}
    for p in clean:
        groups.setdefault(p["rank"], []).append(p)
    per_room = Counter(p["room_id"] for p in clean if p["room_id"] and not p["discharged"])
    chips = []
    for rid, r in rooms.items():
        n = per_room[rid]
        chips.append({"name": r["name"], "n": n, "state": "occ" if n else "avail", "type": r["room_type"]})
    sysst = (fac["system_status"] or "") if fac else ""
    sys_class = "ok" if re.match(r"\s*(normal|ok|operational|online|live)\b", sysst, re.I) else "bad"
    return {"facility": fac, "groups": sorted(groups.items()), "total": len(clean),
            "held": len(patients) - len(clean), "chips": chips, "last_change": last,
            "sys_class": sys_class, "notes": notes}


@app.template_filter("fmt")
def fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


TRIAGE_LABEL = {1: "TRIAGE 1 - RESUSCITATION", 2: "TRIAGE 2 - EMERGENT", 3: "TRIAGE 3 - URGENT",
                4: "TRIAGE 4 - LESS URGENT", 5: "TRIAGE 5 - NON-URGENT",
                9: "TRIAGE NOT SET / INVALID", 10: "DISCHARGED"}
app.jinja_env.globals["TRIAGE_LABEL"] = TRIAGE_LABEL



app.jinja_env.globals["rule_for_flag"] = rule_for_flag


# ---------------------------------------------------------------- public board
@app.get("/")
def board():
    return render_template("board.html", **board_data())


@app.get("/board/fragment")
def board_fragment():
    return render_template("_board_body.html", **board_data())


# ---------------------------------------------------------------- admin guard
@app.before_request
def guard():
    global _ready
    if not _ready:
        db.init_db()
        _ready = True
    if not request.path.startswith("/admin"):
        return
    # Admin is for the laptop itself only, even if the server is accidentally bound wider.
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    if request.path == "/admin/login":
        return
    if ADMIN_PIN and not session.get("authed"):
        return redirect(url_for("login"))
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Bad CSRF token - reload the page and try again.")


@app.context_processor
def inject():
    session.setdefault("csrf", secrets.token_hex(16))
    return {"csrf": session["csrf"]}


def actor():
    return "local-admin"


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if not ADMIN_PIN:
        return redirect(url_for("admin_home"))
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("pin", ""), ADMIN_PIN):
            session["authed"] = True
            return redirect(url_for("admin_home"))
        time.sleep(1.5)  # slow down guessing
        flash("Wrong PIN.", "error")
    return render_template("login.html")


# ---------------------------------------------------------------- admin: home / patients
def removal_info(conn, patient_ids):
    """Latest archive reason for each removed chart."""
    info = {}
    ids = [str(i) for i in patient_ids]
    if not ids:
        return info
    rows = conn.execute(
        "SELECT entity_id, ts, actor, detail FROM audit_log WHERE action='archive' AND entity='patient' "
        "AND entity_id IN (%s) ORDER BY id DESC" % ",".join("?" * len(ids)), ids)
    for r in rows:
        if r["entity_id"] in info:
            continue
        try:
            detail = json.loads(r["detail"]) if r["detail"] else {}
        except json.JSONDecodeError:
            detail = {}
        reason = (detail.get("reason") or "").strip()
        info[r["entity_id"]] = {"reason": reason or None, "at": r["ts"], "by": r["actor"]}
    return info


@app.get("/admin")
def admin_home():
    tab = request.args.get("tab", "complete")
    if tab not in ("complete", "incomplete", "removed"):
        tab = "complete"
    conn = db.connect()
    patients, _, _, notes = annotate(conn, include_inactive=True)
    active = [p for p in patients if p["active"]]
    removed = [p for p in patients if not p["active"]]
    complete = sorted((p for p in active if p["review"] == "complete"), key=lambda p: p["name"])
    incomplete = sorted((p for p in active if p["review"] == "incomplete"),
                        key=lambda p: (-len(p["flags"]), p["name"]))
    info = removal_info(conn, [p["id"] for p in removed])
    for p in removed:
        meta = info.get(str(p["id"]), {})
        p["removed_reason"] = meta.get("reason")
        p["removed_at"] = meta.get("at")
        p["removed_by"] = meta.get("by")
    removed.sort(key=lambda p: (p["removed_at"] or "", p["name"]), reverse=True)
    last_import = conn.execute("SELECT ts, mode, summary FROM imports ORDER BY id DESC LIMIT 1").fetchone()
    pending_photos = conn.execute("SELECT COUNT(*) FROM photos WHERE transcribed=0").fetchone()[0]
    conn.close()
    return render_template("admin.html", tab=tab, incomplete=incomplete, complete=complete,
                           removed=removed, notes=notes, last_import=last_import,
                           pending_photos=pending_photos)


_UNSET = object()


def patient_id_from_form(form, current):
    """Patient ID submitted on an incomplete chart. None leaves the stored id as it is."""
    raw = form.get("source_id", "").strip()
    if not raw:
        if current:
            raise ValueError("Patient ID is required.")
        return None
    if len(raw) > 40 or any(ch.isspace() for ch in raw):
        raise ValueError("Patient ID must be one token, up to 40 characters, with no spaces.")
    return raw


def form_values(form):
    def txt(k):
        v = form.get(k, "").strip()
        return v or None
    age = txt("age")
    if age is not None:
        if not age.isdigit() or not 0 <= int(age) <= 130:
            raise ValueError("Age must be a whole number 0-130.")
        age = int(age)
    vals = {k: txt(k) for k in PATIENT_FIELDS}
    vals["age"] = age
    if not vals["name"]:
        raise ValueError("Patient name is required.")
    return vals


def vitals_from_form(form):
    out = {}
    for k in ("hr", "spo2", "temp"):
        v = form.get("v_" + k, "").strip()
        if v:
            try:
                out[k] = float(v)
            except ValueError:
                raise ValueError(f"Vital '{k}' must be a number.")
    bp = form.get("v_bp", "").strip()
    if bp:
        if not re.fullmatch(r"\d{2,3}\s*/\s*\d{2,3}", bp):
            raise ValueError("BP must look like 120/80.")
        out["bp"] = bp.replace(" ", "")
    return out


def room_for_bed(conn, bed):
    if not bed:
        return None
    r = conn.execute("SELECT id FROM rooms WHERE id=? OR lower(name)=lower(?)", (bed, bed)).fetchone()
    return r[0] if r else None


def _set_room_occupant(conn, room_id, token, patient_id, clear=False):
    """Write the room master. clear releases this patient; otherwise they become occupied_by."""
    row = conn.execute("SELECT occupied_by, status FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not row:
        return False
    if clear:
        if row["occupied_by"] != token:
            return False
        new_occ, new_status = None, "available"
    else:
        new_occ, new_status = token, "occupied"
        if row["occupied_by"] == new_occ and (row["status"] or "").lower() == new_status:
            return False
    conn.execute("UPDATE rooms SET occupied_by=?, status=? WHERE id=?", (new_occ, new_status, room_id))
    db.audit(conn, actor(), "update", "room", room_id, {
        "occupied_by": {"from": row["occupied_by"], "to": new_occ},
        "status": {"from": row["status"], "to": new_status},
        "patient_id": patient_id,
    })
    return True


def apply_room_master(conn, patient_id, source_id, old_room_id, new_room_id, discharged,
                      previous_source_id=_UNSET):
    """Keep rooms.occupied_by in step with a chart a person just verified.

    A corrected patient id claims the room under the new id and releases the old id.
    """
    token = source_id or f"manual-{patient_id}"
    if previous_source_id is _UNSET:
        old_token = token
    else:
        old_token = previous_source_id or f"manual-{patient_id}"
    changed = False
    if discharged:
        for rid in {old_room_id, new_room_id}:
            if rid:
                changed = _set_room_occupant(conn, rid, old_token, patient_id, clear=True) or changed
        return changed
    if old_room_id and old_room_id != new_room_id:
        changed = _set_room_occupant(conn, old_room_id, old_token, patient_id, clear=True) or changed
    if new_room_id:
        changed = _set_room_occupant(conn, new_room_id, token, patient_id) or changed
    return changed


def save_vitals(conn, pid, vit, origin):
    if vit:
        conn.execute("INSERT INTO vitals (patient_id,recorded_at,hr,bp,spo2,temp,origin) VALUES (?,?,?,?,?,?,?)",
                     (pid, db.now(), vit.get("hr"), vit.get("bp"), vit.get("spo2"), vit.get("temp"), origin))


def create_patient(conn, vals, vit, origin, extra_detail=None):
    ts = db.now()
    fac = conn.execute("SELECT id FROM facilities ORDER BY rowid LIMIT 1").fetchone()
    cur = conn.execute(
        """INSERT INTO patients (facility_id,name,age,sex,room_id,bed,chief_complaint,triage_level,status,
           doctor_id,notes,origin,active,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
        (fac[0] if fac else None, vals["name"], vals["age"], vals["sex"], room_for_bed(conn, vals["bed"]),
         vals["bed"], vals["chief_complaint"], vals["triage_level"], vals["status"], vals["doctor_id"],
         vals["notes"], origin, ts, ts))
    pid = cur.lastrowid
    save_vitals(conn, pid, vit, origin)
    apply_room_master(conn, pid, None, None, room_for_bed(conn, vals["bed"]),
                      (vals["status"] or "").strip().lower().startswith("discharged"))
    db.audit(conn, actor(), "create", "patient", pid,
             {"origin": origin, "values": vals, "vitals": vit, **(extra_detail or {})})
    return pid


def update_patient(conn, pid, vals, vit, origin, extra_detail=None, source_id=None):
    before = dict(conn.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone())
    new_room = room_for_bed(conn, vals["bed"])
    new_source = before["source_id"] if source_id is None else source_id
    changes = {k: {"from": before[k], "to": v} for k, v in vals.items() if before[k] != v}
    if new_source != before["source_id"]:
        changes["source_id"] = {"from": before["source_id"], "to": new_source}
    if changes or vit:
        conn.execute(
            """UPDATE patients SET source_id=?,name=?,age=?,sex=?,bed=?,room_id=?,chief_complaint=?,triage_level=?,
               status=?,doctor_id=?,notes=?,updated_at=? WHERE id=?""",
            (new_source, vals["name"], vals["age"], vals["sex"], vals["bed"], new_room,
             vals["chief_complaint"], vals["triage_level"], vals["status"], vals["doctor_id"],
             vals["notes"], db.now(), pid))
        save_vitals(conn, pid, vit, origin)
        db.audit(conn, actor(), "update", "patient", pid,
                 {"origin": origin, "changes": changes, "new_vitals": vit, **(extra_detail or {})})
    discharged = (vals["status"] or "").strip().lower().startswith("discharged")
    room_changed = apply_room_master(
        conn, pid, new_source, before["room_id"], new_room, discharged,
        previous_source_id=before["source_id"])
    return bool(changes or vit or room_changed)


def _issues(conn, pid):
    """Data-issue list for one chart, using the same rules as the team board."""
    patients, _, _, _ = annotate(conn, include_inactive=True)
    row = next((x for x in patients if x["id"] == pid), None)
    return row["flags"] if row else []


def doctors():
    conn = db.connect()
    rows = conn.execute("SELECT id, name, role, specialty FROM doctors ORDER BY name").fetchall()
    conn.close()
    return rows


def room_choices():
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, name, room_type, occupied_by, status FROM rooms ORDER BY name").fetchall()
    conn.close()
    return rows


@app.route("/admin/patients/new", methods=["GET", "POST"])
def patient_new():
    if request.method == "POST":
        conn = db.connect()
        try:
            vals, vit = form_values(request.form), vitals_from_form(request.form)
            with conn:
                pid = create_patient(conn, vals, vit, "manual")
            issues = _issues(conn, pid)
            if issues:
                flash(f"Created '{vals['name']}'. Incomplete — not on the team board until verified: "
                      + "; ".join(issues), "error")
            else:
                flash(f"Created '{vals['name']}'. Complete — now on the team board.", "ok")
            return redirect(url_for("admin_home", tab="incomplete" if issues else "complete"))
        except ValueError as e:
            flash(str(e), "error")
        finally:
            conn.close()
    rooms = room_choices()
    return render_template("patient_form.html", p=request.form, doctors=doctors(), rooms=rooms,
                           room_ids=[r["id"] for r in rooms], mode="new")


@app.route("/admin/patients/<int:pid>/edit", methods=["GET", "POST"])
def patient_edit(pid):
    conn = db.connect()
    p = conn.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        abort(404)
    if request.method == "POST":
        try:
            issues_before = _issues(conn, pid)
            vals, vit = form_values(request.form), vitals_from_form(request.form)
            # Patient ID is part of verification. A complete chart keeps the id it already has.
            source_id = patient_id_from_form(request.form, p["source_id"]) if issues_before else None
            with conn:
                changed = update_patient(conn, pid, vals, vit, "manual", source_id=source_id)
            issues = _issues(conn, pid)
            active = conn.execute("SELECT active FROM patients WHERE id=?", (pid,)).fetchone()[0]
            prefix = "Saved. " if changed else "No changes. "
            if issues:
                flash(prefix + "Still incomplete — not on the team board: " + "; ".join(issues), "error")
            elif active:
                flash(prefix + "Complete — this chart is on the team board.", "ok")
            else:
                flash(prefix + "Complete. Restore the chart to put it on the team board.", "ok")
            conn.close()
            if not active:
                dest = "removed"
            elif issues:
                dest = "incomplete"
            else:
                dest = "complete"
            return redirect(url_for("admin_home", tab=dest))
        except ValueError as e:
            flash(str(e), "error")
            base = dict(p)
            for k in list(PATIENT_FIELDS) + ["source_id"]:
                if k in request.form:
                    base[k] = request.form.get(k)
            p = base
    hist = conn.execute("SELECT * FROM audit_log WHERE entity='patient' AND entity_id=? ORDER BY id DESC LIMIT 15",
                        (str(pid),)).fetchall()
    vitals = conn.execute("SELECT * FROM vitals WHERE patient_id=? ORDER BY recorded_at DESC, id DESC LIMIT 10",
                          (pid,)).fetchall()
    issues = _issues(conn, pid)
    on_board = not issues and (p["active"] if not isinstance(p, dict) else True)
    conn.close()
    rooms = room_choices()
    return render_template("patient_form.html", p=p, pid=pid, doctors=doctors(), rooms=rooms,
                           room_ids=[r["id"] for r in rooms], mode="edit",
                           history=hist, vitals=vitals, issues=issues, on_board=on_board)


@app.post("/admin/patients/<int:pid>/active")
def patient_active(pid):
    """Remove from / restore to the board. Rows are never hard-deleted."""
    on = request.form.get("active") == "1"
    reason = (request.form.get("reason") or "").strip()
    if not on and not reason:
        flash("Enter a reason before removing a chart.", "error")
        return redirect(url_for("admin_home", tab=request.form.get("tab") or "complete"))
    conn = db.connect()
    with conn:
        n = conn.execute("UPDATE patients SET active=?, updated_at=? WHERE id=?", (int(on), db.now(), pid)).rowcount
        if not n:
            abort(404)
        db.audit(conn, actor(), "restore" if on else "archive", "patient", pid,
                 {"reason": reason} if reason else None)
    if on:
        dest = "incomplete" if _issues(conn, pid) else "complete"
        flash("Restored.", "ok")
    else:
        dest = "removed"
        flash("Removed. It is listed under Removed with your reason.", "ok")
    conn.close()
    return redirect(url_for("admin_home", tab=dest))


def _rule_rows(conn):
    from collections import Counter as _Counter
    catalog = load_rules()
    mode_map = rule_modes()
    patients, _, _, _ = annotate(conn)
    held = _Counter()
    for p in patients:
        if not p["active"]:
            continue
        for rid in {flag_rule_id(fl) for fl in p["all_flags"] if flag_rule_id(fl)}:
            held[rid] += 1
    return [{**r, "mode": mode_map[r["id"]], "held": held[r["id"]]} for r in catalog]


# ---------------------------------------------------------------- admin: JSON import
@app.route("/admin/import", methods=["GET", "POST"])
def import_json():
    if request.method == "POST" and request.form.get("action") == "save_rules":
        conn = db.connect()
        try:
            catalog = load_rules()
            before = rule_modes()
            chosen = {}
            for rule in catalog:
                mode = request.form.get("rule_" + rule["id"], "apply")
                if mode not in ("apply", "ignore"):
                    raise ValueError("Each rule must be Apply or Ignore.")
                chosen[rule["id"]] = mode
            save_modes(chosen)
            changed = {k: {"from": before[k], "to": chosen[k]} for k in chosen if before[k] != chosen[k]}
            if changed:
                with conn:
                    db.audit(conn, actor(), "update", "import_rules", "import_rules.yaml", changed)
            ignored = [r["name"] for r in catalog if chosen[r["id"]] == "ignore"]
            if ignored:
                flash("Import rules saved. Ignored: " + ", ".join(ignored)
                      + ". Charts already loaded are rechecked. Nothing in the snapshot is rewritten.", "ok")
            else:
                flash("Import rules saved. Every rule is set to apply.", "ok")
        except ValueError as e:
            flash(str(e), "error")
        finally:
            conn.close()
        return redirect(url_for("import_json"))
    if request.method == "POST":
        up = request.files.get("file")
        try:
            raw = up.read().decode("utf-8-sig") if up and up.filename else request.form.get("raw", "")
        except UnicodeDecodeError:
            flash("File is not valid UTF-8 text; nothing was imported.", "error")
            return redirect(url_for("import_json"))
        mode = request.form.get("mode", "replace")
        if not raw.strip():
            flash("Paste JSON or choose a file.", "error")
        elif mode == "replace" and request.form.get("confirm", "").strip().upper() != "REPLACE":
            flash("Type REPLACE to confirm wiping the current board.", "error")
        else:
            conn = db.connect()
            try:
                with conn:  # all-or-nothing: a bad file leaves the board untouched
                    counts = import_snapshot(conn, raw, mode, actor())
                flash("Import OK (%s): " % mode + ", ".join(f"{v} {k}" for k, v in counts.items() if not isinstance(v, list)), "ok")
                modes = rule_modes()
                if counts.get("duplicate_patient_ids") and modes.get("duplicate_id") == "apply":
                    flash("WARNING: duplicate patient IDs in source (kept as separate patients, held for review): "
                          + ", ".join(counts["duplicate_patient_ids"]), "error")
                room_bits = []
                if counts.get("room_not_in_master") and modes.get("room_not_in_master") == "apply":
                    room_bits.append("%s chart(s) name a room that is not in the room list" % counts["room_not_in_master"])
                if counts.get("not_assigned_in_room") and modes.get("not_assigned_in_room") == "apply":
                    room_bits.append("%s are not the patient stored on that room" % counts["not_assigned_in_room"])
                if room_bits:
                    flash("Room master check: " + ", ".join(room_bits) + ". Held for verification.", "error")
                ignored = [r["name"] for r in load_rules() if modes.get(r["id"]) == "ignore"]
                if ignored:
                    flash("Rules set to ignore, so they do not hold a chart: " + ", ".join(ignored) + ".", "ok")
                return redirect(url_for("admin_home", tab="incomplete"))
            except ImportError_ as e:
                flash(f"Import rejected, nothing changed: {e}", "error")
            except sqlite3.Error as e:
                flash(f"Database error, nothing changed: {e}", "error")
            finally:
                conn.close()
    conn = db.connect()
    hist = conn.execute("SELECT id, ts, actor, mode, sha256, summary FROM imports ORDER BY id DESC LIMIT 10").fetchall()
    rules = _rule_rows(conn)
    conn.close()
    return render_template("import.html", history=hist, rules=rules)


# ---------------------------------------------------------------- admin: photo -> record
@app.route("/admin/photos", methods=["GET", "POST"])
def photos():
    conn = db.connect()
    if request.method == "POST":
        f = request.files.get("photo")
        ext = Path(f.filename).suffix.lower() if f and f.filename else ""
        if ext not in ALLOWED_IMG:
            flash("Choose an image file (jpg, png, gif, webp, heic).", "error")
        else:
            data = f.read()
            sha = hashlib.sha256(data).hexdigest()
            name = f"{sha[:16]}{ext}"
            (UPLOADS / name).write_bytes(data)
            with conn:
                cur = conn.execute("INSERT INTO photos (filename,original_name,sha256,uploaded_at,actor) VALUES (?,?,?,?,?)",
                                   (name, f.filename, sha, db.now(), actor()))
                db.audit(conn, actor(), "photo_upload", "photo", cur.lastrowid, {"original_name": f.filename, "sha256": sha})
            conn.close()
            return redirect(url_for("photo_transcribe", photo_id=cur.lastrowid))
    rows = conn.execute("SELECT * FROM photos ORDER BY transcribed, id DESC").fetchall()
    conn.close()
    return render_template("photos.html", photos=rows)


@app.get("/admin/uploads/<name>")
def upload_file(name):
    return send_from_directory(UPLOADS, name)


@app.route("/admin/photos/<int:photo_id>", methods=["GET", "POST"])
def photo_transcribe(photo_id):
    conn = db.connect()
    photo = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not photo:
        conn.close()
        abort(404)
    if request.method == "POST":
        try:
            target = request.form.get("target", "new")
            vit = vitals_from_form(request.form)
            detail = {"photo_id": photo_id, "photo_sha256": photo["sha256"]}
            with conn:
                if target == "new":
                    pid = create_patient(conn, form_values(request.form), vit, "photo", detail)
                else:
                    pid = int(target)
                    cur = conn.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
                    if not cur:
                        raise ValueError("Selected patient no longer exists.")
                    # Only fields actually typed on the form overwrite; blank keeps the existing value.
                    posted = {k: request.form.get(k, "").strip() for k in PATIENT_FIELDS}
                    merged = {k: (posted[k] if posted[k] != "" else cur[k]) for k in PATIENT_FIELDS}
                    vals = form_values({k: ("" if v is None else str(v)) for k, v in merged.items()})
                    update_patient(conn, pid, vals, vit, "photo", detail)
                conn.execute("UPDATE photos SET transcribed=1 WHERE id=?", (photo_id,))
            flash("Photo entry saved to the record.", "ok")
            conn.close()
            return redirect(url_for("photos"))
        except ValueError as e:
            flash(str(e), "error")
    existing = conn.execute("SELECT id, name, bed, age FROM patients WHERE active=1 ORDER BY name").fetchall()
    conn.close()
    rooms = room_choices()
    return render_template("photo_transcribe.html", photo=photo, existing=existing, doctors=doctors(),
                           rooms=rooms, room_ids=[r["id"] for r in rooms], p=request.form)


@app.get("/admin/photos/prefill/<int:pid>")
def photo_prefill(pid):
    conn = db.connect()
    r = conn.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not r:
        abort(404)
    return {k: r[k] for k in PATIENT_FIELDS}


# ---------------------------------------------------------------- admin: audit log
@app.get("/admin/audit")
def audit_view():
    conn = db.connect()
    q = request.args.get("action", "")
    rows = conn.execute("SELECT * FROM audit_log" + (" WHERE action=?" if q else "") + " ORDER BY id DESC LIMIT 500",
                        (q,) if q else ()).fetchall()
    actions = [r[0] for r in conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY 1")]
    conn.close()
    parsed = []
    for r in rows:
        d = dict(r)
        d["detail_pretty"] = json.dumps(json.loads(r["detail"]), indent=1, ensure_ascii=False) if r["detail"] else ""
        parsed.append(d)
    return render_template("audit.html", rows=parsed, actions=actions, sel=q)


if __name__ == "__main__":
    db.init_db()
    _ready = True
    # Bind to loopback only: nothing on the network can reach this app.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5050)), debug=False)
