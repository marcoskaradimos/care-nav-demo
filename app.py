import os
import json
import re
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict
from html.parser import HTMLParser
from flask import Flask, render_template, request, redirect, url_for, session, Response, stream_with_context, jsonify, g, abort
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)

# ── Database ─────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "tickets.db")

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

AVATAR_COLORS = [
    '#e74c3c', '#e67e22', '#f1c40f', '#2ecc71',
    '#1abc9c', '#3498db', '#9b59b6', '#e91e63'
]

def get_avatar_color(name):
    """Return a color from AVATAR_COLORS based on name hash."""
    if not name:
        return AVATAR_COLORS[0]
    return AVATAR_COLORS[abs(hash(name)) % len(AVATAR_COLORS)]

def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_name TEXT,
            nhs_number TEXT,
            dob TEXT,
            phone TEXT,
            postcode TEXT,
            title TEXT,
            category TEXT,
            form_data TEXT,
            status TEXT DEFAULT 'Open',
            priority TEXT DEFAULT 'Medium',
            assigned_to TEXT DEFAULT '',
            is_read INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    # Add columns if they don't exist (for existing databases)
    for col, definition in [
        ("assigned_to", "TEXT DEFAULT ''"),
        ("is_read", "INTEGER DEFAULT 0"),
    ]:
        try:
            db.execute(f"ALTER TABLE tickets ADD COLUMN {col} {definition}")
        except Exception:
            pass
    db.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            author TEXT,
            content TEXT,
            created_at TEXT
        )
    """)
    db.commit()
    db.close()

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
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")


GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
MODEL = "gemini-flash-latest"

# ── HTML Stripper ────────────────────────────────────────────────────────────
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

# ── Load Flow Graph ──────────────────────────────────────────────────────────
def load_flow():
    flow_path = os.path.join(os.path.dirname(__file__), "Flow.json")
    with open(flow_path, encoding="utf-8") as f:
        raw = json.load(f)

    flow = raw[0] if isinstance(raw, list) else raw
    fd = flow["flow_data"]
    raw_nodes = fd["nodes"]
    edges = fd["edges"]

    # Build adjacency map: node_id -> [{target, handle}]
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
        for f in d.get("fields", []):
            entry["fields"].append({
                "id": f.get("id", ""),
                "label": f.get("label", ""),
                "type": f.get("type", "text"),
                "required": f.get("required", False),
            })
        # schedules for scheduleNode
        if "schedules" in d:
            entry["schedules"] = d["schedules"]
        # custom system prompt and auto-trigger for AI nodes
        if "systemPrompt" in d:
            entry["systemPrompt"] = d["systemPrompt"]
        if "auto_trigger" in d:
            entry["auto_trigger"] = d["auto_trigger"]
        # placeholder for form fields
        for f in d.get("fields", []):
            if "placeholder" not in [x.get("id") for x in entry["fields"]]:
                pass  # already added above
        # re-map fields with placeholder and options
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

    # ── Blood test: inject dedicated type form + patient details ────────────
    BLOOD_TEST_FORM = "synthetic_blood_test_form"
    BLOOD_TEST_PATIENT = "synthetic_blood_test_patient"
    # Original confirmation chain after the Nurse form createTicket
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

    # Rewire blood test message node -> blood test form (skip the Nurse/HCA form)
    if "messageNode-1764442597203" in nodes:
        adj["messageNode-1764442597203"] = [{"target": BLOOD_TEST_FORM, "handle": ""}]
        nodes["messageNode-1764442597203"]["next"] = adj["messageNode-1764442597203"]

    # ── Override welcome message ─────────────────────────────────────────────
    if "welcome_message" in nodes:
        nodes["welcome_message"]["message"] = (
            "Hello! Welcome to Access Care Navigation, I'm your assistant, here to help you "
            "find the right information or service. How can I assist you today?"
        )

    # ── Inject synthetic patient details form after specified forms ──────────
    # Each form gets its own synthetic node so edges don't clash.
    # ── Inject URL links for specific options ────────────────────────────────
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

    # Set form title for medicine query form
    med_form = nodes.get("customFormNode-1764448405115")
    if med_form:
        med_form["form_title"] = "Please complete this form"

    # Replace general enquiry form fields with just the enquiry details field
    gen_enquiry = nodes.get("general_enquiry_form")
    if gen_enquiry:
        gen_enquiry["fields"] = [
            {"id": "enquiry_details", "label": "Your Enquiry Details", "type": "long_text", "required": True},
        ]

    # Forms that need patient details injected, with optional extra fields
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

# ── Flow Engine Helpers ──────────────────────────────────────────────────────
def get_node(node_id):
    return FLOW_NODES.get(node_id)

def follow_edge(node_id, handle=None):
    """Return the target node_id for a given source and optional handle."""
    edges = FLOW_ADJ.get(node_id, [])
    if not edges:
        return None
    if handle:
        for e in edges:
            if e["handle"] == handle:
                return e["target"]
    return edges[0]["target"]

def skip_passthrough_nodes(node_id):
    """Walk past waitNode and startNode automatically."""
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
    """Skip pass-through nodes and return the first real node."""
    return skip_passthrough_nodes(node_id)

def build_node_response(node_id):
    """
    Walk the flow from node_id and build a response dict:
    {
        node_id, type, message, options, fields, is_end, next_node_id
    }
    Handles chains of messageNodes automatically.
    """
    node_id = resolve_node(node_id)
    if not node_id:
        return None

    node = get_node(node_id)
    if not node:
        return None

    ntype = node["type"]

    # Auto-chain through messageNodes that have no options (just display)
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
        # Stop chaining when we hit something interactive
        if not next_node or next_node["type"] in ("optionsNode", "buttonNode", "customFormNode",
                                                    "questionNode", "endNode", "knowledgeBaseNode",
                                                    "createTicketNode"):
            current_id = next_id
            current = next_node
            break
        current_id = next_id
        current = next_node

    combined_message = "\n\n".join(m for m in messages if m)

    # Now handle what we landed on
    if not current:
        return {
            "node_id": node_id,
            "type": "end",
            "message": combined_message,
            "options": [],
            "fields": [],
            "is_end": True,
        }

    ctype = current["type"] if current else "end"

    if ctype == "endNode":
        node_msg = current.get("message", "")
        msg = combined_message + ("\n\n" + node_msg if node_msg else "") if combined_message else node_msg
        return {
            "node_id": current_id,
            "type": "end",
            "message": msg,
            "options": [],
            "fields": [],
            "is_end": True,
        }

    if ctype == "optionsNode":
        msg = current["message"] or ""
        if combined_message:
            msg = combined_message + ("\n\n" + msg if msg else "")
        options = current["options"]
        # For testing: restrict main menu to Book an appointment only
        if current_id == "main_menu_options":
            options = [o for o in options if o["id"] == "opt_appointments"]
        return {
            "node_id": current_id,
            "type": "options",
            "message": msg,
            "options": options,
            "fields": [],
            "is_end": False,
        }

    if ctype == "buttonNode":
        msg = current["message"] or ""
        if combined_message:
            msg = combined_message + ("\n\n" + msg if msg else "")
        return {
            "node_id": current_id,
            "type": "options",
            "message": msg,
            "options": current["options"],
            "fields": [],
            "is_end": False,
        }

    if ctype == "customFormNode":
        node_message = current.get("message", "")
        msg = combined_message or node_message or "Please fill in the form below:"
        return {
            "node_id": current_id,
            "type": "form",
            "message": msg,
            "form_title": current.get("form_title", ""),
            "options": [],
            "fields": current["fields"],
            "is_end": False,
        }

    if ctype == "apptSymptomNode":
        msg = current["message"] or ""
        if combined_message:
            msg = combined_message + ("\n\n" + msg if msg else "")
        return {
            "node_id": current_id,
            "type": "appt_symptom",
            "message": msg,
            "options": current.get("options", []),
            "fields": current.get("fields", []),
            "is_end": False,
        }

    if ctype == "questionNode":
        msg = current["message"] or ""
        if combined_message:
            msg = combined_message + ("\n\n" + msg if msg else "")
        return {
            "node_id": current_id,
            "type": "question",
            "message": msg,
            "options": [],
            "fields": [],
            "is_end": False,
        }

    if ctype == "knowledgeBaseNode":
        return {
            "node_id": current_id,
            "type": "ai",
            "message": combined_message or current.get("label", ""),
            "options": [],
            "fields": [],
            "is_end": False,
            "auto_trigger": current.get("auto_trigger", False),
        }

    if ctype == "createTicketNode":
        # Auto-follow to next node after ticket creation
        next_id = resolve_node(follow_edge(current_id))
        if next_id:
            sub = build_node_response(next_id)
            if sub:
                sub["message"] = combined_message + ("\n\n" + sub["message"] if sub["message"] else "")
                return sub
        return {
            "node_id": current_id,
            "type": "end",
            "message": combined_message or "Your request has been submitted.",
            "options": [],
            "fields": [],
            "is_end": True,
        }

    if ctype == "scheduleNode":
        # Always route to in-hours path (index 0) for now
        nexts = current["next"]
        if nexts:
            return build_node_response(nexts[0]["target"])

    if ctype == "routingNode":
        nexts = current["next"]
        if nexts:
            return build_node_response(nexts[0]["target"])

    if ctype == "logicNode":
        nexts = current["next"]
        if nexts:
            return build_node_response(nexts[0]["target"])

    # Fallback — follow the edge
    next_id = resolve_node(follow_edge(current_id))
    if next_id and next_id != current_id:
        return build_node_response(next_id)

    return {
        "node_id": current_id,
        "type": "end",
        "message": combined_message or current.get("message", ""),
        "options": [],
        "fields": [],
        "is_end": True,
    }


# ── Auth ─────────────────────────────────────────────────────────────────────
# Staff users for inbox access
STAFF_USERS = {
    "staff": generate_password_hash("staff123"),
    "admin": generate_password_hash("admin123"),
}

def staff_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("staff_authenticated"):
            return redirect(url_for("staff_login", next=request.url))
        return f(*args, **kwargs)
    return decorated


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if session.get("staff_authenticated"):
        return redirect(url_for("inbox"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        hashed = STAFF_USERS.get(username)
        if hashed and check_password_hash(hashed, password):
            session["staff_authenticated"] = True
            session["staff_username"] = username
            return redirect(request.args.get("next") or url_for("inbox"))
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

@app.route("/flow/start")
def flow_start():
    """Return the initial flow state (start node)."""
    result = build_node_response("start")
    return jsonify(result)

@app.route("/flow/step", methods=["POST"])
def flow_step():
    """
    Advance the flow.
    Body: { node_id, option_handle }  (option_handle = the option id selected)
    Returns the next node response.
    """
    data = request.get_json()
    node_id = data.get("node_id")
    option_handle = data.get("option_handle", "")
    form_data = data.get("form_data", {})
    all_form_data = data.get("all_form_data", {})

    node = get_node(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404

    # Find next node
    if option_handle:
        next_id = follow_edge(node_id, option_handle)
        if not next_id:
            # Try matching by option id in options list then follow first edge with that handle
            next_id = follow_edge(node_id, option_handle)
        if not next_id:
            # Fallback: first edge
            next_id = follow_edge(node_id)
    else:
        next_id = follow_edge(node_id)

    if not next_id:
        return jsonify({"node_id": node_id, "type": "end", "message": "Thank you for using our service.", "options": [], "fields": [], "is_end": True})

    # ── Create ticket if this is a patient details form submission ────────────
    # Clinician consultation request — create ticket from accumulated all_form_data
    if node_id == "appt_contact_time" and all_form_data:
        try:
            patient_name = all_form_data.get("patient_name", "Unknown")
            now = datetime.utcnow().isoformat()
            symptoms = all_form_data.get("symptom_description", "")
            available_time = all_form_data.get("available_time", "")
            contact_number = all_form_data.get("contact_number", "")
            title = f"Clinician Consultation – {symptoms[:60]}{'...' if len(symptoms) > 60 else ''}" if symptoms else "Clinician Consultation Request"

            # ── Suggested priority from pain score + patient-reported urgency ──
            def suggested_priority(data):
                urgency = data.get("patient_urgency", "").lower()
                pain_raw = data.get("experiencing_pain", "").lower()
                # Extract pain score if present
                pain_score = 0
                if "pain score:" in pain_raw:
                    try:
                        pain_score = int(pain_raw.split("pain score:")[-1].strip().split("/")[0].strip())
                    except Exception:
                        pain_score = 0

                if "emergency" in urgency or pain_score >= 9:
                    return "Urgent"
                if "very urgent" in urgency or pain_score >= 7:
                    return "High"
                if "fairly urgent" in urgency or pain_score >= 4:
                    return "Medium"
                return "Low"

            priority = suggested_priority(all_form_data)

            db = get_db()
            db.execute(
                """INSERT INTO tickets
                   (patient_name, nhs_number, dob, phone, postcode, title, category, form_data, status, priority, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Open', ?, ?, ?)""",
                (
                    patient_name,
                    all_form_data.get("identifier", ""),
                    all_form_data.get("dob", ""),
                    contact_number,
                    "",
                    title,
                    "Appointments",
                    json.dumps(all_form_data),
                    priority,
                    now,
                    now,
                )
            )
            db.commit()
            db.close()
        except Exception as e:
            app.logger.error(f"Clinician ticket creation failed: {e}")

    PATIENT_DETAIL_NODES = {
        "synthetic_blood_test_patient",
        "synthetic_patient_details_customFormNode-1764448405115",
        "synthetic_patient_details_customFormNode-1764440727948",
        "synthetic_patient_details_cert_request_form",
        "synthetic_patient_details_general_enquiry_form",
    }
    if node_id in PATIENT_DETAIL_NODES and form_data and (
        form_data.get("first_name") or form_data.get("nhs_number")
    ):
        try:
            first = form_data.get("first_name", "")
            last = form_data.get("last_name", "")
            patient_name = f"{first} {last}".strip() or "Unknown"
            now = datetime.utcnow().isoformat()
            # Use all_form_data (all forms combined) if available, else fall back to form_data
            ticket_form_data = all_form_data if all_form_data else form_data
            db = get_db()
            db.execute(
                """INSERT INTO tickets
                   (patient_name, nhs_number, dob, phone, postcode, title, category, form_data, status, priority, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Open', 'Medium', ?, ?)""",
                (
                    patient_name,
                    form_data.get("nhs_number", ""),
                    form_data.get("dob", ""),
                    form_data.get("phone", ""),
                    form_data.get("postcode", ""),
                    form_data.get("title", ""),
                    infer_category(node_id),
                    json.dumps(ticket_form_data),
                    now,
                    now,
                )
            )
            db.commit()
            db.close()
        except Exception as e:
            app.logger.error(f"Ticket creation failed: {e}")

    result = build_node_response(next_id)
    if not result:
        return jsonify({"node_id": next_id, "type": "end", "message": "Thank you for using our service.", "options": [], "fields": [], "is_end": True})

    return jsonify(result)

@app.route("/pharmacy/nearby", methods=["POST"])
def pharmacy_nearby():
    """Find nearby Dischem and Clicks pharmacies using Google Places API."""
    import urllib.request
    import urllib.parse

    data = request.get_json()
    lat = data.get("lat")
    lng = data.get("lng")

    if not lat or not lng:
        return jsonify({"error": "Location required"}), 400

    def get_phone(place_id):
        """Fetch formatted phone number from Place Details API."""
        try:
            params = urllib.parse.urlencode({
                "place_id": place_id,
                "fields": "formatted_phone_number",
                "key": GOOGLE_PLACES_API_KEY,
            })
            url = f"https://maps.googleapis.com/maps/api/place/details/json?{params}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                detail = json.loads(resp.read())
            return detail.get("result", {}).get("formatted_phone_number", "")
        except Exception:
            return ""

    results = []
    for brand in ["Dischem", "Clicks"]:
        params = urllib.parse.urlencode({
            "location": f"{lat},{lng}",
            "rankby": "distance",
            "keyword": brand,
            "type": "pharmacy",
            "key": GOOGLE_PLACES_API_KEY,
        })
        url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?{params}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                payload = json.loads(resp.read())
            places = payload.get("results", [])[:3]
            for p in places:
                loc = p.get("geometry", {}).get("location", {})
                place_id = p.get("place_id", "")
                results.append({
                    "brand": brand,
                    "name": p.get("name", brand),
                    "address": p.get("vicinity", ""),
                    "rating": p.get("rating"),
                    "open_now": p.get("opening_hours", {}).get("open_now"),
                    "place_id": place_id,
                    "lat": loc.get("lat"),
                    "lng": loc.get("lng"),
                    "phone": get_phone(place_id),
                })
        except Exception as e:
            app.logger.error(f"Places API error for {brand}: {e}")

    return jsonify({"pharmacies": results})


@app.route("/flow/ai", methods=["POST"])
def flow_ai():
    """Stream an AI response for knowledgeBaseNode or questionNode."""
    data = request.get_json()
    node_id = data.get("node_id")
    user_message = data.get("message", "").strip()
    history = data.get("history", [])

    node = get_node(node_id) if node_id else None
    label = node["label"] if node else "medical query"

    # Use node-level systemPrompt if defined, otherwise default
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
        role = entry.get("role")
        content = entry.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    # Build Gemini message history (convert role 'assistant' -> 'model')
    gemini_history = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        gemini_history.append({"role": role, "parts": [m["content"]]})

    def generate():
        try:
            client = genai.Client(api_key=GOOGLE_API_KEY)
            contents = []
            for msg in gemini_history:
                role = "model" if msg["role"] == "model" else "user"
                contents.append(genai_types.Content(role=role, parts=[genai_types.Part(text=msg["parts"][0])]))
            contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=user_message)]))
            config = genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=2048,
            )
            for chunk in client.models.generate_content_stream(model=MODEL, contents=contents, config=config):
                if chunk.text:
                    yield f"data: {json.dumps({'text': chunk.text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': f'An unexpected error occurred: {str(e)}'})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


TEAMS = ["Clinical Team", "Administration Team", "Appointments", "Pharmacist Team"]

def get_nav_counts():
    """Return ticket counts for inbox nav badges."""
    try:
        db = get_db()
        active = "status NOT IN ('Closed','Resolved')"
        your_inbox = db.execute(f"SELECT COUNT(*) FROM tickets WHERE {active}").fetchone()[0]
        all_tickets = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        unassigned = db.execute(
            f"SELECT COUNT(*) FROM tickets WHERE {active} AND (assigned_to IS NULL OR assigned_to='')"
        ).fetchone()[0]
        team_counts = {}
        for t in TEAMS:
            c = db.execute(
                f"SELECT COUNT(*) FROM tickets WHERE {active} AND assigned_to=?", (t,)
            ).fetchone()[0]
            team_counts[t] = c
        db.close()
        return {"your_inbox": your_inbox, "all": all_tickets, "unassigned": unassigned, "teams": team_counts}
    except Exception:
        return {"your_inbox": 0, "all": 0, "unassigned": 0, "teams": {}}

def _build_ticket_list(filters=None):
    """Fetch tickets with optional filtering. Returns list of dicts with extra computed fields."""
    db = get_db()
    status_filter = (filters or {}).get("status", "Open")
    sort = (filters or {}).get("sort", "newest")
    search = (filters or {}).get("search", "").strip()
    team = (filters or {}).get("team", "")

    conditions = []
    params = []

    if status_filter and status_filter != "All":
        conditions.append("status = ?")
        params.append(status_filter)

    if search:
        conditions.append("(patient_name LIKE ? OR CAST(id AS TEXT) LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    if team == "unassigned":
        conditions.append("(assigned_to IS NULL OR assigned_to = '')")
    elif team:
        conditions.append("assigned_to = ?")
        params.append(team)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order = "ORDER BY created_at " + ("DESC" if sort != "oldest" else "ASC")

    rows = db.execute(f"SELECT * FROM tickets {where} {order}", params).fetchall()
    db.close()

    result = []
    for t in rows:
        d = dict(t)
        d["time_ago"] = time_ago(d["created_at"])
        d["avatar_color"] = get_avatar_color(d.get("patient_name", ""))
        # Format display name: LASTNAME, Firstname
        name = d.get("patient_name", "") or ""
        parts = name.strip().split()
        if len(parts) >= 2:
            d["display_name"] = f"{parts[-1].upper()}, {' '.join(parts[:-1])}"
        else:
            d["display_name"] = name.upper() if name else "Unknown"
        result.append(d)
    return result


@app.route("/inbox")
@staff_required
def inbox():
    status_filter = request.args.get("status", "Open")
    sort = request.args.get("sort", "newest")
    search = request.args.get("search", "")
    team = request.args.get("team", "")
    filters = {"status": status_filter, "sort": sort, "search": search, "team": team}
    tickets_list = _build_ticket_list(filters)
    return render_template(
        "inbox.html",
        tickets=tickets_list,
        selected=None,
        filters=filters,
        teams=TEAMS,
        nav_counts=get_nav_counts(),
    )


@app.route("/inbox/<int:ticket_id>")
@staff_required
def ticket_detail(ticket_id):
    # Mark as read
    db = get_db()
    db.execute("UPDATE tickets SET is_read=1 WHERE id=?", (ticket_id,))
    db.commit()
    ticket = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not ticket:
        db.close()
        return redirect(url_for("inbox"))
    notes = db.execute(
        "SELECT * FROM notes WHERE ticket_id=? ORDER BY created_at ASC", (ticket_id,)
    ).fetchall()
    # Recent tickets from same NHS number
    nhs = ticket["nhs_number"] or ""
    recent = []
    if nhs:
        recent = db.execute(
            "SELECT * FROM tickets WHERE nhs_number=? AND id!=? ORDER BY created_at DESC LIMIT 3",
            (nhs, ticket_id)
        ).fetchall()
    db.close()

    # Build ticket list using same filters from query params
    status_filter = request.args.get("status", "Open")
    sort = request.args.get("sort", "newest")
    search = request.args.get("search", "")
    team = request.args.get("team", "")
    filters = {"status": status_filter, "sort": sort, "search": search, "team": team}
    tickets_list = _build_ticket_list(filters)

    ticket_dict = dict(ticket)
    try:
        ticket_dict["form_fields"] = json.loads(ticket_dict.get("form_data") or "{}")
    except Exception:
        ticket_dict["form_fields"] = {}
    ticket_dict["avatar_color"] = get_avatar_color(ticket_dict.get("patient_name", ""))
    name = ticket_dict.get("patient_name", "") or ""
    parts = name.strip().split()
    if len(parts) >= 2:
        ticket_dict["display_name"] = f"{parts[-1].upper()}, {' '.join(parts[:-1])}"
    else:
        ticket_dict["display_name"] = name.upper() if name else "Unknown"

    notes_list = [dict(n) for n in notes]
    recent_list = [dict(r) for r in recent]
    for r in recent_list:
        r["time_ago"] = time_ago(r["created_at"])

    return render_template(
        "ticket_detail.html",
        tickets=tickets_list,
        selected=ticket_dict,
        notes=notes_list,
        recent_tickets=recent_list,
        filters=filters,
        teams=TEAMS,
        nav_counts=get_nav_counts(),
    )


@app.route("/inbox/<int:ticket_id>/update", methods=["POST"])
@staff_required
def ticket_update(ticket_id):
    status = request.form.get("status", "Open")
    priority = request.form.get("priority", "Medium")
    assigned_to = request.form.get("assigned_to", "")
    assignment_comment = request.form.get("assignment_comment", "").strip()
    now = datetime.utcnow().isoformat()
    db = get_db()
    db.execute(
        "UPDATE tickets SET status=?, priority=?, assigned_to=?, updated_at=? WHERE id=?",
        (status, priority, assigned_to, now, ticket_id)
    )
    # Save assignment comment as a note
    if assignment_comment and assigned_to:
        author = session.get("staff_username", "Staff")
        note_content = f"Assigned to **{assigned_to}**: {assignment_comment}"
        db.execute(
            "INSERT INTO notes (ticket_id, author, content, created_at) VALUES (?, ?, ?, ?)",
            (ticket_id, author, note_content, now)
        )
    db.commit()
    db.close()
    # Preserve list filters in redirect
    params = {k: v for k, v in request.form.items()
              if k in ("list_status", "list_sort", "list_search", "list_team")}
    qs = "&".join(
        f"{k[5:]}={v}" for k, v in params.items() if v
    )
    return redirect(url_for("ticket_detail", ticket_id=ticket_id) + (f"?{qs}" if qs else ""))


@app.route("/inbox/<int:ticket_id>/close", methods=["POST"])
@staff_required
def ticket_close(ticket_id):
    now = datetime.utcnow().isoformat()
    db = get_db()
    db.execute(
        "UPDATE tickets SET status='Closed', updated_at=? WHERE id=?",
        (now, ticket_id)
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
        db = get_db()
        db.execute(
            "INSERT INTO notes (ticket_id, author, content, created_at) VALUES (?, ?, ?, ?)",
            (ticket_id, session.get("staff_username", "Staff"), content, now)
        )
        db.commit()
        db.close()
    # Preserve list filters
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
    days = int(request.args.get("days", 30))
    db = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    # Core stats
    total = db.execute("SELECT COUNT(*) FROM tickets WHERE created_at >= ?", (cutoff,)).fetchone()[0]
    open_count = db.execute(
        "SELECT COUNT(*) FROM tickets WHERE status NOT IN ('Closed','Resolved') AND created_at >= ?", (cutoff,)
    ).fetchone()[0]
    closed_count = db.execute(
        "SELECT COUNT(*) FROM tickets WHERE status IN ('Closed','Resolved') AND created_at >= ?", (cutoff,)
    ).fetchone()[0]
    resolution_rate = round(closed_count / total * 100) if total else 0

    # Avg response time (ticket created → first note)
    response_times = db.execute("""
        SELECT t.created_at, MIN(n.created_at) as first_note
        FROM tickets t JOIN notes n ON n.ticket_id = t.id
        WHERE t.created_at >= ?
        GROUP BY t.id
    """, (cutoff,)).fetchall()
    if response_times:
        diffs = []
        for row in response_times:
            try:
                t_created = datetime.fromisoformat(row[0])
                t_note = datetime.fromisoformat(row[1])
                diffs.append((t_note - t_created).total_seconds() / 3600)
            except Exception:
                pass
        avg_response = round(sum(diffs) / len(diffs), 1) if diffs else 0.0
    else:
        avg_response = 0.0

    # Ticket volume over time (group by date)
    volume_rows = db.execute("""
        SELECT DATE(created_at) as day, COUNT(*) as cnt
        FROM tickets WHERE created_at >= ?
        GROUP BY day ORDER BY day
    """, (cutoff,)).fetchall()
    volume_labels = [r[0] for r in volume_rows]
    volume_data = [r[1] for r in volume_rows]

    # Status distribution
    status_rows = db.execute("""
        SELECT status, COUNT(*) FROM tickets WHERE created_at >= ? GROUP BY status
    """, (cutoff,)).fetchall()
    status_labels = [r[0] for r in status_rows]
    status_data = [r[1] for r in status_rows]

    # Priority breakdown
    priority_rows = db.execute("""
        SELECT priority, COUNT(*) FROM tickets WHERE created_at >= ? GROUP BY priority
    """, (cutoff,)).fetchall()
    priority_labels = [r[0] for r in priority_rows]
    priority_data = [r[1] for r in priority_rows]

    # Team workload (open tickets)
    team_rows = db.execute("""
        SELECT assigned_to, COUNT(*) FROM tickets
        WHERE status NOT IN ('Closed','Resolved') AND assigned_to IS NOT NULL AND assigned_to != ''
        GROUP BY assigned_to ORDER BY COUNT(*) DESC
    """).fetchall()

    # User workload (open tickets by assigned user)
    user_rows = db.execute("""
        SELECT assigned_to, COUNT(*) FROM tickets
        WHERE status NOT IN ('Closed','Resolved') AND assigned_to IS NOT NULL AND assigned_to != ''
        GROUP BY assigned_to ORDER BY COUNT(*) DESC
    """).fetchall()

    # Unassigned count
    unassigned = db.execute("""
        SELECT COUNT(*) FROM tickets
        WHERE status NOT IN ('Closed','Resolved') AND (assigned_to IS NULL OR assigned_to='')
    """).fetchone()[0]

    tickets_list = _build_ticket_list({"status": "All", "sort": "newest", "search": "", "team": ""})
    db.close()

    filters = {"status": "Open", "sort": "newest", "search": "", "team": ""}
    return render_template("dashboard.html",
        tickets=tickets_list,
        filters=filters,
        teams=TEAMS,
        nav_counts=get_nav_counts(),
        days=days,
        total=total,
        open_count=open_count,
        avg_response=avg_response,
        resolution_rate=resolution_rate,
        volume_labels=volume_labels,
        volume_data=volume_data,
        status_labels=status_labels,
        status_data=status_data,
        priority_labels=priority_labels,
        priority_data=priority_data,
        team_rows=team_rows,
        user_rows=user_rows,
        unassigned=unassigned,
    )


@app.route("/inbox/counts")
@staff_required
def inbox_counts():
    db = get_db()
    total_open = db.execute("SELECT COUNT(*) FROM tickets WHERE status='Open'").fetchone()[0]
    total_all = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    unassigned = db.execute(
        "SELECT COUNT(*) FROM tickets WHERE status='Open' AND (assigned_to IS NULL OR assigned_to='')"
    ).fetchone()[0]
    team_counts = {}
    for team in TEAMS:
        c = db.execute(
            "SELECT COUNT(*) FROM tickets WHERE status='Open' AND assigned_to=?", (team,)
        ).fetchone()[0]
        team_counts[team] = c
    db.close()
    return jsonify({
        "total_open": total_open,
        "total_all": total_all,
        "unassigned": unassigned,
        "teams": team_counts,
    })

@app.context_processor
def inject_open_ticket_count():
    return {"open_ticket_count": 0, "staff_authenticated": session.get("staff_authenticated", False)}


if __name__ == "__main__":
    app.run(debug=True, port=5001)
