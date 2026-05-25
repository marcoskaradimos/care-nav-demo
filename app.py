import os
import json
import re
from datetime import datetime, timedelta
from collections import defaultdict
from html.parser import HTMLParser
from flask import Flask, render_template, request, redirect, url_for, session, Response, stream_with_context, jsonify, g, abort
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from google.cloud.sql.connector import Connector
import sqlalchemy
from sqlalchemy import text

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import demo_flow as df

app = Flask(__name__)

# Jinja2 custom filters
app.jinja_env.filters['split']    = lambda s, sep=' ': s.split(sep)
app.jinja_env.filters['fromjson'] = lambda s: json.loads(s) if isinstance(s, str) else s

# ── Secret Loading ────────────────────────────────────────────────────────────
def _get_secret(secret_id, fallback_env=None):
    """Load from Secret Manager, fall back to env/dotenv for local dev."""
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "navdemo-494307")
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception:
        if fallback_env:
            return os.environ.get(fallback_env, "")
        return ""

DB_PASSWORD               = _get_secret("DB_PASSWORD",               "DB_PASSWORD")
DB_INSTANCE_CONNECTION_NAME = _get_secret("DB_INSTANCE_CONNECTION_NAME", "DB_INSTANCE_CONNECTION_NAME")
DB_NAME                   = os.environ.get("DB_NAME", "carenav")
DB_USER                   = os.environ.get("DB_USER", "carenav_app")
GOOGLE_API_KEY            = _get_secret("GOOGLE_API_KEY",            "GOOGLE_API_KEY")
GOOGLE_PLACES_API_KEY     = _get_secret("GOOGLE_PLACES_API_KEY",     "GOOGLE_PLACES_API_KEY")
_secret_key               = _get_secret("SECRET_KEY",                "SECRET_KEY")

MODEL = "gemini-flash-latest"

# ── Database ──────────────────────────────────────────────────────────────────
_connector = None
_engine    = None

def _get_connector():
    global _connector
    if _connector is None:
        _connector = Connector()
    return _connector

def _getconn():
    return _get_connector().connect(
        DB_INSTANCE_CONNECTION_NAME,
        "pg8000",
        user=DB_USER,
        password=DB_PASSWORD,
        db=DB_NAME,
    )

def get_engine():
    global _engine
    if _engine is None:
        if DB_INSTANCE_CONNECTION_NAME:
            _engine = sqlalchemy.create_engine(
                "postgresql+pg8000://",
                creator=_getconn,
                pool_size=2,
                max_overflow=2,
                pool_timeout=30,
                pool_recycle=1800,
            )
        else:
            _engine = sqlalchemy.create_engine(
                os.environ.get("DATABASE_URL", "postgresql+pg8000://carenav_app:@localhost/carenav")
            )
    return _engine

def get_db():
    return get_engine().connect()

# ── Avatar Colors ─────────────────────────────────────────────────────────────
AVATAR_COLORS = [
    '#e74c3c', '#e67e22', '#f1c40f', '#2ecc71',
    '#1abc9c', '#3498db', '#9b59b6', '#e91e63'
]

def get_avatar_color(name):
    if not name:
        return AVATAR_COLORS[0]
    return AVATAR_COLORS[abs(hash(name)) % len(AVATAR_COLORS)]

# ── init_db ───────────────────────────────────────────────────────────────────
def init_db():
    with get_engine().begin() as db:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS tickets (
                id SERIAL PRIMARY KEY,
                case_number TEXT,
                patient_name TEXT,
                nhs_number TEXT,
                dob TEXT,
                phone TEXT,
                postcode TEXT,
                gp_practice TEXT DEFAULT '',
                title TEXT,
                category TEXT,
                inbox TEXT DEFAULT 'Unassigned',
                form_data TEXT,
                status TEXT DEFAULT 'Open',
                priority TEXT DEFAULT 'Medium',
                assigned_to TEXT DEFAULT '',
                closed_by TEXT DEFAULT '',
                is_read INTEGER DEFAULT 0,
                match_status TEXT DEFAULT 'unverified',
                created_at TEXT,
                updated_at TEXT,
                closed_at TEXT
            )
        """))
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS notes (
                id SERIAL PRIMARY KEY,
                ticket_id INTEGER,
                author TEXT,
                content TEXT,
                created_at TEXT
            )
        """))
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS registered_patients (
                id SERIAL PRIMARY KEY,
                patient_ref TEXT,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                dob TEXT,
                phone TEXT,
                nhs_number TEXT,
                postcode TEXT,
                gp_practice TEXT DEFAULT '',
                registered_at TEXT
            )
        """))
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS staff_users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                role TEXT DEFAULT 'staff',
                created_at TEXT
            )
        """))
    _seed_staff_users()
    _seed_patients()

# ── Patient Seeding ───────────────────────────────────────────────────────────
def _seed_patients():
    """Seed all patients from patients.json into registered_patients if table is empty."""
    try:
        with get_engine().begin() as db:
            count = db.execute(text("SELECT COUNT(*) FROM registered_patients")).fetchone()[0]
            if count > 0:
                return
            patients = load_patients()
            now = datetime.utcnow().isoformat()
            for p in patients:
                db.execute(text("""
                    INSERT INTO registered_patients
                        (patient_ref, first_name, last_name, dob, phone, nhs_number, postcode, gp_practice, registered_at)
                    VALUES (:ref, :fn, :ln, :dob, :phone, :nhs, :pc, :gp, :now)
                """), {
                    "ref":   p.get("id", ""),
                    "fn":    p["first_name"],
                    "ln":    p["last_name"],
                    "dob":   p.get("date_of_birth", ""),
                    "phone": p.get("telephone", ""),
                    "nhs":   p.get("nhs_number", ""),
                    "pc":    p.get("postcode", ""),
                    "gp":    p.get("gp_practice", ""),
                    "now":   now,
                })
            app.logger.info(f"Seeded {len(patients)} patients into registered_patients")
    except Exception as e:
        app.logger.error(f"Patient seeding failed: {e}")

# ── Staff User Seeding ────────────────────────────────────────────────────────
_DEFAULT_STAFF = [
    ("admin",    "admin123",    "Administrator", "admin"),
    ("marcos",   "marcos123",   "Marcos",        "staff"),
    ("drjones",  "jones123",    "Dr Jones",      "staff"),
    ("flemming", "flemming123", "Flemming",      "staff"),
]

def _seed_staff_users():
    try:
        with get_engine().begin() as db:
            count = db.execute(text("SELECT COUNT(*) FROM staff_users")).fetchone()[0]
            if count == 0:
                now = datetime.utcnow().isoformat()
                for username, password, display_name, role in _DEFAULT_STAFF:
                    db.execute(text("""
                        INSERT INTO staff_users (username, password_hash, display_name, role, created_at)
                        VALUES (:username, :hash, :display_name, :role, :now)
                        ON CONFLICT (username) DO NOTHING
                    """), {
                        "username":     username,
                        "hash":         generate_password_hash(password),
                        "display_name": display_name,
                        "role":         role,
                        "now":          now,
                    })
    except Exception as e:
        app.logger.error(f"Staff seeding failed: {e}")

def get_staff_usernames():
    try:
        db = get_db()
        rows = db.execute(text("SELECT username FROM staff_users ORDER BY username")).fetchall()
        db.close()
        return [r[0] for r in rows]
    except Exception:
        return [u for u, *_ in _DEFAULT_STAFF]

# ── Helpers ───────────────────────────────────────────────────────────────────
def infer_category(node_id):
    mapping = {
        "synthetic_blood_test_patient": "Blood Test",
        "synthetic_patient_details_customFormNode-1764448405115": "Medicine Query",
        "synthetic_patient_details_customFormNode-1764440727948": "General Symptoms",
        "synthetic_patient_details_cert_request_form": "Medical Certificate",
        "synthetic_patient_details_general_enquiry_form": "General Enquiry",
    }
    if node_id in mapping:
        return mapping[node_id]
    if "blood" in node_id:
        return "Blood Test"
    if "cert" in node_id:
        return "Medical Certificate"
    if "enquiry" in node_id:
        return "General Enquiry"
    return "General Enquiry"

def generate_case_number():
    date_part = datetime.utcnow().strftime("%Y%m%d")
    db = get_db()
    count = db.execute(
        text("SELECT COUNT(*) FROM tickets WHERE case_number LIKE :pat"),
        {"pat": f"LN-{date_part}-%"}
    ).fetchone()[0]
    db.close()
    return f"LN-{date_part}-{count + 1:04d}"

def inbox_for_category(category):
    mapping = {
        "Appointments":         "Unassigned",
        "Nurse Appointment":    "Nurse Appointment",
        "Doctor Appointment":   "Clinical Triage",
        "Repeat Medicine":      "Pharmacist",
        "Medical Certificate":  "Medical Certificate",
        "Admin Query":          "Admin",
        "Symptom Query":        "Unassigned",
        "Blood Test":           "Unassigned",
        "General Enquiry":      "Unassigned",
        "Medicine Query":       "Pharmacist",
    }
    return mapping.get(category, "Unassigned")

def time_ago(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
        diff = datetime.utcnow() - dt
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        elif seconds < 3600:
            return f"{seconds // 60}m ago"
        elif seconds < 86400:
            return f"{seconds // 3600}h ago"
        else:
            return f"{seconds // 86400}d ago"
    except Exception:
        return ""

app.secret_key = _secret_key or "dev-secret-key-change-in-production"

# ── Patient List ──────────────────────────────────────────────────────────────
def load_patients():
    path = os.path.join(os.path.dirname(__file__), "data", "patients.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _norm_dob(dob):
    if not dob:
        return ""
    dob = dob.strip()
    if "-" in dob:
        parts = dob.split("-")
        if len(parts) == 3:
            return f"{parts[2]}/{parts[1]}/{parts[0]}"
    return dob

def _fuzzy(a, b):
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def _score_candidate(fn, ln, dob_norm, pc, nhs, pfn, pln, pdob, ppc, pnhs):
    score = 0.0
    if nhs and pnhs and nhs == pnhs:
        score += 5.0
    if dob_norm and pdob and dob_norm == pdob:
        score += 3.0
    fn_sim = _fuzzy(fn, pfn)
    if fn_sim == 1.0:   score += 3.0
    elif fn_sim >= 0.8: score += 2.0
    elif fn_sim >= 0.6: score += 1.0
    ln_sim = _fuzzy(ln, pln)
    if ln_sim == 1.0:   score += 3.0
    elif ln_sim >= 0.8: score += 2.0
    elif ln_sim >= 0.6: score += 1.0
    if pc and ppc:
        pc_sim = _fuzzy(pc, ppc)
        if pc_sim == 1.0:      score += 2.0
        elif pc_sim >= 0.75:   score += 1.0
        elif pc[:4] == ppc[:4]: score += 0.5
    return score

def match_patient(first_name, last_name, dob, postcode, nhs_number=""):
    """Match against patients.json only (the authoritative source).
    Returns (best_match, confidence, all_strong_candidates).
    all_strong_candidates is populated when multiple records score >= 7.0."""
    fn       = first_name.strip().lower()
    ln       = last_name.strip().lower()
    dob_norm = _norm_dob(dob)
    pc       = postcode.strip().upper().replace(" ", "")
    nhs      = nhs_number.strip().replace(" ", "")
    best, best_score = None, 0.0
    strong_candidates = []  # all records scoring >= 7.0

    for p in load_patients():
        pfn  = p["first_name"].strip().lower()
        pln  = p["last_name"].strip().lower()
        pdob = p["date_of_birth"].strip()
        ppc  = p["postcode"].strip().upper().replace(" ", "")
        pnhs = p["nhs_number"].strip().replace(" ", "")
        score = _score_candidate(fn, ln, dob_norm, pc, nhs, pfn, pln, pdob, ppc, pnhs)
        if score >= 7.0:
            strong_candidates.append((score, p))
        if score > best_score:
            best_score, best = score, p

    # Sort strong candidates best-first
    strong_candidates.sort(key=lambda x: x[0], reverse=True)

    if best_score >= 7.0:
        if len(strong_candidates) > 1:
            return best, "ambiguous", [p for _, p in strong_candidates]
        return best, "matched", []
    elif best_score >= 3.5:
        return best, "partial", []
    return None, None

# ── HTML Stripper ─────────────────────────────────────────────────────────────
class _MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fed = []
    def handle_data(self, d):
        self.fed.append(d)
    def get_data(self):
        return " ".join(self.fed).strip()

def strip_html(s):
    s = re.sub(r"&nbsp;", " ", str(s))
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</div>", "\n", s, flags=re.IGNORECASE)
    stripper = _MLStripper()
    stripper.feed(s)
    return re.sub(r"\n{3,}", "\n\n", stripper.get_data()).strip()

# ── Load Flow Graph ───────────────────────────────────────────────────────────
def load_flow():
    flow_path = os.path.join(os.path.dirname(__file__), "Flow.json")
    with open(flow_path, encoding="utf-8") as f:
        raw = json.load(f)

    flow = raw[0] if isinstance(raw, list) else raw
    fd = flow["flow_data"]
    raw_nodes = fd["nodes"]
    edges = fd["edges"]

    adj = defaultdict(list)
    for e in edges:
        adj[e["source"]].append({
            "target": e["target"],
            "handle": e.get("sourceHandle", "")
        })

    nodes = {}
    for n in raw_nodes:
        nid = n["id"]
        d = n.get("data", {})
        entry = {
            "type": n["type"],
            "label": d.get("label", ""),
            "message": strip_html(d.get("message", "")),
            "options": [],
            "next": adj.get(nid, []),
            "fields": [],
        }
        for o in d.get("options", []):
            entry["options"].append({
                "id": o.get("id", ""),
                "label": o.get("label", o.get("text", "")).strip(),
            })
        if "schedules" in d:
            entry["schedules"] = d["schedules"]
        if "systemPrompt" in d:
            entry["systemPrompt"] = d["systemPrompt"]
        if "auto_trigger" in d:
            entry["auto_trigger"] = d["auto_trigger"]
        entry["fields"] = []
        for f in d.get("fields", []):
            entry["fields"].append({
                "id": f.get("id", ""),
                "label": f.get("label", ""),
                "type": f.get("type", "text"),
                "required": f.get("required", False),
                "placeholder": f.get("placeholder", ""),
                "options": f.get("options", []),
            })
        nodes[nid] = entry

    BLOOD_TEST_FORM    = "synthetic_blood_test_form"
    BLOOD_TEST_PATIENT = "synthetic_blood_test_patient"
    blood_confirmation_next = adj.get("customFormNode-1764442624724", [])

    nodes[BLOOD_TEST_FORM] = {
        "type": "customFormNode",
        "label": "Blood Test Request",
        "message": "Please tell us about the blood test you need.",
        "options": [],
        "next": [{"target": BLOOD_TEST_PATIENT, "handle": ""}],
        "fields": [
            {"id": "blood_test_type", "label": "Type of Blood Test (select all that apply)", "type": "multi_select", "required": True,
             "options": ["Full Blood Count (FBC)", "HbA1c (Diabetes)", "Cholesterol / Lipid Panel",
                         "Thyroid Function (TFT)", "Liver Function (LFT)", "Kidney Function (U&E)",
                         "Iron Studies / Ferritin", "Vitamin B12 / Folate", "Vitamin D",
                         "Inflammatory Markers (CRP / ESR)", "Other"]},
            {"id": "other_blood_test", "label": "If other, please specify", "type": "short_text", "required": False},
            {"id": "reason",          "label": "Reason for blood test", "type": "long_text",   "required": True},
            {"id": "gp_requested",    "label": "Who requested this test?", "type": "select",   "required": True,
             "options": ["Please select", "GP", "Other"]},
        ],
    }
    adj[BLOOD_TEST_FORM] = [{"target": BLOOD_TEST_PATIENT, "handle": ""}]

    nodes[BLOOD_TEST_PATIENT] = {
        "type": "customFormNode",
        "label": "Enter Patient Information",
        "message": "Almost done! Please provide your details so the practice team can get back to you.",
        "options": [],
        "next": blood_confirmation_next,
        "fields": [
            {"id": "title",      "label": "Title",         "type": "select",     "required": False, "options": ["Select title", "Mr", "Mrs", "Miss", "Ms", "Dr", "Prof"]},
            {"id": "first_name", "label": "First Name",    "type": "short_text", "required": True},
            {"id": "last_name",  "label": "Last Name",     "type": "short_text", "required": True},
            {"id": "nhs_number", "label": "NHS Number",    "type": "short_text", "required": True,  "placeholder": "Enter 10-digit NHS number"},
            {"id": "phone",      "label": "Phone",         "type": "short_text", "required": False, "placeholder": "Enter phone number"},
            {"id": "postcode",   "label": "Postcode",      "type": "short_text", "required": False, "placeholder": "Enter postcode"},
            {"id": "dob",        "label": "Date of Birth", "type": "date",       "required": True},
        ],
    }
    adj[BLOOD_TEST_PATIENT] = blood_confirmation_next

    if "messageNode-1764442597203" in nodes:
        adj["messageNode-1764442597203"] = [{"target": BLOOD_TEST_FORM, "handle": ""}]
        nodes["messageNode-1764442597203"]["next"] = adj["messageNode-1764442597203"]

    if "welcome_message" in nodes:
        nodes["welcome_message"]["message"] = (
            "Hello! Welcome to Access Care Navigation, I'm your assistant, here to help you "
            "find the right information or service. How can I assist you today?"
        )

    reg_node = nodes.get("patient_registration_options")
    if reg_node:
        for opt in reg_node["options"]:
            if opt["label"].strip().lower() == "register online":
                opt["url"] = "https://www.nhs.uk/nhs-services/gps/how-to-register-with-a-gp-surgery/"

    dental_node = nodes.get("optionsNode-1768503338735")
    if dental_node:
        for opt in dental_node["options"]:
            if opt["label"].strip().lower() == "dental emergency":
                opt["url"] = "https://www.nhs.uk/nhs-services/dentists/how-to-find-an-nhs-dentist-in-an-emergency/"
            if opt["label"].strip().lower() == "find a dentist":
                opt["url"] = "https://www.nhs.uk/service-search/find-a-dentist/"

    PATIENT_DETAILS_FIELDS = [
        {"id": "title",      "label": "Title",          "type": "select",     "required": False, "options": ["Select title", "Mr", "Mrs", "Miss", "Ms", "Dr", "Prof"]},
        {"id": "first_name", "label": "First Name",     "type": "short_text", "required": True},
        {"id": "last_name",  "label": "Last Name",      "type": "short_text", "required": True},
        {"id": "nhs_number", "label": "NHS Number",     "type": "short_text", "required": True,  "placeholder": "Enter 10-digit NHS number", "maxlength": 10, "inputmode": "numeric"},
        {"id": "phone",      "label": "Phone",          "type": "short_text", "required": False, "placeholder": "Enter phone number"},
        {"id": "postcode",   "label": "Postcode",       "type": "short_text", "required": False, "placeholder": "Enter postcode"},
        {"id": "dob",        "label": "Date of Birth",  "type": "date",       "required": True},
    ]

    med_form = nodes.get("customFormNode-1764448405115")
    if med_form:
        med_form["form_title"] = "Please complete this form"

    gen_enquiry = nodes.get("general_enquiry_form")
    if gen_enquiry:
        gen_enquiry["fields"] = [
            {"id": "enquiry_details", "label": "Your Enquiry Details", "type": "long_text", "required": True},
        ]

    FORMS_NEEDING_PATIENT_DETAILS = {
        "customFormNode-1764440727948": PATIENT_DETAILS_FIELDS,
        "cert_request_form":            PATIENT_DETAILS_FIELDS,
        "general_enquiry_form":         PATIENT_DETAILS_FIELDS,
        "customFormNode-1764448405115": PATIENT_DETAILS_FIELDS,
    }

    for form_id, fields in FORMS_NEEDING_PATIENT_DETAILS.items():
        if form_id not in nodes:
            continue
        synthetic_id = f"synthetic_patient_details_{form_id}"
        original_next = adj.get(form_id, [])
        nodes[synthetic_id] = {
            "type": "customFormNode",
            "label": "Enter Patient Information",
            "message": "Almost done! Please provide your details so the practice team can get back to you.",
            "options": [],
            "next": original_next,
            "fields": fields,
        }
        adj[synthetic_id] = original_next
        adj[form_id] = [{"target": synthetic_id, "handle": ""}]
        nodes[form_id]["next"] = adj[form_id]

    return nodes, adj

FLOW_NODES, FLOW_ADJ = load_flow()
init_db()

# ── Flow Engine ───────────────────────────────────────────────────────────────
def get_node(node_id):
    return FLOW_NODES.get(node_id)

def follow_edge(node_id, handle=None):
    edges = FLOW_ADJ.get(node_id, [])
    if not edges:
        return None
    if handle:
        for e in edges:
            if e["handle"] == handle:
                return e["target"]
    return edges[0]["target"]

def skip_passthrough_nodes(node_id):
    visited = set()
    while node_id and node_id not in visited:
        node = get_node(node_id)
        if not node:
            break
        if node["type"] in ("waitNode", "startNode", "scheduleNode"):
            visited.add(node_id)
            node_id = follow_edge(node_id)
        else:
            break
    return node_id

def resolve_node(node_id):
    return skip_passthrough_nodes(node_id)

def build_node_response(node_id):
    node_id = resolve_node(node_id)
    if not node_id:
        return None
    node = get_node(node_id)
    if not node:
        return None

    messages = []
    current_id = node_id
    current = node

    while current and current["type"] == "messageNode":
        if current["message"]:
            messages.append(current["message"])
        nexts = current["next"]
        if not nexts:
            break
        next_id = resolve_node(nexts[0]["target"])
        next_node = get_node(next_id) if next_id else None
        if not next_node or next_node["type"] in ("optionsNode", "buttonNode", "customFormNode",
                                                    "questionNode", "endNode", "knowledgeBaseNode",
                                                    "createTicketNode"):
            current_id = next_id
            current = next_node
            break
        current_id = next_id
        current = next_node

    combined_message = "\n\n".join(m for m in messages if m)

    if not current:
        return {"node_id": node_id, "type": "end", "message": combined_message, "options": [], "fields": [], "is_end": True}

    ctype = current["type"] if current else "end"

    if ctype == "endNode":
        node_msg = current.get("message", "")
        msg = combined_message + ("\n\n" + node_msg if node_msg else "") if combined_message else node_msg
        return {"node_id": current_id, "type": "end", "message": msg, "options": [], "fields": [], "is_end": True}

    if ctype == "optionsNode":
        msg = current["message"] or ""
        if combined_message:
            msg = combined_message + ("\n\n" + msg if msg else "")
        options = current["options"]
        if current_id == "main_menu_options":
            options = [o for o in options if o["id"] == "opt_appointments"]
        if current_id == "optionsNode-1764439981132":
            options = [o for o in options if o["id"] == "opt-1-1764439981132"]
        return {"node_id": current_id, "type": "options", "message": msg, "options": options, "fields": [], "is_end": False}

    if ctype == "buttonNode":
        msg = current["message"] or ""
        if combined_message:
            msg = combined_message + ("\n\n" + msg if msg else "")
        return {"node_id": current_id, "type": "options", "message": msg, "options": current["options"], "fields": [], "is_end": False}

    if ctype == "customFormNode":
        node_message = current.get("message", "")
        msg = combined_message or node_message or "Please fill in the form below:"
        return {"node_id": current_id, "type": "form", "message": msg, "form_title": current.get("form_title", ""), "options": [], "fields": current["fields"], "is_end": False}

    if ctype == "apptSymptomNode":
        msg = current["message"] or ""
        if combined_message:
            msg = combined_message + ("\n\n" + msg if msg else "")
        return {"node_id": current_id, "type": "appt_symptom", "message": msg, "options": current.get("options", []), "fields": current.get("fields", []), "is_end": False}

    if ctype == "questionNode":
        msg = current["message"] or ""
        if combined_message:
            msg = combined_message + ("\n\n" + msg if msg else "")
        return {"node_id": current_id, "type": "question", "message": msg, "options": [], "fields": [], "is_end": False}

    if ctype == "knowledgeBaseNode":
        return {"node_id": current_id, "type": "ai", "message": combined_message or current.get("label", ""), "options": [], "fields": [], "is_end": False, "auto_trigger": current.get("auto_trigger", False)}

    if ctype == "createTicketNode":
        next_id = resolve_node(follow_edge(current_id))
        if next_id:
            sub = build_node_response(next_id)
            if sub:
                sub["message"] = combined_message + ("\n\n" + sub["message"] if sub["message"] else "")
                return sub
        return {"node_id": current_id, "type": "end", "message": combined_message or "Your request has been submitted.", "options": [], "fields": [], "is_end": True}

    if ctype in ("scheduleNode", "routingNode", "logicNode"):
        nexts = current["next"]
        if nexts:
            return build_node_response(nexts[0]["target"])

    next_id = resolve_node(follow_edge(current_id))
    if next_id and next_id != current_id:
        return build_node_response(next_id)

    return {"node_id": current_id, "type": "end", "message": combined_message or current.get("message", ""), "options": [], "fields": [], "is_end": True}


# ── Auth ──────────────────────────────────────────────────────────────────────
def staff_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("staff_authenticated"):
            return redirect(url_for("staff_login", next=request.url))
        return f(*args, **kwargs)
    return decorated


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if session.get("staff_authenticated"):
        return redirect(url_for("inbox"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            db  = get_db()
            row = db.execute(
                text("SELECT password_hash FROM staff_users WHERE username = :u"),
                {"u": username}
            ).fetchone()
            db.close()
            if row and check_password_hash(row[0], password):
                session["staff_authenticated"] = True
                session["staff_username"] = username
                return redirect(request.args.get("next") or url_for("inbox"))
        except Exception as e:
            app.logger.error(f"Login error: {e}")
        error = "Invalid username or password."
    return render_template("staff_login.html", error=error)

@app.route("/staff/logout")
def staff_logout():
    session.pop("staff_authenticated", None)
    session.pop("staff_username", None)
    return redirect(url_for("staff_login"))

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/staff/triage", methods=["GET", "POST"])
@staff_required
def staff_triage():
    if request.method == "POST":
        first_name  = request.form.get("first_name", "").strip()
        last_name   = request.form.get("last_name",  "").strip()
        dob         = request.form.get("dob", "").strip()
        phone       = request.form.get("phone", "").strip()
        nhs_number  = request.form.get("nhs_number", "").strip()
        postcode    = request.form.get("postcode", "").strip()
        gp_practice = request.form.get("gp_practice", "").strip()
        patient_id  = request.form.get("patient_id", "").strip()
        is_proxy    = request.form.get("is_proxy", "0") == "1"

        # Set patient session — same keys as /patient/match
        session["patient_first_name"] = first_name
        session["patient_last_name"]  = last_name
        session["patient_dob"]        = dob
        session["patient_phone"]      = phone
        session["patient_nhs"]        = nhs_number
        session["patient_postcode"]   = postcode
        session["patient_practice"]   = gp_practice
        session["patient_name"]       = f"{first_name} {last_name}".strip()
        session["matched_patient_id"] = patient_id
        session["triage_by_staff"]    = session.get("staff_username", "")

        if is_proxy:
            session["proxy_first_name"]   = request.form.get("proxy_first_name", "").strip()
            session["proxy_last_name"]    = request.form.get("proxy_last_name",  "").strip()
            session["proxy_relationship"] = request.form.get("relationship", "").strip()
        else:
            session.pop("proxy_first_name",   None)
            session.pop("proxy_last_name",    None)
            session.pop("proxy_relationship", None)

        return redirect(url_for("index") + "?staff_triage=1")

    return render_template("triage.html", nav_counts=get_nav_counts(), teams=TEAMS)

@app.route("/patient/match", methods=["POST"])
def patient_match():
    data        = request.get_json() or {}
    first_name  = data.get("first_name", "")
    last_name   = data.get("last_name", "")
    dob         = data.get("dob", "")
    postcode    = data.get("postcode", "")
    nhs_number  = data.get("nhs_number", "")
    phone       = data.get("phone", "")

    patient, confidence, _similar = match_patient(first_name, last_name, dob, postcode, nhs_number)

    dob_display = dob
    if dob and "-" in dob:
        parts = dob.split("-")
        if len(parts) == 3:
            dob_display = f"{parts[2]}/{parts[1]}/{parts[0]}"

    session["patient_first_name"] = first_name
    session["patient_last_name"]  = last_name
    session["patient_dob"]        = dob_display
    session["patient_phone"]      = phone
    session["patient_postcode"]   = postcode
    session["patient_nhs"]        = nhs_number

    if patient and confidence == "matched":
        session["matched_patient_id"] = patient["id"]
        session["patient_name"]       = f"{patient['first_name']} {patient['last_name']}"
        session["patient_nhs"]        = patient["nhs_number"]
        session["patient_phone"]      = patient.get("telephone") or phone
        session["patient_postcode"]   = patient.get("postcode") or postcode
        session["patient_practice"]   = patient.get("gp_practice") or ""
        return jsonify({"status": "matched", "patient": {
            "name":     f"{patient['first_name']} {patient['last_name']}",
            "nhs":      patient["nhs_number"],
            "dob":      patient["date_of_birth"],
            "phone":    patient.get("telephone") or phone,
            "postcode": patient.get("postcode") or postcode,
            "practice": patient.get("gp_practice") or "",
        }})
    elif patient and confidence == "partial":
        session["matched_patient_id"] = None
        session["patient_name"]       = f"{first_name} {last_name}"
        session["patient_practice"]   = ""
        return jsonify({"status": "partial", "patient": {
            "name":     f"{first_name} {last_name}",
            "nhs":      nhs_number or "Not provided",
            "dob":      dob_display,
            "phone":    phone,
            "postcode": postcode,
            "practice": "Not found — manual check required",
        }})
    else:
        session["matched_patient_id"] = None
        session["patient_name"]       = f"{first_name} {last_name}"
        session["patient_practice"]   = ""
        return jsonify({"status": "unmatched", "patient": {
            "name":     f"{first_name} {last_name}",
            "nhs":      nhs_number or "Not provided",
            "dob":      dob,
            "phone":    phone,
            "postcode": postcode,
            "practice": "Not registered — new patient",
        }})


@app.route("/patient/set_proxy", methods=["POST"])
def patient_set_proxy():
    data = request.get_json() or {}
    session["proxy_first_name"]   = data.get("proxy_first_name", "")
    session["proxy_last_name"]    = data.get("proxy_last_name", "")
    session["proxy_relationship"] = data.get("relationship", "")
    return jsonify({"status": "ok"})


@app.route("/patient/register", methods=["POST"])
def patient_register():
    """Kept for backwards compatibility but does not write to DB.
    The registered_patients table is read-only from the app — seeded from patients.json only."""
    return jsonify({"status": "ok"})


@app.route("/patient/search")
def patient_search():
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 2:
        return jsonify({"results": []})

    results = []
    try:
        db   = get_db()
        rows = db.execute(
            text("""SELECT * FROM registered_patients
                    WHERE lower(first_name) LIKE :q1 OR lower(last_name) LIKE :q2
                       OR lower(first_name || ' ' || last_name) LIKE :q3
                       OR nhs_number LIKE :q4 OR dob LIKE :q5"""),
            {"q1": f"%{q}%", "q2": f"%{q}%", "q3": f"%{q}%", "q4": f"%{q}%", "q5": f"%{q}%"}
        ).mappings().fetchall()
        db.close()
        for r in rows:
            r = dict(r)
            results.append({
                "id": f"reg_{r['id']}", "name": f"{r['first_name']} {r['last_name']}",
                "first_name": r["first_name"], "last_name": r["last_name"],
                "dob": r.get("dob", ""), "phone": r.get("phone", ""),
                "nhs": r.get("nhs_number", "") or "—", "postcode": r.get("postcode", ""),
                "practice": "", "source": "registered",
            })
    except Exception:
        pass

    try:
        patients = load_patients()
        for p in patients:
            full = f"{p['first_name']} {p['last_name']}".lower()
            if (q in full or q in (p.get("nhs_number") or "").lower()
                    or q in (p.get("date_of_birth") or "").lower()):
                results.append({
                    "id": p["id"], "name": f"{p['first_name']} {p['last_name']}",
                    "first_name": p["first_name"], "last_name": p["last_name"],
                    "dob": p.get("date_of_birth", ""), "phone": p.get("telephone", ""),
                    "nhs": p.get("nhs_number", "") or "—", "postcode": p.get("postcode", ""),
                    "practice": p.get("gp_practice", ""), "source": "patients_json",
                })
    except Exception:
        pass

    return jsonify({"results": results[:10]})


@app.route("/flow/node/<node_id>")
def flow_node(node_id):
    node = df.get_node(node_id)
    if not node:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_demo_node_response(node))


@app.route("/flow/start")
def flow_start():
    session.pop("demo_node_id", None)
    session.pop("demo_form_data", None)
    session.pop("demo_symptom", None)
    node = df.get_node("start")
    return jsonify(_demo_node_response(node))

@app.route("/flow/step", methods=["POST"])
def flow_step():
    data          = request.get_json()
    node_id       = data.get("node_id")
    option_id     = data.get("option_handle", "")
    form_data     = data.get("form_data", {})
    all_form_data = data.get("all_form_data", {})
    user_input    = data.get("user_input", "")

    stored = session.get("demo_form_data", {})
    stored.update(form_data)
    stored.update(all_form_data)
    if user_input:
        stored["_last_input"] = user_input
    session["demo_form_data"] = stored

    next_id = None

    if option_id:
        next_id = df.next_node_for_option(node_id, option_id)

    if not next_id and node_id in df.NODES:
        node_def = df.NODES[node_id]
        next_id  = node_def.get("next_id")

    if node_id == "doctor_input":
        symptom = user_input or stored.get("_last_input", "")
        session["demo_symptom"] = symptom
        match = _check_pharmacy_referral(symptom)
        if match:
            node = dict(df.NODES["pharmacy_referral_result"])
            node["message"] = (
                f"Good news — **{match['condition']}** can often be treated by a local pharmacist "
                f"without needing a GP appointment.\n\n"
                f"{match['pharmacy_advice']}\n\n"
                f"[View NHS guidance]({match['nhs_url']})"
            )
            return jsonify(_demo_node_response(node))
        summary = _quick_symptom_summary(symptom)
        return jsonify({
            "node_id": "doctor_ai_summary", "type": "message", "message": summary,
            "options": [{"id": "opt_book_doctor", "label": "Book an appointment"}, {"id": "opt_self_care_dr", "label": "Self-care advice"}],
            "fields": [], "next_id": None,
        })

    if node_id == "symptom_search_input":
        symptom = user_input or stored.get("_last_input", "")
        session["demo_symptom"] = symptom
        summary = _quick_symptom_summary(symptom)
        return jsonify({
            "node_id": "symptom_search_results", "type": "message", "message": summary,
            "options": df.NODES["symptom_after_results"]["options"],
            "fields": [], "next_id": "symptom_after_results",
        })

    if node_id == "doctor_ai_summary":
        if option_id == "opt_book_doctor":
            next_id = "doctor_symptom_form"
        elif option_id == "opt_self_care_dr":
            symptom = session.get("demo_symptom", "your symptoms")
            advice  = _self_care_advice(symptom)
            return jsonify({"node_id": "self_care_result", "type": "end", "message": advice, "symptom": symptom, "options": [], "fields": [], "is_end": True})

    if node_id == "symptom_search_results" and option_id in ("opt_self_care", "opt_self_care_dr"):
        symptom = session.get("demo_symptom", "your symptoms")
        advice  = _self_care_advice(symptom)
        return jsonify({"node_id": "self_care_result", "type": "end", "message": advice, "symptom": symptom, "options": [], "fields": [], "is_end": True})
    if node_id == "symptom_search_results" and option_id == "opt_book_from_symptom":
        next_id = "doctor_symptom_form"

    if next_id == "pharmacy_finder" or option_id == "opt_pharmacy_accept":
        return jsonify({"node_id": "pharmacy_finder", "type": "pharmacy_finder", "message": "Finding your nearest pharmacy...", "options": [], "fields": [], "is_end": True})

    if node_id in df.CONFIRMATION_INBOXES or next_id in df.CONFIRMATION_INBOXES:
        confirm_id = node_id if node_id in df.CONFIRMATION_INBOXES else next_id
        _create_demo_ticket(confirm_id, stored)

    if not next_id:
        return jsonify({"node_id": node_id, "type": "end", "message": "Thank you for using our service.", "options": [], "fields": [], "is_end": True})

    node = df.get_node(next_id)
    if not node:
        return jsonify({"node_id": next_id, "type": "end", "message": "Thank you for using our service.", "options": [], "fields": [], "is_end": True})

    return jsonify(_demo_node_response(node))


def _demo_node_response(node):
    n = dict(node)
    if n.get("type") == "message" and n.get("next_id") and not n.get("options"):
        n["auto_advance"] = True
    return n


def _quick_symptom_summary(symptom):
    try:
        client  = genai.Client(api_key=GOOGLE_API_KEY)
        prompt  = (
            f"Patient symptom: {symptom}\n\n"
            f"List the 3 most likely causes. For each write one line: "
            f"'- [Condition name]: [one sentence explanation]'\n"
            f"Then write: 'See a GP if: [2-3 red flag symptoms in one sentence]'\n"
            f"Be brief. Plain English. No extra text."
        )
        response = client.models.generate_content(model=MODEL, contents=prompt, config=genai_types.GenerateContentConfig(max_output_tokens=1024))
        return response.text.strip()
    except Exception:
        return "Thanks for describing your symptoms. A member of our team will review your request and be in touch to confirm your booking."


def _self_care_advice(symptom):
    try:
        client  = genai.Client(api_key=GOOGLE_API_KEY)
        prompt  = (
            f"Patient symptom: {symptom}\n\n"
            f"Give 4 self-care tips, one per line, each starting with '- '.\n"
            f"Keep each tip to one short sentence.\n"
            f"End with: 'See a GP if: [when to seek help]'\n"
            f"Plain English. No headers. No extra text."
        )
        response = client.models.generate_content(model=MODEL, contents=prompt, config=genai_types.GenerateContentConfig(max_output_tokens=1024))
        return response.text.strip()
    except Exception:
        return "Please rest and stay hydrated. If symptoms worsen or do not improve within 48 hours, contact us to book an appointment."


@app.route("/nhs/link")
def nhs_link():
    """Return the best NHS condition URL for a given symptom."""
    symptom = request.args.get("q", "").strip().lower()
    if not symptom:
        return jsonify({"url": f"https://www.nhs.uk/search/results?q="})
    # Check pharmacy referrals first (they have direct NHS URLs)
    path = os.path.join(os.path.dirname(__file__), "data", "pharmacy_referrals.json")
    try:
        with open(path, encoding="utf-8") as f:
            referrals = json.load(f)
        for r in referrals:
            for kw in r.get("keywords", []):
                if kw.lower() in symptom or symptom in kw.lower():
                    return jsonify({"url": r["nhs_url"], "condition": r["condition"]})
    except Exception:
        pass
    # Check NHS_CONDITIONS mapping
    for condition_name, keywords, url in NHS_CONDITIONS:
        if any(kw in symptom or symptom in kw for kw in keywords):
            return jsonify({"url": url, "condition": condition_name.title()})
    # Fallback to search
    return jsonify({"url": f"https://www.nhs.uk/search/results?q={symptom.replace(' ', '+')}", "condition": symptom})


def _check_pharmacy_referral(symptom):
    path = os.path.join(os.path.dirname(__file__), "data", "pharmacy_referrals.json")
    try:
        with open(path, encoding="utf-8") as f:
            referrals = json.load(f)
        symptom_lower = symptom.lower()
        for r in referrals:
            for kw in r.get("keywords", []):
                if kw.lower() in symptom_lower or symptom_lower in kw.lower():
                    return r
    except Exception:
        pass
    return None


NHS_CONDITIONS = [
    ("back pain",        ["back pain", "back ache", "backache", "lower back", "upper back", "spine pain"], "https://www.nhs.uk/conditions/back-pain/"),
    ("headache",         ["headache", "head pain", "migraine", "tension headache", "head ache"], "https://www.nhs.uk/conditions/headaches/"),
    ("chest pain",       ["chest pain", "chest tightness", "chest pressure", "chest discomfort"], "https://www.nhs.uk/conditions/chest-pain/"),
    ("cough",            ["cough", "coughing", "persistent cough", "dry cough", "wet cough", "chesty cough"], "https://www.nhs.uk/conditions/cough/"),
    ("cold",             ["cold", "common cold", "runny nose", "blocked nose", "sneezing", "stuffy nose"], "https://www.nhs.uk/conditions/common-cold/"),
    ("fever",            ["fever", "high temperature", "temperature", "pyrexia", "sweating", "chills"], "https://www.nhs.uk/conditions/fever-in-adults/"),
    ("fatigue",          ["fatigue", "tiredness", "exhaustion", "low energy", "feeling tired", "lethargy"], "https://www.nhs.uk/conditions/tiredness-and-fatigue/"),
    ("anxiety",          ["anxiety", "anxious", "panic attack", "panic", "worry", "stress"], "https://www.nhs.uk/conditions/generalised-anxiety-disorder/"),
    ("depression",       ["depression", "depressed", "low mood", "feeling low", "sadness"], "https://www.nhs.uk/conditions/clinical-depression/"),
    ("knee pain",        ["knee pain", "knee ache", "sore knee", "knee swelling", "knee injury"], "https://www.nhs.uk/conditions/knee-pain/"),
    ("shoulder pain",    ["shoulder pain", "shoulder ache", "sore shoulder", "frozen shoulder"], "https://www.nhs.uk/conditions/shoulder-pain/"),
    ("stomach ache",     ["stomach ache", "stomach pain", "abdominal pain", "tummy ache", "belly pain"], "https://www.nhs.uk/conditions/stomach-ache/"),
    ("nausea",           ["nausea", "nauseous", "feeling sick", "vomiting", "vomit", "being sick"], "https://www.nhs.uk/conditions/feeling-sick-nausea/"),
    ("insomnia",         ["insomnia", "sleep problems", "cant sleep", "difficulty sleeping", "sleep disorder"], "https://www.nhs.uk/conditions/insomnia/"),
    ("diabetes",         ["diabetes", "high blood sugar", "blood sugar", "diabetic"], "https://www.nhs.uk/conditions/diabetes/"),
    ("high blood pressure", ["high blood pressure", "hypertension", "blood pressure"], "https://www.nhs.uk/conditions/high-blood-pressure-hypertension/"),
    ("asthma",           ["asthma", "wheeze", "wheezing", "shortness of breath", "breathing difficulty", "breathless"], "https://www.nhs.uk/conditions/asthma/"),
    ("dizziness",        ["dizziness", "dizzy", "vertigo", "lightheaded", "light headed", "spinning"], "https://www.nhs.uk/conditions/dizziness/"),
    ("rash",             ["rash", "skin rash", "hives", "red rash", "itchy rash", "bumps on skin"], "https://www.nhs.uk/conditions/rashes-in-babies-and-children/"),
    ("toothache",        ["toothache", "tooth pain", "tooth ache", "dental pain", "sore tooth", "gum pain"], "https://www.nhs.uk/conditions/toothache/"),
    ("neck pain",        ["neck pain", "neck ache", "stiff neck", "sore neck"], "https://www.nhs.uk/conditions/neck-pain-and-stiff-neck/"),
    ("ankle pain",       ["ankle pain", "sprained ankle", "twisted ankle", "ankle swelling"], "https://www.nhs.uk/conditions/sprains-and-strains/"),
    ("sciatica",         ["sciatica", "sciatic nerve", "leg pain from back", "shooting leg pain"], "https://www.nhs.uk/conditions/sciatica/"),
    ("chest infection",  ["chest infection", "bronchitis", "lower respiratory", "phlegm", "mucus cough"], "https://www.nhs.uk/conditions/chest-infection/"),
    ("flu",              ["flu", "influenza", "flu symptoms", "body aches and fever"], "https://www.nhs.uk/conditions/flu/"),
]

def _search_nhs_conditions(symptom):
    path = os.path.join(os.path.dirname(__file__), "data", "pharmacy_referrals.json")
    results, seen = [], set()
    symptom_lower = symptom.lower()
    try:
        with open(path, encoding="utf-8") as f:
            referrals = json.load(f)
        for r in referrals:
            keywords = [k.lower() for k in r.get("keywords", [])]
            if any(symptom_lower in k or k in symptom_lower for k in keywords):
                url = r["nhs_url"]
                if url not in seen:
                    results.append({"name": r["condition"], "url": url})
                    seen.add(url)
    except Exception:
        pass
    for condition_name, keywords, url in NHS_CONDITIONS:
        if url in seen:
            continue
        if any(symptom_lower in k or k in symptom_lower for k in keywords):
            results.append({"name": condition_name.title(), "url": url})
            seen.add(url)
    if not results:
        results.append({"name": f"Search NHS A–Z for '{symptom}'", "url": f"https://www.nhs.uk/search/results?q={symptom.replace(' ', '+')}"})
    return results[:5]


def _create_demo_ticket(confirm_node_id, form_data):
    try:
        category, inbox = df.CONFIRMATION_INBOXES.get(confirm_node_id, ("General", "Unassigned"))
        now      = datetime.utcnow().isoformat()
        case_num = generate_case_number()

        sess_name     = session.get("patient_name", "")
        sess_nhs      = session.get("patient_nhs", "")
        sess_dob      = session.get("patient_dob", "")
        sess_phone    = session.get("patient_phone", "")
        sess_postcode = session.get("patient_postcode", "")
        sess_practice = session.get("patient_practice", "")

        first        = (session.get("patient_first_name") or form_data.get("first_name", "")).strip()
        last         = (session.get("patient_last_name")  or form_data.get("last_name",  "")).strip()
        patient_name = sess_name or f"{first} {last}".strip() or "Unknown"
        nhs_number   = sess_nhs      or form_data.get("nhs_number", "")
        dob          = sess_dob      or form_data.get("dob", "")
        phone        = sess_phone    or form_data.get("phone", "")
        postcode     = sess_postcode or form_data.get("postcode", "")
        gp_practice  = sess_practice

        match_status = "unverified"
        matched, confidence, similar_patients = match_patient(first, last, dob, postcode, nhs_number)
        if matched and confidence in ("matched", "ambiguous"):
            if confidence == "ambiguous":
                match_status = "needs_confirmation"
                # Store all similar candidates on the ticket for staff to review
                form_data["_similar_patients"] = json.dumps([{
                    "name":       f"{p.get('first_name','')} {p.get('last_name','')}".strip(),
                    "dob":        p.get("date_of_birth", ""),
                    "nhs":        p.get("nhs_number", ""),
                    "postcode":   p.get("postcode", ""),
                    "phone":      p.get("telephone", ""),
                    "practice":   p.get("gp_practice", ""),
                } for p in similar_patients])
            else:
                match_status = "verified"
            if matched.get("first_name") and matched.get("last_name"):
                patient_name = f"{matched['first_name']} {matched['last_name']}"
            if matched.get("nhs_number"):
                nhs_number = matched["nhs_number"]
            if matched.get("date_of_birth"):
                dob = matched["date_of_birth"]
            if matched.get("telephone"):
                phone = matched["telephone"]
            if matched.get("postcode"):
                postcode = matched["postcode"]
            if matched.get("gp_practice"):
                gp_practice = matched["gp_practice"]
        elif matched and confidence == "partial":
            match_status = "partial"

        symptom    = session.get("demo_symptom", "")
        query      = form_data.get("query_details", "")
        cert       = form_data.get("cert_type", "")
        med        = form_data.get("medication_name", "")
        nurse_appt = form_data.get("_last_input", "")

        if symptom:       title = f"{category} – {symptom[:60]}"
        elif query:       title = f"Admin Query – {query[:60]}"
        elif cert:        title = f"Medical Certificate – {cert}"
        elif med:         title = f"Repeat Prescription – {med[:60]}"
        elif nurse_appt:  title = f"Nurse Appointment – {nurse_appt[:60]}"
        else:             title = category

        proxy_first = session.get("proxy_first_name", "")
        proxy_last  = session.get("proxy_last_name", "")
        proxy_rel   = session.get("proxy_relationship", "")
        proxy_info  = ""
        if proxy_first or proxy_last:
            proxy_info = f"{proxy_first} {proxy_last}".strip()
            if proxy_rel:
                proxy_info += f" ({proxy_rel})"
        if proxy_info:
            form_data["_proxy_caller"] = proxy_info

        triage_by = session.get("triage_by_staff", "")
        if triage_by:
            form_data["_filled_by_staff"] = triage_by

        db = get_db()
        result = db.execute(
            text("""INSERT INTO tickets
                    (case_number, patient_name, nhs_number, dob, phone, postcode, gp_practice,
                     match_status, title, category, inbox, form_data, status, priority, created_at, updated_at)
                    VALUES (:cn, :pn, :nhs, :dob, :phone, :pc, :gp,
                            :ms, :title, :cat, :inbox, :fd, 'Open', 'Medium', :now, :now)
                    RETURNING id"""),
            {
                "cn": case_num, "pn": patient_name, "nhs": nhs_number, "dob": dob,
                "phone": phone, "pc": postcode, "gp": gp_practice, "ms": match_status,
                "title": title, "cat": category, "inbox": inbox,
                "fd": json.dumps(form_data), "now": now,
            }
        )
        ticket_id_new = result.fetchone()[0]
        db.execute(
            text("INSERT INTO notes (ticket_id, author, content, created_at) VALUES (:tid, :auth, :content, :now)"),
            {"tid": ticket_id_new, "auth": "system", "content": f"Ticket created · routed to **{inbox}** · match status: **{match_status}**", "now": now}
        )
        db.commit()
        db.close()
    except Exception as e:
        app.logger.error(f"Demo ticket creation failed: {e}")


@app.route("/pharmacy/nearby", methods=["POST"])
def pharmacy_nearby():
    import urllib.request
    import urllib.parse

    data = request.get_json()
    lat  = data.get("lat")
    lng  = data.get("lng")
    if not lat or not lng:
        return jsonify({"error": "Location required"}), 400

    def get_phone(place_id):
        try:
            params = urllib.parse.urlencode({"place_id": place_id, "fields": "formatted_phone_number", "key": GOOGLE_PLACES_API_KEY})
            url = f"https://maps.googleapis.com/maps/api/place/details/json?{params}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                detail = json.loads(resp.read())
            return detail.get("result", {}).get("formatted_phone_number", "")
        except Exception:
            return ""

    results = []
    for brand in ["Dischem", "Clicks"]:
        params = urllib.parse.urlencode({"location": f"{lat},{lng}", "rankby": "distance", "keyword": brand, "type": "pharmacy", "key": GOOGLE_PLACES_API_KEY})
        url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?{params}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                payload = json.loads(resp.read())
            places = payload.get("results", [])[:3]
            for p in places:
                loc      = p.get("geometry", {}).get("location", {})
                place_id = p.get("place_id", "")
                results.append({
                    "brand": brand, "name": p.get("name", brand), "address": p.get("vicinity", ""),
                    "rating": p.get("rating"), "open_now": p.get("opening_hours", {}).get("open_now"),
                    "place_id": place_id, "lat": loc.get("lat"), "lng": loc.get("lng"),
                    "phone": get_phone(place_id),
                })
        except Exception as e:
            app.logger.error(f"Places API error for {brand}: {e}")

    return jsonify({"pharmacies": results})


@app.route("/flow/ai", methods=["POST"])
def flow_ai():
    data         = request.get_json()
    node_id      = data.get("node_id")
    user_message = data.get("message", "").strip()
    history      = data.get("history", [])

    node  = get_node(node_id) if node_id else None
    label = node["label"] if node else "medical query"

    if node and node.get("systemPrompt"):
        system_prompt = node["systemPrompt"]
    else:
        system_prompt = (
            f"You are a helpful medical practice assistant for Health Access. "
            f"The patient is asking about: {label}. "
            f"Be professional, empathetic, and concise. "
            f"Always remind patients to call 999 for emergencies and 111 for urgent non-emergency care."
        )

    messages = []
    for entry in history:
        role    = entry.get("role")
        content = entry.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    gemini_history = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        gemini_history.append({"role": role, "parts": [m["content"]]})

    def generate():
        try:
            client   = genai.Client(api_key=GOOGLE_API_KEY)
            contents = []
            for msg in gemini_history:
                role = "model" if msg["role"] == "model" else "user"
                contents.append(genai_types.Content(role=role, parts=[genai_types.Part(text=msg["parts"][0])]))
            contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=user_message)]))
            config = genai_types.GenerateContentConfig(system_instruction=system_prompt, max_output_tokens=2048)
            for chunk in client.models.generate_content_stream(model=MODEL, contents=contents, config=config):
                if chunk.text:
                    yield f"data: {json.dumps({'text': chunk.text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': f'An unexpected error occurred: {str(e)}'})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


TEAMS  = ["Unassigned", "Nurse Appointment", "Clinical Triage", "Pharmacist", "Admin", "Medical Certificate", "Test Results"]
INBOXES = TEAMS

def get_nav_counts():
    try:
        db           = get_db()
        active       = "status NOT IN ('Closed','Resolved')"
        current_user = session.get("staff_username", "")
        your_inbox   = db.execute(text(f"SELECT COUNT(*) FROM tickets WHERE {active} AND assigned_to = :u"), {"u": current_user}).fetchone()[0]
        all_tickets  = db.execute(text(f"SELECT COUNT(*) FROM tickets WHERE {active}")).fetchone()[0]
        closed_count = db.execute(text("SELECT COUNT(*) FROM tickets WHERE status='Closed'")).fetchone()[0]
        unassigned   = db.execute(text(f"SELECT COUNT(*) FROM tickets WHERE {active} AND (assigned_to IS NULL OR assigned_to='')")).fetchone()[0]
        team_counts  = {}
        for t in TEAMS:
            c = db.execute(text(f"SELECT COUNT(*) FROM tickets WHERE {active} AND inbox = :t"), {"t": t}).fetchone()[0]
            team_counts[t] = c
        teammate_counts = {}
        for username in get_staff_usernames():
            c = db.execute(text(f"SELECT COUNT(*) FROM tickets WHERE {active} AND assigned_to = :u"), {"u": username}).fetchone()[0]
            teammate_counts[username] = c
        db.close()
        return {"your_inbox": your_inbox, "all": all_tickets, "unassigned": unassigned,
                "closed": closed_count, "teams": team_counts, "teammates": teammate_counts}
    except Exception:
        return {"your_inbox": 0, "all": 0, "unassigned": 0, "closed": 0, "teams": {}, "teammates": {}}


def _build_ticket_list(filters=None):
    db            = get_db()
    status_filter = (filters or {}).get("status", "Active")
    sort          = (filters or {}).get("sort", "newest")
    search        = (filters or {}).get("search", "").strip()
    team          = (filters or {}).get("team", "")
    view          = (filters or {}).get("view", "")
    user          = (filters or {}).get("user", "")

    conditions = []
    params     = {}
    _pc        = [0]

    def p(val):
        name = f"p{_pc[0]}"
        _pc[0] += 1
        params[name] = val
        return f":{name}"

    if status_filter == "Active":
        conditions.append("status NOT IN ('Closed', 'Resolved')")
    elif status_filter == "Closed":
        conditions.append("status = 'Closed'")
    elif status_filter and status_filter != "All":
        conditions.append(f"status = {p(status_filter)}")

    if search:
        s = f"%{search}%"
        conditions.append(f"(patient_name ILIKE {p(s)} OR id::text LIKE {p(s)} OR case_number ILIKE {p(s)})")

    if user:
        conditions.append(f"assigned_to = {p(user)}")
    elif view == "mine":
        current_user = session.get("staff_username", "")
        conditions.append(f"assigned_to = {p(current_user)}")
    elif team == "unassigned":
        conditions.append("(inbox IS NULL OR inbox = '' OR inbox = 'Unassigned')")
    elif team:
        conditions.append(f"inbox = {p(team)}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order = "ORDER BY created_at " + ("DESC" if sort != "oldest" else "ASC")

    rows = db.execute(text(f"SELECT * FROM tickets {where} {order}"), params).mappings().fetchall()
    db.close()

    result = []
    for t in rows:
        d = dict(t)
        d["time_ago"]    = time_ago(d["created_at"])
        d["avatar_color"] = get_avatar_color(d.get("patient_name", ""))
        name  = d.get("patient_name", "") or ""
        parts = name.strip().split()
        d["display_name"] = f"{parts[-1].upper()}, {' '.join(parts[:-1])}" if len(parts) >= 2 else (name.upper() if name else "Unknown")
        result.append(d)
    return result


@app.route("/inbox")
@staff_required
def inbox():
    status_filter = request.args.get("status", "Active")
    sort   = request.args.get("sort", "newest")
    search = request.args.get("search", "")
    team   = request.args.get("team", "")
    view   = request.args.get("view", "")
    user   = request.args.get("user", "")
    filters      = {"status": status_filter, "sort": sort, "search": search, "team": team, "view": view, "user": user}
    tickets_list = _build_ticket_list(filters)
    return render_template("inbox.html", tickets=tickets_list, selected=None, filters=filters,
                           teams=TEAMS, staff_users=sorted(get_staff_usernames()), nav_counts=get_nav_counts())


@app.route("/inbox/<int:ticket_id>")
@staff_required
def ticket_detail(ticket_id):
    db = get_db()
    db.execute(text("UPDATE tickets SET is_read=1 WHERE id=:id"), {"id": ticket_id})
    db.commit()
    ticket = db.execute(text("SELECT * FROM tickets WHERE id=:id"), {"id": ticket_id}).mappings().fetchone()
    if not ticket:
        db.close()
        return redirect(url_for("inbox"))
    notes  = db.execute(text("SELECT * FROM notes WHERE ticket_id=:tid ORDER BY created_at ASC"), {"tid": ticket_id}).mappings().fetchall()
    nhs    = ticket["nhs_number"] or ""
    recent = []
    if nhs:
        recent = db.execute(
            text("SELECT * FROM tickets WHERE nhs_number=:nhs AND id!=:id ORDER BY created_at DESC LIMIT 3"),
            {"nhs": nhs, "id": ticket_id}
        ).mappings().fetchall()
    db.close()

    status_filter = request.args.get("status", "Active")
    sort   = request.args.get("sort", "newest")
    search = request.args.get("search", "")
    team   = request.args.get("team", "")
    view   = request.args.get("view", "")
    user   = request.args.get("user", "")
    filters      = {"status": status_filter, "sort": sort, "search": search, "team": team, "view": view, "user": user}
    tickets_list = _build_ticket_list(filters)

    ticket_dict = dict(ticket)
    try:
        ticket_dict["form_fields"] = json.loads(ticket_dict.get("form_data") or "{}")
    except Exception:
        ticket_dict["form_fields"] = {}
    ticket_dict["avatar_color"] = get_avatar_color(ticket_dict.get("patient_name", ""))
    name  = ticket_dict.get("patient_name", "") or ""
    parts = name.strip().split()
    ticket_dict["display_name"] = f"{parts[-1].upper()}, {' '.join(parts[:-1])}" if len(parts) >= 2 else (name.upper() if name else "Unknown")

    notes_list  = [dict(n) for n in notes]
    recent_list = [dict(r) for r in recent]
    for r in recent_list:
        r["time_ago"] = time_ago(r["created_at"])

    return render_template("ticket_detail.html", tickets=tickets_list, selected=ticket_dict,
                           notes=notes_list, recent_tickets=recent_list, filters=filters,
                           teams=TEAMS, staff_users=sorted(get_staff_usernames()), nav_counts=get_nav_counts())


@app.route("/inbox/<int:ticket_id>/update", methods=["POST"])
@staff_required
def ticket_update(ticket_id):
    status             = request.form.get("status", "Open")
    priority           = request.form.get("priority", "Medium")
    assigned_to        = request.form.get("assigned_to", "")
    assignment_comment = request.form.get("assignment_comment", "").strip()
    current_user       = session.get("staff_username", "")
    now                = datetime.utcnow().isoformat()
    inbox_val          = request.form.get("inbox", "")
    db                 = get_db()

    prev = db.execute(text("SELECT status, priority, assigned_to, inbox FROM tickets WHERE id=:id"), {"id": ticket_id}).mappings().fetchone()
    prev_status   = prev["status"]      if prev else ""
    prev_priority = prev["priority"]    if prev else ""
    prev_assigned = prev["assigned_to"] if prev else ""
    prev_inbox    = prev["inbox"]       if prev else ""

    new_inbox = inbox_val or assigned_to

    # If redirected to a different team/inbox while In Progress → reset to Open and unassign
    if new_inbox and new_inbox != prev_inbox and prev_status == "In Progress":
        status      = "Open"
        assigned_to = ""

    if status == "In Progress" and not assigned_to:
        assigned_to = current_user

    db.execute(
        text("UPDATE tickets SET status=:s, priority=:pr, assigned_to=:at, inbox=:inbox, updated_at=:now WHERE id=:id"),
        {"s": status, "pr": priority, "at": assigned_to, "inbox": new_inbox, "now": now, "id": ticket_id}
    )

    audit_lines = []
    inbox_changed = new_inbox and new_inbox != prev_inbox

    if inbox_changed:
        audit_lines.append(f"Redirected to **{new_inbox}**")
        if prev_status == "In Progress" and status == "Open":
            audit_lines.append(f"Status reset to **Open**")
            audit_lines.append(f"Unassigned from **{prev_assigned}**")
    elif status != prev_status:
        audit_lines.append(f"Status: **{prev_status}** → **{status}**")

    if priority != prev_priority:
        audit_lines.append(f"Priority: **{prev_priority}** → **{priority}**")

    if not inbox_changed and assigned_to and assigned_to != prev_assigned:
        audit_lines.append(f"Assigned to **{assigned_to}**")
    elif not inbox_changed and not assigned_to and prev_assigned:
        audit_lines.append(f"Unassigned (was **{prev_assigned}**)")

    if assignment_comment:
        audit_lines.append(f"Note: {assignment_comment}")

    if audit_lines:
        db.execute(
            text("INSERT INTO notes (ticket_id, author, content, created_at) VALUES (:tid, :auth, :content, :now)"),
            {"tid": ticket_id, "auth": current_user, "content": " · ".join(audit_lines), "now": now}
        )

    db.commit()
    db.close()
    params = {k: v for k, v in request.form.items() if k in ("list_status", "list_sort", "list_search", "list_team")}
    qs     = "&".join(f"{k[5:]}={v}" for k, v in params.items() if v)
    return redirect(url_for("ticket_detail", ticket_id=ticket_id) + (f"?{qs}" if qs else ""))


@app.route("/inbox/<int:ticket_id>/confirm_patient", methods=["POST"])
@staff_required
def confirm_patient(ticket_id):
    """Staff confirms the correct patient from ambiguous matches."""
    idx          = int(request.form.get("patient_index", 0))
    current_user = session.get("staff_username", "Staff")
    now          = datetime.utcnow().isoformat()
    db           = get_db()

    ticket = db.execute(text("SELECT form_data FROM tickets WHERE id=:id"), {"id": ticket_id}).mappings().fetchone()
    if not ticket:
        db.close()
        return redirect(url_for("inbox"))

    fd = json.loads(ticket["form_data"] or "{}")
    candidates = json.loads(fd.get("_similar_patients", "[]"))

    if 0 <= idx < len(candidates):
        chosen = candidates[idx]
        db.execute(text("""
            UPDATE tickets SET
                patient_name = :pn,
                nhs_number   = :nhs,
                dob          = :dob,
                phone        = :phone,
                postcode     = :pc,
                gp_practice  = :gp,
                match_status = 'verified',
                updated_at   = :now
            WHERE id = :id
        """), {
            "pn":    chosen["name"],
            "nhs":   chosen["nhs"],
            "dob":   chosen["dob"],
            "phone": chosen["phone"],
            "pc":    chosen["postcode"],
            "gp":    chosen["practice"],
            "now":   now,
            "id":    ticket_id,
        })
        # Remove _similar_patients from form_data now confirmed
        fd.pop("_similar_patients", None)
        db.execute(text("UPDATE tickets SET form_data=:fd WHERE id=:id"), {"fd": json.dumps(fd), "id": ticket_id})
        db.execute(text("INSERT INTO notes (ticket_id, author, content, created_at) VALUES (:tid, :auth, :content, :now)"),
            {"tid": ticket_id, "auth": current_user,
             "content": f"Patient confirmed as **{chosen['name']}** (NHS: {chosen['nhs']}) · match status set to **Verified**",
             "now": now})
        db.commit()

    db.close()
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/inbox/<int:ticket_id>/close", methods=["POST"])
@staff_required
def ticket_close(ticket_id):
    now           = datetime.utcnow().isoformat()
    closed_by     = session.get("staff_username", "Staff")
    close_comment = request.form.get("close_comment", "").strip()
    db            = get_db()
    db.execute(
        text("UPDATE tickets SET status='Closed', closed_by=:cb, closed_at=:ca, updated_at=:now WHERE id=:id"),
        {"cb": closed_by, "ca": now, "now": now, "id": ticket_id}
    )
    note_parts = [f"Ticket closed by **{closed_by}**"]
    if close_comment:
        note_parts.append(f"Note: {close_comment}")
    db.execute(
        text("INSERT INTO notes (ticket_id, author, content, created_at) VALUES (:tid, :auth, :content, :now)"),
        {"tid": ticket_id, "auth": closed_by, "content": " · ".join(note_parts), "now": now}
    )
    db.commit()
    db.close()
    return redirect(url_for("inbox"))


@app.route("/inbox/<int:ticket_id>/note", methods=["POST"])
@staff_required
def ticket_note(ticket_id):
    content = request.form.get("content", "").strip()
    if content:
        now = datetime.utcnow().isoformat()
        db  = get_db()
        db.execute(
            text("INSERT INTO notes (ticket_id, author, content, created_at) VALUES (:tid, :auth, :content, :now)"),
            {"tid": ticket_id, "auth": session.get("staff_username", "Staff"), "content": content, "now": now}
        )
        db.commit()
        db.close()
    qs_parts = []
    for k in ("status", "sort", "search", "team"):
        v = request.form.get(f"list_{k}", "")
        if v:
            qs_parts.append(f"{k}={v}")
    qs = "&".join(qs_parts)
    return redirect(url_for("ticket_detail", ticket_id=ticket_id) + (f"?{qs}" if qs else ""))


@app.route("/dashboard")
@staff_required
def dashboard():
    days   = int(request.args.get("days", 30))
    db     = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    total        = db.execute(text("SELECT COUNT(*) FROM tickets WHERE created_at >= :c"), {"c": cutoff}).fetchone()[0]
    open_count   = db.execute(text("SELECT COUNT(*) FROM tickets WHERE status NOT IN ('Closed','Resolved') AND created_at >= :c"), {"c": cutoff}).fetchone()[0]
    closed_count = db.execute(text("SELECT COUNT(*) FROM tickets WHERE status IN ('Closed','Resolved') AND created_at >= :c"), {"c": cutoff}).fetchone()[0]
    resolution_rate = round(closed_count / total * 100) if total else 0

    response_times = db.execute(text("""
        SELECT t.created_at, MIN(n.created_at) as first_note
        FROM tickets t JOIN notes n ON n.ticket_id = t.id
        WHERE t.created_at >= :c
        GROUP BY t.id, t.created_at
    """), {"c": cutoff}).fetchall()
    if response_times:
        diffs = []
        for row in response_times:
            try:
                diffs.append((datetime.fromisoformat(row[1]) - datetime.fromisoformat(row[0])).total_seconds() / 3600)
            except Exception:
                pass
        avg_response = round(sum(diffs) / len(diffs), 1) if diffs else 0.0
    else:
        avg_response = 0.0

    volume_rows   = db.execute(text("SELECT DATE(created_at::timestamp) as day, COUNT(*) as cnt FROM tickets WHERE created_at >= :c GROUP BY day ORDER BY day"), {"c": cutoff}).fetchall()
    volume_labels = [str(r[0]) for r in volume_rows]
    volume_data   = [r[1] for r in volume_rows]

    status_rows   = db.execute(text("SELECT status, COUNT(*) FROM tickets WHERE created_at >= :c GROUP BY status"), {"c": cutoff}).fetchall()
    status_labels = [r[0] for r in status_rows]
    status_data   = [r[1] for r in status_rows]

    priority_rows   = db.execute(text("SELECT priority, COUNT(*) FROM tickets WHERE created_at >= :c GROUP BY priority"), {"c": cutoff}).fetchall()
    priority_labels = [r[0] for r in priority_rows]
    priority_data   = [r[1] for r in priority_rows]

    team_rows = db.execute(text("""
        SELECT assigned_to, COUNT(*) FROM tickets
        WHERE status NOT IN ('Closed','Resolved') AND assigned_to IS NOT NULL AND assigned_to != ''
        GROUP BY assigned_to ORDER BY COUNT(*) DESC
    """)).fetchall()

    user_rows = db.execute(text("""
        SELECT assigned_to, COUNT(*) FROM tickets
        WHERE status NOT IN ('Closed','Resolved') AND assigned_to IS NOT NULL AND assigned_to != ''
        GROUP BY assigned_to ORDER BY COUNT(*) DESC
    """)).fetchall()

    unassigned = db.execute(text("""
        SELECT COUNT(*) FROM tickets
        WHERE status NOT IN ('Closed','Resolved') AND (assigned_to IS NULL OR assigned_to='')
    """)).fetchone()[0]

    tickets_list = _build_ticket_list({"status": "All", "sort": "newest", "search": "", "team": ""})
    db.close()

    filters = {"status": "Open", "sort": "newest", "search": "", "team": ""}
    return render_template("dashboard.html", tickets=tickets_list, filters=filters, teams=TEAMS,
        nav_counts=get_nav_counts(), days=days, total=total, open_count=open_count,
        avg_response=avg_response, resolution_rate=resolution_rate,
        volume_labels=volume_labels, volume_data=volume_data,
        status_labels=status_labels, status_data=status_data,
        priority_labels=priority_labels, priority_data=priority_data,
        team_rows=team_rows, user_rows=user_rows, unassigned=unassigned)


@app.route("/inbox/counts")
@staff_required
def inbox_counts():
    db         = get_db()
    total_open = db.execute(text("SELECT COUNT(*) FROM tickets WHERE status='Open'")).fetchone()[0]
    total_all  = db.execute(text("SELECT COUNT(*) FROM tickets")).fetchone()[0]
    unassigned = db.execute(text("SELECT COUNT(*) FROM tickets WHERE status='Open' AND (assigned_to IS NULL OR assigned_to='')")).fetchone()[0]
    team_counts = {}
    for team in TEAMS:
        c = db.execute(text("SELECT COUNT(*) FROM tickets WHERE status='Open' AND assigned_to=:t"), {"t": team}).fetchone()[0]
        team_counts[team] = c
    db.close()
    return jsonify({"total_open": total_open, "total_all": total_all, "unassigned": unassigned, "teams": team_counts})

@app.context_processor
def inject_open_ticket_count():
    return {"open_ticket_count": 0, "staff_authenticated": session.get("staff_authenticated", False)}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, port=port)
