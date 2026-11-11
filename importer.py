"""Ingest an ER board JSON snapshot into SQLite.

The JSON is the source of truth: values are stored verbatim (only trimmed);
nothing is invented. Because the exact key names of the snapshot can vary,
field lookup accepts a few common aliases, and any unrecognised keys are
preserved untouched in each row's extra_json column.
"""
import hashlib
import json

from db import audit, dump_all, now

ALIASES = {
    "name": ["name", "full_name", "patient_name", "display_name"],
    "id": ["id", "uuid"],
    "facility_id": ["id", "facility_id", "uuid"],
    "doctor_id": ["id", "doctor_id", "uuid"],
    "room_id": ["id", "room_id", "uuid"],
    "patient_id": ["id", "patient_id", "mrn", "uuid"],
    "age": ["age"],
    "sex": ["sex", "gender"],
    "bed": ["bed", "bed_number", "bed_id", "location"],
    "room": ["room_id", "room", "room_name"],
    "complaint": ["chief_complaint", "complaint", "reason_for_visit", "presenting_complaint"],
    "triage": ["triage_level", "triage", "esi", "acuity", "priority"],
    "status": ["status", "clinical_status", "current_status", "patient_status"],
    "doctor": ["doctor_id", "attending_id", "attending", "physician", "doctor", "assigned_doctor"],
    "notes": ["notes", "note", "comments"],
    "system_status": ["system_status", "status", "operational_status", "mode"],
    "role": ["role", "title"],
    "specialty": ["specialty"],
    "room_type": ["type", "room_type", "category"],
    "arrival": ["arrival_time", "arrived_at"],
    "hr": ["hr", "heart_rate", "pulse"],
    "bp": ["bp", "blood_pressure"],
    "spo2": ["spo2", "o2_sat", "oxygen_saturation", "sp_o2"],
    "temp": ["temp_f", "temp", "temperature"],
    "ts": ["last_updated", "recorded_at", "timestamp", "time", "taken_at", "ts"],
}


class ImportError_(ValueError):
    pass


def pick(d, key, skip=()):
    """Return (value, used_key) for the first alias present with a non-empty value."""
    for k in ALIASES[key]:
        if k in skip:
            continue
        if k in d and d[k] not in (None, ""):
            return d[k], k
    return None, None


def clean(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    s = str(v).strip()
    return s or None


def num(v):
    """Number if numeric, otherwise the original text (kept verbatim, flagged on the board)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return str(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v).strip()


def as_list(v):
    if v is None:
        return []
    if isinstance(v, dict):
        # either a single record or {id: record}
        if all(isinstance(x, dict) for x in v.values()) and v:
            return [{"id": k, **x} if "id" not in x else x for k, x in v.items()]
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    raise ImportError_("Expected a list or object of records")


def room_assignment_flags(bed, room_id, source_id, occupied_by, patient_id=None, discharged=False):
    """A chart needs verification when its room is missing from the room master,
    or the room master does not list this patient in occupied_by.

    Discharged charts and charts with no bed are left to the other checks.
    """
    if discharged or not bed:
        return []
    if not room_id:
        return [f"ROOM '{bed}' NOT IN MASTER"]
    token = source_id or (f"manual-{patient_id}" if patient_id is not None else None)
    if occupied_by == token:
        return []
    if occupied_by:
        return [f"NOT ASSIGNED IN ROOM {room_id} (record says {occupied_by})"]
    return [f"NOT ASSIGNED IN ROOM {room_id}"]


def extras(d, used):
    rest = {k: v for k, v in d.items() if k not in used}
    return json.dumps(rest, ensure_ascii=False) if rest else None


def parse_bp(rec):
    v, k = pick(rec, "bp")
    used = {k} if k else set()
    if v is None and ("systolic" in rec or "bp_systolic" in rec):
        s = rec.get("systolic", rec.get("bp_systolic"))
        dia = rec.get("diastolic", rec.get("bp_diastolic"))
        v = f"{s}/{dia}" if s is not None and dia is not None else None
        used |= {"systolic", "diastolic", "bp_systolic", "bp_diastolic"}
    if isinstance(v, dict):
        s = v.get("systolic", v.get("sys"))
        dia = v.get("diastolic", v.get("dia"))
        v = f"{s}/{dia}"
    return clean(v), used


def parse_vitals(rec):
    bp, bp_used = parse_bp(rec)
    used = set(bp_used)
    out = {"bp": bp}
    for key in ("hr", "spo2", "temp"):
        v, k = pick(rec, key)
        out[key] = num(v)
        if k:
            used.add(k)
    ts, k = pick(rec, "ts")
    if k:
        used.add(k)
    out["recorded_at"] = clean(ts)  # None if the source gave no time; never filled in
    out["extra_json"] = extras(rec, used)
    return out


def validate(data):
    if not isinstance(data, dict):
        raise ImportError_("Top level of the JSON must be an object.")
    known = {"facility", "facilities", "patients", "doctors", "rooms", "vitals"}
    if not (known & set(data)):
        raise ImportError_(
            "No recognised sections found. Expected at least one of: "
            "facility/facilities, patients, doctors, rooms, vitals. Found: "
            + ", ".join(list(data)[:10]))


def import_snapshot(conn, raw_text, mode, actor):
    """Parse + load inside the caller's transaction. Returns a summary dict."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ImportError_(f"Invalid JSON: {e}")
    validate(data)
    if mode not in ("replace", "merge"):
        raise ImportError_("Unknown import mode")

    sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    ts = now()
    counts = {"facilities": 0, "doctors": 0, "rooms": 0, "patients": 0, "vitals": 0}

    if mode == "replace":
        # Replace wipes everything, including manual/photo entries. The full prior state is
        # copied into board_backups first, inside the same transaction, so it is never lost.
        conn.execute("INSERT INTO board_backups (ts,actor,reason,dump_json) VALUES (?,?,?,?)",
                     (ts, actor, "before replace import " + sha[:12], json.dumps(dump_all(conn), ensure_ascii=False)))
        for t in ("vitals", "patients", "rooms", "doctors", "facilities"):
            conn.execute(f"DELETE FROM {t}")

    # ---- facilities
    fac_src = data.get("facilities", data.get("facility"))
    if isinstance(fac_src, str):
        fac_src = {"name": fac_src}
    default_fac = None
    for i, f in enumerate(as_list(fac_src)):
        if i == 0:  # top-level status / snapshot time describe the (first) facility
            f = {**f}
            for k in ("system_status", "snapshot_generated_at"):
                if k in data and k not in f:
                    f[k] = data[k]
        name, nk = pick(f, "name")
        if not name:
            raise ImportError_(f"Facility #{i + 1} has no name")
        fid, ik = pick(f, "facility_id")
        fid = clean(fid) or clean(name)
        st, sk = pick(f, "system_status")
        used = {k for k in (nk, ik, sk, "snapshot_generated_at") if k}
        conn.execute(
            """INSERT INTO facilities (id,name,system_status,snapshot_at,extra_json,updated_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, system_status=excluded.system_status,
               snapshot_at=excluded.snapshot_at, extra_json=excluded.extra_json, updated_at=excluded.updated_at""",
            (fid, clean(name), clean(st), clean(f.get("snapshot_generated_at")), extras(f, used), ts))
        default_fac = default_fac or fid
        counts["facilities"] += 1

    # ---- doctors
    for i, d in enumerate(as_list(data.get("doctors"))):
        name, nk = pick(d, "name")
        did, ik = pick(d, "doctor_id")
        did = clean(did) or clean(name)
        if not name:
            raise ImportError_(f"Doctor #{i + 1} has no name")
        role, rk = pick(d, "role")
        spec, spk = pick(d, "specialty")
        fac = clean(d.get("facility_id")) or default_fac
        used = {k for k in (nk, ik, rk, spk, "facility_id") if k}
        conn.execute(
            """INSERT INTO doctors (id,facility_id,name,role,specialty,extra_json) VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET facility_id=excluded.facility_id, name=excluded.name,
               role=excluded.role, specialty=excluded.specialty, extra_json=excluded.extra_json""",
            (did, fac, clean(name), clean(role), clean(spec), extras(d, used)))
        counts["doctors"] += 1

    # ---- rooms
    for i, r in enumerate(as_list(data.get("rooms"))):
        name, nk = pick(r, "name")
        rid, ik = pick(r, "room_id")
        rid = clean(rid) or clean(name)
        if not rid:
            raise ImportError_(f"Room #{i + 1} has no id or name")
        rtype, tk = pick(r, "room_type")
        st, sk = pick(r, "status")
        fac = clean(r.get("facility_id")) or default_fac
        used = {k for k in (nk, ik, tk, sk, "facility_id", "occupied_by") if k}
        conn.execute(
            """INSERT INTO rooms (id,facility_id,name,room_type,status,occupied_by,extra_json) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET facility_id=excluded.facility_id, name=excluded.name,
               room_type=excluded.room_type, status=excluded.status, occupied_by=excluded.occupied_by,
               extra_json=excluded.extra_json""",
            (rid, fac, clean(name) or rid, clean(rtype), clean(st), clean(r.get("occupied_by")), extras(r, used)))
        counts["rooms"] += 1

    room_ids = {row[0] for row in conn.execute("SELECT id FROM rooms")}
    occupied = {row[0]: row[1] for row in conn.execute("SELECT id, occupied_by FROM rooms")}
    room_names = {row[0]: row[1] for row in conn.execute("SELECT id,name FROM rooms")}
    doctor_ids = {row[0] for row in conn.execute("SELECT id FROM doctors")}
    doctor_by_name = {row[1]: row[0] for row in conn.execute("SELECT id,name FROM doctors")}

    # ---- patients (+ nested vitals)
    # Source ids are NOT assumed unique (a real snapshot repeated one). Each source row becomes
    # its own patient row; merge-mode only updates an existing row when the id is unambiguous.
    incoming = as_list(data.get("patients"))
    id_counts = {}
    for p in incoming:
        s_, _ = pick(p, "patient_id")
        if clean(s_):
            id_counts[clean(s_)] = id_counts.get(clean(s_), 0) + 1
    duplicate_ids = sorted(k for k, n in id_counts.items() if n > 1)
    not_in_master = not_assigned = 0
    pids_by_source = {}
    for i, p in enumerate(incoming):
        name, nk = pick(p, "name")
        if not name:
            first, last = p.get("first_name"), p.get("last_name")
            if first or last:
                name, nk = f"{first or ''} {last or ''}".strip(), None
        if not name:
            raise ImportError_(f"Patient #{i + 1} has no name")
        sid, ik = pick(p, "patient_id")
        sid = clean(sid)
        age, ak = pick(p, "age")
        age = num(age)
        if isinstance(age, float) and age.is_integer():
            age = int(age)
        sex, xk = pick(p, "sex")
        room_raw, rk = pick(p, "room")
        bed_raw, bk = pick(p, "bed", skip=("room_id",))
        bed = clean(bed_raw) or clean(room_raw)
        room_id = next((c for c in (clean(room_raw), clean(bed_raw)) if c in room_ids), None)
        cc, ck = pick(p, "complaint")
        tr, tk = pick(p, "triage")
        st, sk = pick(p, "status")
        arr, arrk = pick(p, "arrival")
        doc_raw, dk = pick(p, "doctor")
        doc_raw = clean(doc_raw)
        doc_id = doc_raw if doc_raw in doctor_ids else doctor_by_name.get(doc_raw)
        notes, nok = pick(p, "notes")
        fac = clean(p.get("facility_id")) or default_fac
        used = {k for k in (nk, ik, ak, xk, rk, bk, ck, tk, sk, arrk, dk, nok, "facility_id",
                            "vitals", "first_name", "last_name") if k}
        if doc_raw and not doc_id:
            used.discard(dk)  # keep what the source said rather than dropping it
        values = [fac, clean(name), age, clean(sex), room_id, bed,
                  clean(cc), clean(tr), clean(st), doc_id, clean(notes), clean(arr), extras(p, used), ts]
        existing = None
        if mode == "merge" and sid and id_counts[sid] == 1:
            rows = conn.execute("SELECT id FROM patients WHERE source_id=?", (sid,)).fetchall()
            existing = rows[0][0] if len(rows) == 1 else None
        if existing:
            conn.execute("""UPDATE patients SET facility_id=?,name=?,age=?,sex=?,room_id=?,bed=?,chief_complaint=?,
                triage_level=?,status=?,doctor_id=?,notes=?,arrival_time=?,extra_json=?,updated_at=?,active=1
                WHERE id=?""", (*values, existing))
            pk = existing
        else:
            cur = conn.execute("""INSERT INTO patients (source_id,facility_id,name,age,sex,room_id,bed,chief_complaint,
                triage_level,status,doctor_id,notes,arrival_time,extra_json,created_at,updated_at,origin,active)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'import',1)""", (sid, *values, ts))
            pk = cur.lastrowid
        if sid:
            pids_by_source.setdefault(sid, []).append(pk)
        for flag in room_assignment_flags(
                bed, room_id, sid, occupied.get(room_id) if room_id else None,
                discharged=(clean(st) or "").lower().startswith("discharged")):
            if flag.startswith("ROOM "):
                not_in_master += 1
            else:
                not_assigned += 1
        counts["patients"] += 1

        for v in as_list(p.get("vitals")):
            pv = parse_vitals(v)
            if existing and conn.execute(
                    "SELECT 1 FROM vitals WHERE patient_id=? AND recorded_at IS ? AND hr IS ? AND bp IS ? AND spo2 IS ? AND temp IS ?",
                    (pk, pv["recorded_at"], pv["hr"], pv["bp"], pv["spo2"], pv["temp"])).fetchone():
                continue
            conn.execute(
                "INSERT INTO vitals (patient_id,recorded_at,hr,bp,spo2,temp,origin,extra_json) "
                "VALUES (?,?,?,?,?,?,'import',?)",
                (pk, pv["recorded_at"], pv["hr"], pv["bp"], pv["spo2"], pv["temp"], pv["extra_json"]))
            counts["vitals"] += 1

    # ---- top-level vitals referencing a patient
    for i, v in enumerate(as_list(data.get("vitals"))):
        ref = clean(v.get("patient_id"))
        pks = pids_by_source.get(ref) or [r[0] for r in conn.execute("SELECT id FROM patients WHERE source_id=?", (ref,))]
        if not pks:
            raise ImportError_(f"Vitals record #{i + 1} references unknown patient_id {ref!r}")
        if len(pks) > 1:
            raise ImportError_(f"Vitals record #{i + 1} references patient_id {ref!r}, which is ambiguous (duplicate id)")
        pv = parse_vitals({k: x for k, x in v.items() if k != "patient_id"})
        conn.execute(
            "INSERT INTO vitals (patient_id,recorded_at,hr,bp,spo2,temp,origin,extra_json) "
            "VALUES (?,?,?,?,?,?,'import',?)",
            (pks[0], pv["recorded_at"], pv["hr"], pv["bp"], pv["spo2"], pv["temp"], pv["extra_json"]))
        counts["vitals"] += 1

    if duplicate_ids:
        counts["duplicate_patient_ids"] = duplicate_ids
    if not_in_master:
        counts["room_not_in_master"] = not_in_master
    if not_assigned:
        counts["not_assigned_in_room"] = not_assigned

    summary = json.dumps(counts)
    conn.execute("INSERT INTO imports (ts,actor,mode,sha256,raw_json,summary) VALUES (?,?,?,?,?,?)",
                 (ts, actor, mode, sha, raw_text, summary))
    audit(conn, actor, "import", "snapshot", sha[:12], {"mode": mode, "counts": counts})
    return counts
