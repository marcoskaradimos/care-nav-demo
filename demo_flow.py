"""
LineIn Demo Flow
Defines all nodes for the new patient journey.
Each node is a dict that the frontend can render directly.
"""

# ── Node helpers ──────────────────────────────────────────────────────────────

def msg(node_id, message, next_id):
    return {"node_id": node_id, "type": "message", "message": message,
            "next_id": next_id, "options": [], "fields": []}

def opts(node_id, message, options):
    """options = list of {"id": ..., "label": ...}"""
    return {"node_id": node_id, "type": "options", "message": message,
            "options": options, "fields": [], "next_id": None}

def form(node_id, title, message, fields, next_id):
    return {"node_id": node_id, "type": "customFormNode", "label": title,
            "message": message, "fields": fields, "options": [], "next_id": next_id}

def input_node(node_id, prompt, placeholder, next_id):
    return {"node_id": node_id, "type": "inputNode", "message": prompt,
            "placeholder": placeholder, "options": [], "fields": [], "next_id": next_id}

def end(node_id, message):
    return {"node_id": node_id, "type": "end", "message": message,
            "options": [], "fields": [], "is_end": True}

# ── Shared field sets ─────────────────────────────────────────────────────────

CONTACT_FIELDS = [
    {"id": "first_name",  "label": "First Name",    "type": "short_text", "required": True,  "placeholder": ""},
    {"id": "last_name",   "label": "Last Name",      "type": "short_text", "required": True,  "placeholder": ""},
    {"id": "dob",         "label": "Date of Birth",  "type": "date",       "required": True,  "placeholder": ""},
    {"id": "phone",       "label": "Phone Number",   "type": "short_text", "required": True,  "placeholder": "e.g. 07700 900000"},
    {"id": "postcode",    "label": "Postcode",        "type": "short_text", "required": True,  "placeholder": "e.g. SW1A 1AA"},
    {"id": "nhs_number",  "label": "NHS Number",     "type": "short_text", "required": False, "placeholder": "Optional"},
]

# ── Flow nodes ────────────────────────────────────────────────────────────────

NODES = {}

# Welcome
NODES["start"] = msg(
    "start",
    "Hello! Welcome. I'm your digital assistant. How can I help you today?",
    "main_menu"
)

# Main Menu
NODES["main_menu"] = opts("main_menu", "", [
    {"id": "opt_appointment",  "label": "Appointment"},
    {"id": "opt_symptoms",     "label": "Symptom search"},
    {"id": "opt_repeat_med",   "label": "Repeat prescription"},
    {"id": "opt_admin",        "label": "Administration query"},
    {"id": "opt_med_cert",     "label": "Medical Certificate"},
    {"id": "opt_test_results", "label": "Test result"},
])

# ── APPOINTMENT PATH ──────────────────────────────────────────────────────────

NODES["appointment_type"] = opts("appointment_type", "What type of appointment would you like to book?", [
    {"id": "opt_nurse",  "label": "Nurse Appointment"},
    {"id": "opt_doctor", "label": "Doctor Appointment"},
])

# Nurse path
NODES["nurse_input"] = input_node(
    "nurse_input",
    "What type of nurse appointment are you looking for?",
    "e.g. blood pressure check, dressing change, vaccination...",
    "nurse_confirmation"
)

NODES["nurse_confirmation"] = end(
    "nurse_confirmation",
    "✅ Thank you! Your nurse appointment request has been received. A member of our team will be in touch shortly to confirm your booking."
)

# Doctor path
NODES["doctor_input"] = input_node(
    "doctor_input",
    "What symptom or condition would you like to see the doctor about?",
    "e.g. sore throat, back pain, skin rash...",
    "doctor_pharmacy_check"
)

# doctor_pharmacy_check is handled dynamically in app.py
# Routes to pharmacy_referral_result or directly to doctor_symptom_form

NODES["pharmacy_referral_result"] = {
    "node_id": "pharmacy_referral_result",
    "type": "pharmacy_referral",
    "message": "",   # filled dynamically
    "options": [
        {"id": "opt_pharmacy_accept", "label": "Find my nearest pharmacy"},
        {"id": "opt_pharmacy_decline", "label": "I'd still like to see a doctor"},
    ],
    "fields": [],
    "next_id": None
}

NODES["doctor_symptom_form"] = form(
    "doctor_symptom_form",
    "About Your Symptoms",
    "Please tell us more about what you're experiencing.",
    [
        {"id": "symptom_duration",  "label": "How long have you had this symptom?", "type": "select", "required": True,
         "options": ["Select one", "Less than 24 hours", "1–3 days", "4–7 days", "1–2 weeks", "More than 2 weeks"]},
        {"id": "symptom_severity",  "label": "How severe is it? (1 = mild, 10 = severe)", "type": "select", "required": True,
         "options": ["Select one", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]},
        {"id": "symptom_history",   "label": "Any relevant medical history or current medications?", "type": "long_text", "required": False, "placeholder": "Optional"},
        {"id": "preferred_time",    "label": "Preferred appointment time", "type": "select", "required": True,
         "options": ["Select one", "Morning (8am–12pm)", "Afternoon (12pm–4pm)", "Late afternoon (4pm–6:30pm)", "Any time"]},
    ],
    "doctor_confirmation"
)

NODES["doctor_confirmation"] = end(
    "doctor_confirmation",
    "✅ Thank you! Your doctor appointment request has been received. A member of our team will review your request and be in touch to confirm your booking."
)

# ── SYMPTOM SEARCH PATH ───────────────────────────────────────────────────────

NODES["symptom_search_input"] = input_node(
    "symptom_search_input",
    "What symptom or condition would you like to look up?",
    "e.g. headache, chest pain, rash...",
    "symptom_search_results"
)

# symptom_search_results is handled dynamically in app.py

NODES["symptom_after_results"] = opts("symptom_after_results", "What would you like to do next?", [
    {"id": "opt_self_care",   "label": "Self-care advice"},
    {"id": "opt_book_from_symptom", "label": "Book an appointment"},
])

NODES["symptom_self_care"] = end(
    "symptom_self_care",
    "For self-care advice, please visit the NHS page linked above. If your symptoms worsen or do not improve within 48 hours, please contact us to book an appointment."
)

# opt_book_from_symptom → doctor_symptom_form

# ── REPEAT MEDICATION PATH ────────────────────────────────────────────────────

NODES["repeat_med_info"] = opts(
    "repeat_med_info",
    "For repeat prescriptions, the quickest way is through the **NHS App**. You can order your repeat medication 24/7 without needing to contact the surgery.\n\nWould you like to use the NHS App or submit a request here?",
    [
        {"id": "opt_nhs_app",      "label": "Use the NHS App", "url": "https://www.nhsapp.service.nhs.uk/"},
        {"id": "opt_repeat_request", "label": "Submit a request here"},
    ]
)

NODES["repeat_med_request_form"] = form(
    "repeat_med_request_form",
    "Repeat Prescription Request",
    "Please provide details of the medication you need.",
    [
        {"id": "medication_name",    "label": "Medication name(s)", "type": "long_text", "required": True, "placeholder": "e.g. Metformin 500mg, Ramipril 5mg"},
        {"id": "last_prescribed",    "label": "When were you last prescribed this?", "type": "select", "required": True,
         "options": ["Select one", "Within the last month", "1–3 months ago", "3–6 months ago", "Over 6 months ago"]},
        {"id": "pharmacy_preference","label": "Preferred pharmacy (optional)", "type": "short_text", "required": False, "placeholder": "e.g. Boots, Lloyds Pharmacy"},
    ],
    "repeat_med_confirmation"
)

NODES["repeat_med_confirmation"] = end(
    "repeat_med_confirmation",
    "✅ Thank you! Your repeat prescription request has been submitted to our pharmacist team. You will be notified once it has been processed, usually within 2 working days."
)

# ── ADMIN QUERY PATH ──────────────────────────────────────────────────────────

NODES["admin_query_form"] = form(
    "admin_query_form",
    "Admin Query",
    "Please describe your query below.",
    [
        {"id": "query_type",    "label": "Type of query", "type": "select", "required": True,
         "options": ["Select one", "Change of address", "Change of contact details", "GP registration", "Referral query", "Sick note / fit note", "Other"]},
        {"id": "query_details", "label": "Please provide details", "type": "long_text", "required": True, "placeholder": "Describe your query..."},
    ],
    "admin_confirmation"
)

NODES["admin_confirmation"] = end(
    "admin_confirmation",
    "✅ Thank you! Your admin query has been received and passed to our administration team. We aim to respond within 2 working days."
)

# ── MEDICAL CERTIFICATE PATH ──────────────────────────────────────────────────

NODES["med_cert_request_form"] = form(
    "med_cert_request_form",
    "Medical Certificate Request",
    "Please provide details for your medical certificate.",
    [
        {"id": "cert_type",       "label": "Type of certificate", "type": "select", "required": True,
         "options": ["Select one", "Fit note (sick note)", "Private sick note", "Medical certificate for work", "Medical certificate for insurance", "Other"]},
        {"id": "cert_start_date", "label": "Start date required", "type": "date", "required": True},
        {"id": "cert_reason",     "label": "Reason for certificate", "type": "long_text", "required": True, "placeholder": "Brief description of your condition..."},
        {"id": "employer_name",   "label": "Employer / institution name (if applicable)", "type": "short_text", "required": False},
    ],
    "med_cert_confirmation"
)

NODES["med_cert_confirmation"] = end(
    "med_cert_confirmation",
    "✅ Thank you! Your medical certificate request has been submitted. A GP will review your request and the certificate will be ready within 2–3 working days."
)

# ── TEST RESULTS PATH ─────────────────────────────────────────────────────────

NODES["test_results_form"] = form(
    "test_results_form",
    "Test Result Enquiry",
    "Please provide details about the test result you are enquiring about.",
    [
        {"id": "test_type", "label": "Type of test", "type": "select", "required": True,
         "options": ["Select one", "Blood test", "Urine test", "X-ray / Scan", "Biopsy", "ECG", "Smear test", "Other"]},
        {"id": "test_date", "label": "Approximate date of test", "type": "date", "required": True},
        {"id": "test_query", "label": "What would you like to know?", "type": "select", "required": True,
         "options": ["Select one", "I haven't received my results yet", "I'd like to understand my results", "I have concerns about my results", "Other"]},
        {"id": "test_details", "label": "Additional details (optional)", "type": "long_text", "required": False,
         "placeholder": "Any additional information..."},
    ],
    "test_results_confirmation"
)

NODES["test_results_confirmation"] = end(
    "test_results_confirmation",
    "✅ Thank you! Your test result enquiry has been received. A member of our team will review your request and be in touch within 2 working days."
)


# ── Option routing table ──────────────────────────────────────────────────────
# Maps (current_node_id, option_id) → next_node_id

OPTION_ROUTES = {
    # Main menu
    ("main_menu",            "opt_appointment"):       "appointment_type",
    ("main_menu",            "opt_symptoms"):          "symptom_search_input",
    ("main_menu",            "opt_repeat_med"):        "repeat_med_info",
    ("main_menu",            "opt_admin"):             "admin_query_form",
    ("main_menu",            "opt_med_cert"):          "med_cert_request_form",
    ("main_menu",            "opt_test_results"):      "test_results_form",

    # Appointment type
    ("appointment_type",     "opt_nurse"):             "nurse_input",
    ("appointment_type",     "opt_doctor"):            "doctor_input",

    # Pharmacy referral
    ("pharmacy_referral_result", "opt_pharmacy_accept"):  "pharmacy_finder",
    ("pharmacy_referral_result", "opt_pharmacy_decline"): "doctor_symptom_form",

    # Symptom search after results
    ("symptom_after_results", "opt_self_care"):             "symptom_self_care",
    ("symptom_after_results", "opt_book_from_symptom"):     "doctor_symptom_form",

    # Symptom search book appointment
    ("symptom_after_results", "opt_book_from_symptom"): "doctor_symptom_form",

    # Repeat med
    ("repeat_med_info",      "opt_repeat_request"):    "repeat_med_request_form",

    # Back to main from anywhere
    ("test_results_info",    "opt_back"):              "main_menu",
}


def get_node(node_id):
    return NODES.get(node_id)


def next_node_for_option(current_node_id, option_id):
    return OPTION_ROUTES.get((current_node_id, option_id))


# ── Ticket inbox mapping ──────────────────────────────────────────────────────

CONFIRMATION_INBOXES = {
    "nurse_confirmation":        ("Nurse Appointment",   "Nurse Appointment"),
    "doctor_confirmation":       ("Doctor Appointment",  "Clinical Triage"),
    "repeat_med_confirmation":   ("Repeat Prescription", "Pharmacist"),
    "admin_confirmation":        ("Admin Query",          "Admin"),
    "med_cert_confirmation":     ("Medical Certificate",  "Medical Certificate"),
    "test_results_confirmation": ("Test Result Enquiry",  "Test Results"),
}
