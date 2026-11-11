"""Import rules, loaded from import_rules.yaml on every check.

enabled: true holds a chart. enabled: false does not. A new rule is a new
block in that file. Built-in ids use the checks in the app. A new id needs
flag, why, and check.
"""
import os
import re
from pathlib import Path

import yaml

BASE = Path(__file__).parent
CHECK_FIELDS = {
    "name", "status", "bed", "chief_complaint", "notes", "triage_level",
    "age", "sex", "hr", "bp", "spo2", "temp", "source_id", "rank",
}
CHECK_OPS = {"contains", "equals", "missing", "present", "gt", "lt", "in"}


def config_path():
    return Path(os.environ.get("ER_RULES", BASE / "import_rules.yaml"))


def load_rules():
    """Rules from the yaml file. Read every time so an edit is picked up immediately."""
    path = config_path()
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raise ValueError(f"Import rules file is missing: {path}")
    except yaml.YAMLError as e:
        raise ValueError(f"Import rules file is not valid YAML: {e}")
    raw = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a 'rules:' list.")
    rules = []
    seen = set()
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            raise ValueError(f"Rule {i} in {path.name} needs an id and a name.")
        rid = str(item["id"]).strip()
        if rid in seen:
            raise ValueError(f"Rule id {rid} is listed more than once in {path.name}.")
        seen.add(rid)
        rule = {
            "id": rid,
            "enabled": bool(item.get("enabled", True)),
            "name": str(item["name"]),
            "catches": str(item.get("catches") or ""),
        }
        if item.get("flag"):
            rule["flag"] = str(item["flag"])
        if item.get("why"):
            rule["why"] = str(item["why"])
        if item.get("check"):
            rule["check"] = item["check"]
        rules.append(rule)
    return rules


def modes():
    """rule id -> 'apply' or 'ignore', from enabled in the yaml."""
    return {r["id"]: "apply" if r["enabled"] else "ignore" for r in load_rules()}


def save_modes(modes_map):
    """Write enabled true/false for known ids. The rest of the file is left as written."""
    path = config_path()
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    current = None
    seen = set()
    id_re = re.compile(r"^(\s*)-\s+id:\s*([A-Za-z0-9_-]+)\s*$")
    en_re = re.compile(r"^(\s*)enabled:\s*\S+\s*$")
    for i, line in enumerate(lines):
        mid = id_re.match(line.rstrip("\n"))
        if mid:
            current = mid.group(2)
            continue
        men = en_re.match(line.rstrip("\n"))
        if current and men and current in modes_map:
            val = "true" if modes_map[current] == "apply" else "false"
            lines[i] = f"{men.group(1)}enabled: {val}\n"
            seen.add(current)
            current = None
    missing = [rid for rid in modes_map if rid not in seen]
    if missing:
        raise ValueError("No enabled line for: " + ", ".join(missing))
    path.write_text("".join(lines))


def custom_flags(patient):
    """Flags from rules that define their own check in the yaml."""
    flags = []
    for rule in load_rules():
        if not rule.get("check"):
            continue
        if _matches(patient, rule["check"]):
            flags.append(_format_flag(rule.get("flag") or rule["name"], patient))
    return flags


def explain_flag(flag):
    """Rule id, name, and what a verifier should do. Unknown flags have id None."""
    f = flag or ""
    for rule in load_rules():
        template = rule.get("flag") or ""
        if not template:
            continue
        prefix = template.split("{", 1)[0]
        if f == template or ("{" in template and prefix.strip() and f.startswith(prefix)):
            return {"id": rule["id"], "rule": rule["name"],
                    "why": rule.get("why") or rule.get("catches") or ""}
    if f.startswith("DUPLICATE ID"):
        return _info("duplicate_id",
                     "This source id is already on another active chart. Both rows were kept. "
                     "Correct Patient ID on this form, or remove the extra chart.")
    if f == "NO BED ASSIGNED":
        return _info("no_bed",
                     "This chart has no bed and is not discharged. Choose a room under Bed / room.")
    if f.startswith("BED SHARED"):
        return _info("bed_shared",
                     "More than one active patient is on this bed. A room whose type contains "
                     "Overflow may hold several people; this room is not one of those. "
                     "Move this patient to the room they occupy, or correct the other chart.")
    if "IN PEDIATRIC ROOM" in f:
        return _info("pediatric_room",
                     "Age is 18 or older and the room type is pediatric. 18 is the line this app uses. "
                     "Correct the age, or move the patient out of the pediatric room.")
    if f == "AGE MISSING" or f.startswith("AGE "):
        why = ("Age was empty in the import. Enter a whole number from 0 to 130." if f == "AGE MISSING"
               else "Age must be a whole number from 0 to 130. The imported value is kept as written. "
                    "Enter a whole number in that range.")
        return _info("age", why)
    if f.startswith("TRIAGE "):
        return _info("triage_value",
                     "A triage level is present and is not 1, 2, 3, 4, or 5. "
                     "Enter one of those levels. An empty triage level is not this rule.")
    if f.startswith("STATUS WAITING FOR BED"):
        return _info("waiting_for_bed",
                     "The status text says waiting for a bed, and a bed is already set. "
                     "Change the status or clear the bed so the two fields agree.")
    if f.startswith("ARRIVAL "):
        return _info("arrival_time",
                     "Arrival is stored exactly as imported and is shown under Source fields. "
                     "It is not an ISO-8601 time (for example 2026-07-10T07:09:00Z). "
                     "This form does not change arrival.")
    if f == "VITALS TIME BEFORE ARRIVAL":
        return _info("vitals_time",
                     "The latest vitals time is earlier than arrival. Both times are shown below. "
                     "A new vitals reading saved here is stamped with the current time and becomes "
                     "the reading the board uses. Fill HR, BP, SpO2, and temp together.")
    if f == "VITALS TIME MISSING":
        return _info("vitals_time",
                     "The latest vitals have no recorded time. A new vitals reading saved here is "
                     "stamped with the current time. Fill HR, BP, SpO2, and temp together.")
    if f == "NO VITALS":
        return _info("vitals_values",
                     "This chart has no vitals row. Enter HR, BP, SpO2, and temp. "
                     "Saving writes that reading with the current time.")
    for label, window in (("HR", "20–250"), ("SpO2", "50–100"), ("TEMP", "90–110 °F")):
        if f.startswith(label + " "):
            if f.endswith("MISSING"):
                why = f"{label} is empty on the latest reading. Enter it with the other vitals. A new reading replaces the one the board uses."
            elif "IMPLAUSIBLE" in f:
                why = f"{label} is outside the window this app uses ({window}). The imported value is kept. Enter a reading inside that window with the other vitals."
            else:
                why = f"{label} is not a number. The imported value is kept. Enter a number with the other vitals."
            return _info("vitals_values", why)
    if f == "BP MISSING":
        return _info("vitals_values",
                     "Blood pressure is empty on the latest reading. Enter it as systolic/diastolic, "
                     "for example 120/80, together with the other vitals.")
    if f.startswith("BP "):
        return _info("vitals_values",
                     "Blood pressure must look like systolic/diastolic, with systolic from 50 to 260 "
                     "and diastolic lower than systolic. The imported value is kept. "
                     "Enter a corrected reading with the other vitals.")
    if f.startswith("ROOM ") and "NOT IN MASTER" in f:
        return _info("room_not_in_master",
                     "This bed is not a room in the room list. Choose a room under Bed / room. "
                     "Saving a listed room writes this patient into that room's record.")
    if f.startswith("NOT ASSIGNED IN ROOM"):
        return _info("not_assigned_in_room",
                     "The room record's occupied_by is not this patient's id. "
                     "Saving writes this patient's id into that room. "
                     "If the Patient ID itself is wrong, correct it here first. The room is updated to the id you save.")
    return {"id": None, "rule": "Data check",
            "why": "This chart is held for the flag shown. Correct the field it names and save."}


def rule_for_flag(flag):
    info = explain_flag(flag)
    return {"rule": info["rule"], "why": info["why"]}


def flag_rule_id(flag):
    return explain_flag(flag)["id"]


def _info(rule_id, why):
    match = next((r for r in load_rules() if r["id"] == rule_id), None)
    return {"id": rule_id, "rule": match["name"] if match else rule_id, "why": why}


def _format_flag(template, patient):
    def repl(match):
        return str(patient.get(match.group(1), ""))
    return re.sub(r"\{([A-Za-z0-9_]+)\}", repl, template)


def _same(raw, opt):
    if str(raw).strip().lower() == str(opt).strip().lower():
        return True
    try:
        return float(raw) == float(opt)
    except (TypeError, ValueError):
        return False


def _matches(patient, check):
    if not isinstance(check, dict):
        return False
    if "all" in check:
        parts = check["all"]
        return isinstance(parts, list) and bool(parts) and all(_matches(patient, part) for part in parts)
    field = check.get("field")
    op = check.get("op")
    if field not in CHECK_FIELDS or op not in CHECK_OPS:
        return False
    raw = patient.get(field)
    if op == "missing":
        return raw is None or str(raw).strip() == ""
    if op == "present":
        return raw is not None and str(raw).strip() != ""
    value = check.get("value")
    if op == "contains":
        return value is not None and str(value).lower() in str(raw or "").lower()
    if op == "equals":
        return str(raw or "").strip().lower() == str(value or "").strip().lower()
    if op == "in":
        return isinstance(value, list) and any(_same(raw, opt) for opt in value)
    try:
        num = float(raw)
        bound = float(value)
    except (TypeError, ValueError):
        return False
    return num > bound if op == "gt" else num < bound
