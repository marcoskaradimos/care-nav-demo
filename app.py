import os
import json
import re
from collections import defaultdict
from html.parser import HTMLParser
from flask import Flask, render_template, request, redirect, url_for, session, Response, stream_with_context, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import anthropic

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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
        {"id": "nhs_number", "label": "NHS Number",     "type": "short_text", "required": True,  "placeholder": "Enter 10-digit NHS number"},
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
        return {
            "node_id": current_id,
            "type": "end",
            "message": combined_message,
            "options": [],
            "fields": [],
            "is_end": True,
        }

    if ctype == "optionsNode":
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
            "message": combined_message or current["label"],
            "options": [],
            "fields": [],
            "is_end": False,
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
ADMIN_USER = {
    "id": "1",
    "username": "admin",
    "password_hash": generate_password_hash("admin123"),
}

class User(UserMixin):
    def __init__(self, user_id, username):
        self.id = user_id
        self.username = username

@login_manager.user_loader
def load_user(user_id):
    if user_id == ADMIN_USER["id"]:
        return User(ADMIN_USER["id"], ADMIN_USER["username"])
    return None

# ── Routes ───────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == ADMIN_USER["username"] and check_password_hash(ADMIN_USER["password_hash"], password):
            login_user(User(ADMIN_USER["id"], ADMIN_USER["username"]))
            return redirect(request.args.get("next") or url_for("index"))
        error = "Invalid username or password. Please try again."
    return render_template("login.html", error=error)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/flow/start")
@login_required
def flow_start():
    """Return the initial flow state (start node)."""
    result = build_node_response("start")
    return jsonify(result)

@app.route("/flow/step", methods=["POST"])
@login_required
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

    result = build_node_response(next_id)
    if not result:
        return jsonify({"node_id": next_id, "type": "end", "message": "Thank you for using our service.", "options": [], "fields": [], "is_end": True})

    return jsonify(result)

@app.route("/flow/ai", methods=["POST"])
@login_required
def flow_ai():
    """Stream an AI response for knowledgeBaseNode or questionNode."""
    data = request.get_json()
    node_id = data.get("node_id")
    user_message = data.get("message", "").strip()
    history = data.get("history", [])

    node = get_node(node_id) if node_id else None
    label = node["label"] if node else "medical query"

    system_prompt = (
        f"You are a helpful medical practice assistant for Access Care Navigation Agent. "
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

    def generate():
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except anthropic.AuthenticationError:
            yield f"data: {json.dumps({'error': 'API authentication failed. Please check your ANTHROPIC_API_KEY.'})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': f'An unexpected error occurred: {str(e)}'})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
