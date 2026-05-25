// ===== State =====
let symptomDescription = "";
let currentNodeId = null;
let currentNodeType = null;
let currentNodeData = null; // full node object for back navigation
let aiHistory = [];
let isStreaming = false;
let isAiMode = false;
let formHistory = [];
let previousOptionsState = null; // { options } saved before a form is shown
let nodeHistory = []; // stack of previous node data objects for back navigation

// ===== DOM =====
const chatMessages     = document.getElementById("chatMessages");
const chatInput        = document.getElementById("chatInput");
const sendBtn          = document.getElementById("sendBtn");
const buttonGrid       = document.getElementById("buttonGrid");
const buttonPanelLabel = document.getElementById("buttonPanelLabel");
const contextLabel     = document.getElementById("contextLabel");
const buttonPanel      = document.getElementById("buttonPanel");
const formPanel        = document.getElementById("formPanel");
const landingScreen    = document.getElementById("landingScreen");
const getStartedBtn    = document.getElementById("getStartedBtn");

// ===== Patient Details =====
let patientDetails = {};
let proxyMode = false;

function showPatientDetailsForm() {
    landingScreen.style.display = "none";
    const sidebar = document.querySelector(".sidebar");
    if (sidebar) sidebar.style.display = "none";
    document.getElementById("patientDetailsScreen").style.display = "";
}


function setWho(mode) {
    proxyMode = (mode === "proxy");
    document.getElementById("pdWhoMyself").classList.toggle("active", !proxyMode);
    document.getElementById("pdWhoProxy").classList.toggle("active", proxyMode);
    const proxyFields = document.getElementById("pdProxyFields");
    if (proxyFields) proxyFields.style.display = proxyMode ? "" : "none";
    ["pd-proxy-first", "pd-proxy-last", "pd-relationship"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.required = proxyMode;
    });
}

async function submitPatientDetails(e) {
    e.preventDefault();
    const form = document.getElementById("patientDetailsForm");
    const data = new FormData(form);

    const firstName  = (data.get("first_name") || "").trim();
    const lastName   = (data.get("last_name") || "").trim();
    const dob        = data.get("dob") || "";
    const phone      = (data.get("phone") || "").trim();
    const nhsNumber  = (data.get("nhs_number") || "").trim();
    const postcode   = (data.get("postcode") || "").trim();
    const fullName   = `${firstName} ${lastName}`.trim();

    // Try to match / create session
    const payload = { first_name: firstName, last_name: lastName, dob, phone, nhs_number: nhsNumber, postcode };
    let matchedPhone = phone, matchedPostcode = postcode, matchedNhs = nhsNumber, matchedPractice = "";
    try {
        const matchResp = await fetch("/patient/match", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const matchData = await matchResp.json();
        if (matchData.status === "matched" && matchData.patient) {
            matchedPhone    = matchData.patient.phone    || phone;
            matchedPostcode = matchData.patient.postcode || postcode;
            matchedNhs      = matchData.patient.nhs      || nhsNumber;
            matchedPractice = matchData.patient.practice || "";
        }
    } catch (err) { /* non-blocking */ }

    const proxyFirst    = (data.get("proxy_first_name") || "").trim();
    const proxyLast     = (data.get("proxy_last_name")  || "").trim();
    const relationship  = (data.get("relationship")     || "").trim();

    patientDetails = {
        name: fullName, first_name: firstName, last_name: lastName,
        dob, phone: matchedPhone, nhs_number: matchedNhs, postcode: matchedPostcode, practice: matchedPractice,
        proxy_first_name: proxyFirst, proxy_last_name: proxyLast, relationship,
        is_proxy: proxyMode,
    };

    // Save proxy info to session via match endpoint
    if (proxyMode && proxyFirst) {
        try {
            await fetch("/patient/set_proxy", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ proxy_first_name: proxyFirst, proxy_last_name: proxyLast, relationship }),
            });
        } catch (err) { /* non-blocking */ }
    }

    _proceedToChat(fullName, dob, matchedPhone || "—", matchedNhs || "—", matchedPostcode || "—", matchedPractice || "—", proxyMode);
}

function _proceedToChat(name, dob, phone, nhs, postcode, practice, isProxy) {
    document.getElementById("phName").textContent     = name || "—";
    document.getElementById("phDob").textContent      = formatDob(dob);
    document.getElementById("phPhone").textContent    = phone || "—";
    document.getElementById("phNhs").textContent      = nhs || "—";
    document.getElementById("phPostcode").textContent = postcode || "—";
    document.getElementById("phPractice").textContent = practice || "—";
    const badge = document.getElementById("phProxyBadge");
    if (badge) badge.style.display = isProxy ? "inline-block" : "none";

    // Show header bar, hide registration screen, show chat
    document.getElementById("patientHeaderBar").style.display = "grid";
    document.getElementById("patientDetailsScreen").style.display = "none";
    document.getElementById("landingScreen").style.display = "none";
    chatMessages.style.display = "flex";

    // Restore sidebar
    const sidebar = document.querySelector(".sidebar");
    if (sidebar) sidebar.style.display = "";

    startFlow();
}

function formatDob(dob) {
    if (!dob) return "—";
    if (dob.includes("-")) {
        const parts = dob.split("-");
        if (parts.length === 3) return `${parts[2]}/${parts[1]}/${parts[0]}`;
    }
    return dob;
}

// ===== Init =====
document.addEventListener("DOMContentLoaded", () => {
    setupInputHandlers();

    // Hide chat input bar by default — only shown for free-text nodes
    document.getElementById("chatInputArea").style.display = "none";

    getStartedBtn.addEventListener("click", showPatientDetailsForm);
    const detailsForm = document.getElementById("patientDetailsForm");
    if (detailsForm) detailsForm.addEventListener("submit", submitPatientDetails);

    // Auto-start if arriving from staff triage (session already populated server-side)
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get("staff_triage") === "1" && window.STAFF_TRIAGE_PATIENT) {
        const p = window.STAFF_TRIAGE_PATIENT;
        proxyMode = p.is_proxy;
        patientDetails = {
            name: p.name, first_name: p.first_name, last_name: p.last_name,
            dob: p.dob, phone: p.phone, nhs_number: p.nhs, postcode: p.postcode,
            practice: p.practice,
            proxy_first_name: p.proxy_first, proxy_last_name: p.proxy_last,
            relationship: p.proxy_relationship, is_proxy: p.is_proxy,
        };
        _proceedToChat(p.name, p.dob, p.phone || "—", p.nhs || "—", p.postcode || "—", p.practice || "—", p.is_proxy);
    }
});

function setupInputHandlers() {
    chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendUserMessage();
        }
    });
    chatInput.addEventListener("input", () => {
        chatInput.style.height = "auto";
        chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + "px";
    });
}

// ===== Start Flow =====
async function startFlow() {
    try {
        const res = await fetch("/flow/start");
        const data = await res.json();
        renderFlowNode(data);
    } catch (err) {
        appendAssistantMessage("Sorry, unable to load the navigation flow. Please refresh.");
    }
}

// ===== Render a flow node =====
function renderFlowNode(node, skipHistory = false) {
    if (!node) return;

    // Save current node to history before moving forward
    if (!skipHistory && currentNodeId && currentNodeId !== node.node_id && currentNodeData) {
        nodeHistory.push(currentNodeData);
    }

    currentNodeId   = node.node_id;
    currentNodeType = node.type;
    currentNodeData = node;
    isAiMode        = (node.type === "ai" || node.type === "question");

    // Show message
    if (node.message) {
        appendAssistantMessage(node.message);
    }

    // Update sidebar label
    const labelMap = {
        "options":          "Please choose an option",
        "customFormNode":   "Please fill in the form",
        "form":             "Please fill in the form",
        "inputNode":        "Type your response",
        "ai":               "AI Assistant",
        "question":         "Please answer",
        "end":              "Complete",
    };
    updateContextLabel(labelMap[node.type] || "Navigation");

    // Hide both panels first
    hideFormPanel();
    clearButtonGrid();

    if (node.is_end) {
        showEndButtons();
        setInputEnabled(false);
        scrollToPanel();
        return;
    }

    // Custom form node (demo flow forms)
    if (node.type === "customFormNode" && node.fields && node.fields.length > 0) {
        showFormPanel(node.fields, node.node_id, {}, node.label || "");
        setInputEnabled(false);
        scrollToPanel();
        return;
    }

    if (node.type === "form") {
        showFormPanel(node.fields, node.node_id, {}, node.form_title || "");
        setInputEnabled(false);
        scrollToPanel();
        return;
    }

    // Input node — show text input at bottom, no button panel
    if (node.type === "inputNode") {
        currentNodeType = "inputNode";
        clearButtonGrid();
        buttonPanel.style.display = "none";
        document.getElementById("chatInputArea").style.display = "";
        chatInput.placeholder = node.placeholder || "Type your response...";
        setInputEnabled(true);
        chatInput.focus();
        scrollToBottom();
        return;
    }

    if (node.type === "options") {
        previousOptionsState = { options: node.options };
        showInlineOptions(node.options);
        setInputEnabled(false);
        scrollToBottom();
        return;
    }

    // Pharmacy referral — message + options
    if (node.type === "pharmacy_referral") {
        if (node.options && node.options.length > 0) {
            previousOptionsState = { options: node.options };
            showInlineOptions(node.options);
            setInputEnabled(false);
        }
        scrollToBottom();
        return;
    }

    // Pharmacy finder — trigger nearby search
    if (node.type === "pharmacy_finder") {
        showNearbyPharmacies();
        showEndButtons();
        setInputEnabled(false);
        scrollToPanel();
        return;
    }

    if (node.type === "ai" || node.type === "question") {
        buttonPanelLabel.textContent = "Type your response below:";
        showBackButton();
        setInputEnabled(true);
        chatInput.focus();
        aiHistory = [];
        scrollToPanel();
        return;
    }

    // message node with options (symptom results)
    if (node.type === "message" && node.options && node.options.length > 0) {
        previousOptionsState = { options: node.options };
        showInlineOptions(node.options);
        setInputEnabled(false);
        scrollToBottom();
        return;
    }

    // Auto-advance message node (either has auto_advance flag or has next_id and no options)
    if (node.type === "message" && node.next_id && (!node.options || node.options.length === 0)) {
        setTimeout(() => advanceFromMessage(node), 800);
        return;
    }

    // Fallback
    setInputEnabled(true);
}

async function advanceFromMessage(node) {
    try {
        const res = await fetch("/flow/step", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ node_id: node.node_id, option_handle: "" }),
        });
        const data = await res.json();
        renderFlowNode(data);
    } catch (err) { /* ignore */ }
}

// ===== Inline options (rendered inside chat messages) =====
function showInlineOptions(options) {
    clearButtonGrid();
    buttonPanel.style.display = "none";

    const wrapper = document.createElement("div");
    wrapper.className = "inline-options-wrapper";

    options.forEach(opt => {
        if (!opt.label) return;
        const btn = document.createElement("button");
        btn.className = "inline-option-btn" + (opt.id === "opt_back" || opt.label.toLowerCase().includes("back") ? " inline-option-back" : "");
        btn.textContent = opt.label;

        if (opt.url) {
            btn.addEventListener("click", () => window.open(opt.url, "_blank"));
        } else if (opt.label.toLowerCase().includes("back") && opt.label.toLowerCase().includes("main")) {
            btn.addEventListener("click", goBack);
        } else {
            btn.addEventListener("click", () => {
                // Remove the options wrapper after selection
                wrapper.remove();
                handleOptionClick(opt.id, opt.label);
            });
        }
        wrapper.appendChild(btn);
    });

    // Only show Back button if NOT on main menu
    if (currentNodeId !== "main_menu") {
        const backBtn = document.createElement("button");
        backBtn.className = "inline-option-btn inline-option-back";
        backBtn.textContent = "← Back";
        backBtn.addEventListener("click", () => { wrapper.remove(); goBack(); });
        wrapper.appendChild(backBtn);
    }

    chatMessages.appendChild(wrapper);
    scrollToBottom();
}

function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// ===== Option Buttons =====
function showOptionButtons(options) {
    buttonPanel.style.display = "";
    buttonGrid.innerHTML = "";
    scrollToPanel();

    const ICON_MAP = {
        "appointments":           "📅",
        "medical certificates":   "📋",
        "register":               "📝",
        "medicine":               "💊",
        "test results":           "🔬",
        "general enquiry":        "💬",
        "symptoms":               "🩺",
        "check my symptoms":      "🩺",
        "ask a question":         "❓",
        "dental":                 "🦷",
        "prescription":           "💉",
        "repeat prescription":    "🔄",
        "mental health":          "🧠",
        "crisis":                 "🚨",
        "register online":        "🌐",
        "call":                   "📞",
        "online":                 "💻",
        "back":                   "←",
        "nurse":                  "👩‍⚕️",
        "follow up":              "📆",
        "blood test":             "🩸",
        "emergency":              "🚨",
        "find a dentist":         "🔍",
        "yes":                    "✅",
        "no":                     "❌",
        "submit":                 "📤",
        "nhs app":                "📱",
    };

    function getIcon(label) {
        const lower = label.toLowerCase();
        for (const [key, icon] of Object.entries(ICON_MAP)) {
            if (lower.includes(key)) return icon;
        }
        return "›";
    }

    // Only add back button if the flow doesn't already include one
    const hasBackOption = options.some(o => o.label && o.label.toLowerCase().includes("back") && o.label.toLowerCase().includes("main"));

    options.forEach(opt => {
        if (!opt.label) return;
        const btn = document.createElement("button");
        btn.className = "menu-btn";
        btn.textContent = opt.label;

        if (opt.url) {
            btn.addEventListener("click", () => window.open(opt.url, "_blank"));
        } else if (opt.label.toLowerCase().includes("back") && opt.label.toLowerCase().includes("main")) {
            btn.addEventListener("click", resetToMainMenu);
        } else {
            btn.addEventListener("click", () => handleOptionClick(opt.id, opt.label));
        }
        buttonGrid.appendChild(btn);
    });

    // Only add back button if the flow doesn't already have one
    if (!hasBackOption) {
        addBackButtonToGrid();
    }
}

function addBackButtonToGrid() {
    const btn = document.createElement("button");
    btn.className = "menu-btn back-btn";
    btn.textContent = "← Back";
    btn.addEventListener("click", goBack);
    buttonGrid.appendChild(btn);
}

function showBackButton() {
    const wrapper = document.createElement("div");
    wrapper.className = "inline-options-wrapper";
    const btn = document.createElement("button");
    btn.className = "inline-option-btn inline-option-back";
    btn.textContent = "← Back";
    btn.addEventListener("click", () => { wrapper.remove(); goBack(); });
    wrapper.appendChild(btn);
    chatMessages.appendChild(wrapper);
    scrollToBottom();
}

function showEndButtons() {
    const wrapper = document.createElement("div");
    wrapper.className = "inline-options-wrapper";
    const btn = document.createElement("button");
    btn.className = "inline-option-btn";
    btn.textContent = "← Return to Main Menu";
    btn.addEventListener("click", () => { wrapper.remove(); goBack(); });
    wrapper.appendChild(btn);
    chatMessages.appendChild(wrapper);
    scrollToBottom();
}

function clearButtonGrid() {
    buttonGrid.innerHTML = "";
    buttonPanel.style.display = "none";
}

// ===== Handle option click =====
async function handleOptionClick(optionHandle, optionLabel) {
    if (isStreaming) return;

    appendUserMessage(optionLabel);
    clearButtonGrid();
    setInputEnabled(false);
    appendTypingIndicator();

    try {
        const res = await fetch("/flow/step", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ node_id: currentNodeId, option_handle: optionHandle }),
        });
        const data = await res.json();
        removeTypingIndicator();
        renderFlowNode(data);
    } catch (err) {
        removeTypingIndicator();
        appendAssistantMessage("Sorry, something went wrong. Please try again.");
        showBackButton();
    }
}

// ===== Handle free-text send =====
function sendUserMessage() {
    const text = chatInput.value.trim();
    if (!text || isStreaming) return;
    chatInput.value = "";
    chatInput.style.height = "auto";

    if (isAiMode) {
        streamAiResponse(text);
    } else if (currentNodeType === "inputNode") {
        handleInputNodeSubmit(text);
    } else {
        handleFlowTextInput(text);
    }
}

async function handleInputNodeSubmit(text) {
    appendUserMessage(text);
    setInputEnabled(false);
    clearButtonGrid();
    appendTypingIndicator();

    try {
        const res = await fetch("/flow/step", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ node_id: currentNodeId, option_handle: "", user_input: text }),
        });
        const data = await res.json();
        removeTypingIndicator();
        renderFlowNode(data);
    } catch (err) {
        removeTypingIndicator();
        appendAssistantMessage("Sorry, something went wrong. Please try again.");
        showBackButton();
    }
}

async function handleFlowTextInput(text) {
    appendUserMessage(text);
    setInputEnabled(false);
    appendTypingIndicator();

    try {
        const res = await fetch("/flow/step", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ node_id: currentNodeId, option_handle: "", user_input: text }),
        });
        const data = await res.json();
        removeTypingIndicator();
        renderFlowNode(data);
    } catch (err) {
        removeTypingIndicator();
        appendAssistantMessage("Sorry, something went wrong. Please try again.");
    }
}

// ===== AI Streaming =====
async function streamAiResponse(userText, skipUserMessage = false) {
    if (isStreaming) return;
    isStreaming = true;

    if (!skipUserMessage) appendUserMessage(userText);
    aiHistory.push({ role: "user", content: userText });
    setInputEnabled(false);

    const typingEl = appendTypingIndicator();

    try {
        const res = await fetch("/flow/ai", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                node_id: currentNodeId,
                message: userText,
                history: aiHistory.slice(-20),
            }),
        });

        typingEl.remove();
        const { bubble } = createAssistantBubble();

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let fullText = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop();
            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                const raw = line.slice(6).trim();
                if (raw === "[DONE]") break;
                try {
                    const parsed = JSON.parse(raw);
                    if (parsed.error) {
                        bubble.classList.add("error");
                        bubble.textContent = parsed.error;
                        fullText = parsed.error;
                    } else if (parsed.text) {
                        fullText += parsed.text;
                        renderMarkdown(bubble, fullText);
                        scrollToBottom();
                    }
                } catch { /* ignore */ }
            }
        }

        if (fullText) {
            aiHistory.push({ role: "assistant", content: fullText });
        }

        // After AI response, follow flow to next node
        await advanceAfterAi();
        scrollToPanel();

    } catch (err) {
        removeTypingIndicator();
        const { bubble } = createAssistantBubble();
        bubble.classList.add("error");
        bubble.textContent = `Sorry, something went wrong: ${err.message}`;
    } finally {
        isStreaming = false;
        setInputEnabled(true);
        chatInput.focus();
    }
}

async function advanceAfterAi() {
    // Check if the current node has a next node to follow
    try {
        const res = await fetch("/flow/step", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ node_id: currentNodeId, option_handle: "" }),
        });
        const data = await res.json();
        if (data && data.type !== currentNodeType) {
            renderFlowNode(data);
        } else if (data && data.options && data.options.length > 0) {
            renderFlowNode(data);
        }
    } catch { /* stay on current node */ }
}

// ===== Form Panel =====
function captureCurrentFormData(fields) {
    const data = {};
    fields.forEach(f => {
        const el = document.getElementById("field_" + f.id);
        if (!el) return;
        if (f.type === "multi_select") {
            data[f.id] = [...el.querySelectorAll("input[type=checkbox]:checked")].map(c => c.value);
        } else if (f.type === "checkbox") {
            data[f.id] = el.checked;
        } else {
            data[f.id] = el.value;
        }
    });
    return data;
}

function showApptSymptomPanel(node) {
    if (!formPanel) return;
    formPanel.innerHTML = "";
    formPanel.style.display = "";
    document.getElementById("chatInputArea").style.display = "none";

    const wrapper = document.createElement("div");
    wrapper.className = "appt-symptom-panel";

    // Textarea
    const fieldDiv = document.createElement("div");
    fieldDiv.className = "pd-field";
    const label = document.createElement("label");
    label.textContent = "Describe your symptoms";
    label.className = "appt-symptom-label";
    const textarea = document.createElement("textarea");
    textarea.className = "appt-symptom-textarea";
    textarea.placeholder = "Please describe your symptoms or reason for appointment...";
    textarea.rows = 4;
    fieldDiv.appendChild(label);
    fieldDiv.appendChild(textarea);
    wrapper.appendChild(fieldDiv);

    // Button
    const btn = document.createElement("button");
    btn.className = "appt-symptom-btn";
    btn.textContent = "Submit Response";
    btn.addEventListener("click", async () => {
        const symptoms = textarea.value.trim();
        if (symptoms) {
            appendUserMessage(symptoms);
        }
        formPanel.style.display = "none";
        formPanel.innerHTML = "";
        document.getElementById("chatInputArea").style.display = "";
        // Advance flow using the option handle
        const optionHandle = node.options[0]?.id || "";
        try {
            const res = await fetch("/flow/step", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ node_id: node.node_id, option_handle: optionHandle, form_data: { symptom_description: symptoms } }),
            });
            const data = await res.json();
            renderFlowNode(data);
        } catch (err) {
            appendAssistantMessage("Sorry, something went wrong. Please try again.");
        }
    });
    wrapper.appendChild(btn);

    formPanel.appendChild(wrapper);
    scrollToPanel();
}

function showFormPanel(fields, nodeId, savedData = {}, formTitle = "") {
    if (!formPanel) return;

    formPanel.innerHTML = "";
    formPanel.style.display = "";
    document.getElementById("chatInputArea").style.display = "none";
    scrollToPanel();

    // Form title
    if (formTitle) {
        const titleEl = document.createElement("h2");
        titleEl.className = "form-panel-title";
        titleEl.textContent = formTitle;
        formPanel.appendChild(titleEl);
    }

    // Go Back button — always shown on forms
    const goBackBtn = document.createElement("button");
    goBackBtn.type = "button";
    goBackBtn.className = "form-go-back-btn";
    goBackBtn.textContent = "← Go Back";
    goBackBtn.addEventListener("click", () => {
        // Always go back to main menu — clears form history too
        goBack();
    });
    formPanel.appendChild(goBackBtn);

    const form = document.createElement("form");
    form.id = "flowForm";

    fields.forEach(field => {
        const group = document.createElement("div");
        group.className = "form-group";

        const label = document.createElement("label");
        label.textContent = field.label + (field.required ? " *" : "");
        label.htmlFor = "field_" + field.id;

        if (field.type === "prefilled_readonly") {
            const ta = document.createElement("textarea");
            ta.id = "field_" + field.id;
            ta.name = field.id;
            ta.className = "form-input prefilled-readonly";
            ta.rows = 3;
            ta.readOnly = true;
            ta.value = symptomDescription || "(No symptoms entered)";
            group.appendChild(label);
            group.appendChild(ta);
            form.appendChild(group);
            return;
        }

        if (field.type === "multi_select") {
            // Render as a collapsible dropdown with checkboxes inside
            const wrapper = document.createElement("div");
            wrapper.className = "multi-select-wrapper";
            wrapper.id = "field_" + field.id;

            const trigger = document.createElement("div");
            trigger.className = "multi-select-trigger";
            trigger.innerHTML = `<span class="multi-select-placeholder">Select all that apply</span><span class="multi-select-arrow">▾</span>`;

            const dropdown = document.createElement("div");
            dropdown.className = "multi-select-dropdown";
            dropdown.style.display = "none";

            const savedMulti = savedData[field.id] || [];
            (field.options || []).forEach(opt => {
                const item = document.createElement("label");
                item.className = "multi-select-item";
                const cb = document.createElement("input");
                cb.type = "checkbox";
                cb.value = opt;
                cb.name = field.id;
                if (savedMulti.includes(opt)) cb.checked = true;
                cb.addEventListener("change", () => {
                    const selected = [...dropdown.querySelectorAll("input:checked")].map(c => c.value);
                    trigger.querySelector(".multi-select-placeholder").textContent =
                        selected.length ? selected.join(", ") : "Select all that apply";
                });
                item.appendChild(cb);
                item.appendChild(document.createTextNode(opt));
                dropdown.appendChild(item);
            });
            // Restore placeholder if values were saved
            if (savedMulti.length > 0) {
                trigger.querySelector(".multi-select-placeholder").textContent = savedMulti.join(", ");
            }

            trigger.addEventListener("click", () => {
                const open = dropdown.style.display !== "none";
                dropdown.style.display = open ? "none" : "";
                trigger.classList.toggle("open", !open);
            });

            // Close when clicking outside
            document.addEventListener("click", (e) => {
                if (!wrapper.contains(e.target)) {
                    dropdown.style.display = "none";
                    trigger.classList.remove("open");
                }
            });

            wrapper.appendChild(trigger);
            wrapper.appendChild(dropdown);
            group.appendChild(label);
            group.appendChild(wrapper);
            form.appendChild(group);
            return;
        }

        let input;
        if (field.type === "textarea" || field.type === "long_text") {
            input = document.createElement("textarea");
            input.rows = 3;
        } else if (field.type === "date") {
            input = document.createElement("input");
            input.type = "date";
        } else if (field.type === "select") {
            input = document.createElement("select");
            (field.options || []).forEach(opt => {
                const o = document.createElement("option");
                o.value = opt === (field.options || [])[0] ? "" : opt;
                o.textContent = opt;
                if (opt === (field.options || [])[0]) o.disabled = true, o.selected = true;
                input.appendChild(o);
            });
        } else if (field.type === "checkbox") {
            input = document.createElement("input");
            input.type = "checkbox";
        } else if (field.type === "pain_check") {
            // Yes/No + pain slider
            const painWrapper = document.createElement("div");
            painWrapper.className = "pain-check-wrapper";
            painWrapper.id = "field_" + field.id;

            const yesNoRow = document.createElement("div");
            yesNoRow.className = "pain-yesno-row";
            ["Yes", "No"].forEach(val => {
                const lbl = document.createElement("label");
                lbl.className = "pain-yesno-label";
                const rb = document.createElement("input");
                rb.type = "radio";
                rb.name = field.id + "_yesno";
                rb.value = val;
                rb.addEventListener("change", () => {
                    scoreSection.style.display = val === "Yes" ? "" : "none";
                });
                lbl.appendChild(rb);
                lbl.appendChild(document.createTextNode(" " + val));
                yesNoRow.appendChild(lbl);
            });

            const scoreSection = document.createElement("div");
            scoreSection.className = "pain-score-section";
            scoreSection.style.display = "none";
            const scoreLabel = document.createElement("div");
            scoreLabel.className = "pain-score-label";
            scoreLabel.textContent = "Pain score: 5";
            const slider = document.createElement("input");
            slider.type = "range";
            slider.min = "1";
            slider.max = "10";
            slider.value = "5";
            slider.className = "pain-slider";
            slider.addEventListener("input", () => {
                scoreLabel.textContent = "Pain score: " + slider.value;
            });
            const tickRow = document.createElement("div");
            tickRow.className = "pain-tick-row";
            for (let i = 1; i <= 10; i++) {
                const t = document.createElement("span");
                t.textContent = i;
                tickRow.appendChild(t);
            }
            scoreSection.appendChild(scoreLabel);
            scoreSection.appendChild(slider);
            scoreSection.appendChild(tickRow);

            painWrapper.appendChild(yesNoRow);
            painWrapper.appendChild(scoreSection);

            group.appendChild(label);
            group.appendChild(painWrapper);
            form.appendChild(group);

            // Custom data capture for this field
            painWrapper._getValue = () => {
                const selected = painWrapper.querySelector(`input[name="${field.id}_yesno"]:checked`);
                if (!selected) return "";
                if (selected.value === "No") return "No pain";
                return "Yes — pain score: " + slider.value + "/10";
            };
            return;
        } else {
            input = document.createElement("input");
            input.type = "text";
        }
        input.id = "field_" + field.id;
        input.name = field.id;
        if (field.type !== "select" && field.type !== "checkbox") input.className = "form-input";
        else if (field.type === "select") input.className = "form-input";
        if (field.required) input.required = true;
        if (field.placeholder) input.placeholder = field.placeholder;
        if (field.maxlength) input.maxLength = field.maxlength;
        if (field.inputmode) input.inputMode = field.inputmode;
        // For numeric-only fields, block non-digit keypresses
        if (field.inputmode === "numeric") {
            input.addEventListener("keypress", e => {
                if (!/\d/.test(e.key)) e.preventDefault();
            });
            input.addEventListener("input", () => {
                input.value = input.value.replace(/\D/g, "");
            });
        }
        // Restore saved value
        if (savedData[field.id] !== undefined) {
            if (field.type === "checkbox") input.checked = savedData[field.id];
            else input.value = savedData[field.id];
        }

        group.appendChild(label);
        group.appendChild(input);
        form.appendChild(group);
    });

    const submitBtn = document.createElement("button");
    submitBtn.type = "submit";
    submitBtn.className = "form-submit-btn";
    submitBtn.textContent = "Submit";
    form.appendChild(submitBtn);

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        // Validate NHS number exactly 10 digits
        const nhsEl = form.querySelector("input[name=nhs_number]");
        if (nhsEl && nhsEl.value.replace(/\D/g, "").length !== 10) {
            nhsEl.setCustomValidity("NHS Number must be exactly 10 digits.");
            nhsEl.reportValidity();
            return;
        }
        if (nhsEl) nhsEl.setCustomValidity("");
        const formData = {};
        fields.forEach(f => {
            const el = document.getElementById("field_" + f.id);
            if (!el) return;
            if (f.type === "multi_select") {
                const checked = [...el.querySelectorAll("input[type=checkbox]:checked")].map(c => c.value);
                formData[f.id] = checked.join(", ") || "None selected";
            } else if (f.type === "checkbox") {
                formData[f.id] = el.checked ? "Yes" : "No";
            } else if (f.type === "pain_check" || el._getValue) {
                formData[f.id] = el._getValue ? el._getValue() : el.value;
            } else {
                formData[f.id] = el.value;
            }
        });

        // Show summary message
        const summary = Object.entries(formData)
            .filter(([k, v]) => v)
            .map(([k, v]) => {
                const field = fields.find(f => f.id === k);
                return `${field ? field.label : k}: ${v}`;
            }).join("\n");
        appendUserMessage(summary || "Form submitted");

        // Build combined form data: patient details + all previous forms + current form
        const allFormData = {};
        if (patientDetails) Object.assign(allFormData, {
            patient_name: patientDetails.name || "",
            dob: patientDetails.dob || "",
            phone: patientDetails.phone || "",
            nhs_number: patientDetails.nhs_number || "",
            postcode: patientDetails.postcode || "",
        });
        if (symptomDescription) allFormData.symptom_description = symptomDescription;
        formHistory.forEach(h => {
            if (h.savedData) Object.assign(allFormData, h.savedData);
        });
        Object.assign(allFormData, formData);

        // Save this form (with raw captured data for field restoration) to history
        formHistory.push({ fields, nodeId, savedData: captureCurrentFormData(fields), formTitle });
        hideFormPanel();
        appendTypingIndicator();

        try {
            const res = await fetch("/flow/step", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ node_id: nodeId, option_handle: "", form_data: formData, all_form_data: allFormData }),
            });
            const data = await res.json();
            removeTypingIndicator();
            // Auto-trigger AI differential diagnosis if next node requests it
            if (data.type === "ai" && data.auto_trigger && formData.symptom_description) {
                symptomDescription = formData.symptom_description;
                currentNodeId = data.node_id;
                currentNodeType = "ai";
                isAiMode = true;
                if (data.message) appendAssistantMessage(data.message);
                streamAiResponse(formData.symptom_description, true);
            } else {
                renderFlowNode(data);
            }
            // After pharmacist form, find nearby pharmacies then show booking calendar
            if (nodeId === "local_pharmacist_form") {
                await showNearbyPharmacies();
                showBookingCalendar();
            }
        } catch (err) {
            removeTypingIndicator();
            appendAssistantMessage("Your form was submitted. Thank you!");
            showEndButtons();
        }
    });

    formPanel.appendChild(form);
}

// ===== Booking Calendar =====
function showBookingCalendar() {
    const wrapper = document.createElement("div");
    wrapper.className = "message assistant-message";
    wrapper.innerHTML = `
        <div class="booking-calendar-wrapper">
            <p class="booking-calendar-intro">To complete your referral, please book an appointment with the pharmacist:</p>
            <a href="https://calendar.app.google/gnqrEkLNFWgPvErP7" target="_blank" class="booking-calendar-btn">
                <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                    <rect x="1" y="3" width="16" height="14" rx="2" stroke="currentColor" stroke-width="1.5"/>
                    <path d="M1 7h16" stroke="currentColor" stroke-width="1.5"/>
                    <path d="M5 1v4M13 1v4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
                View Available Appointment Times
            </a>
            <p class="booking-calendar-note">Opens your pharmacist's booking calendar in a new tab — select a time that suits you.</p>
        </div>`;
    chatMessages.appendChild(wrapper);
    wrapper.scrollIntoView({ behavior: "smooth", block: "end" });
}

// ===== Pharmacy Finder =====
async function showNearbyPharmacies() {
    appendTypingIndicator();
    try {
        const pos = await new Promise((resolve, reject) =>
            navigator.geolocation.getCurrentPosition(resolve, reject, { timeout: 8000 })
        );
        const { latitude: lat, longitude: lng } = pos.coords;

        const res = await fetch("/pharmacy/nearby", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ lat, lng }),
        });
        const data = await res.json();
        removeTypingIndicator();

        const pharmacies = data.pharmacies || [];
        if (!pharmacies.length) {
            appendAssistantMessage("We couldn't find any Dischem or Clicks pharmacies near you. Please search on [Google Maps](https://www.google.com/maps/search/pharmacy+near+me).");
            return;
        }

        // Group by brand
        const brands = {};
        for (const p of pharmacies) {
            if (!brands[p.brand]) brands[p.brand] = [];
            brands[p.brand].push(p);
        }

        let html = `<div class="pharmacy-results"><p class="pharmacy-intro">Here are the nearest pharmacies to you:</p>`;
        for (const [brand, list] of Object.entries(brands)) {
            html += `<div class="pharmacy-brand-group"><h4 class="pharmacy-brand-title">${brand}</h4>`;
            for (const p of list) {
                const mapsUrl = `https://www.google.com/maps/place/?q=place_id:${p.place_id}`;
                const directionsUrl = `https://www.google.com/maps/dir/?api=1&destination=${p.lat},${p.lng}&destination_place_id=${p.place_id}`;
                const openStatus = p.open_now === true ? '<span class="pharm-open">Open now</span>' : p.open_now === false ? '<span class="pharm-closed">Closed</span>' : '';
                const rating = p.rating ? `⭐ ${p.rating}` : '';
                const phone = p.phone ? `<a href="tel:${p.phone}" class="pharm-phone">📞 ${p.phone}</a>` : '';
                html += `
                <div class="pharmacy-card">
                    <div class="pharmacy-info">
                        <strong class="pharmacy-name">${p.name}</strong>
                        <span class="pharmacy-address">${p.address}</span>
                        ${phone}
                        <span class="pharmacy-meta">${[rating, openStatus].filter(Boolean).join(' &nbsp;·&nbsp; ')}</span>
                    </div>
                    <div class="pharmacy-actions">
                        <a href="${mapsUrl}" target="_blank" class="pharm-btn pharm-btn-view">View</a>
                        <a href="${directionsUrl}" target="_blank" class="pharm-btn pharm-btn-directions">Directions</a>
                    </div>
                </div>`;
            }
            html += `</div>`;
        }
        html += `</div>`;

        const bubble = document.createElement("div");
        bubble.className = "message assistant-message";
        bubble.innerHTML = html;
        chatMessages.appendChild(bubble);
        bubble.scrollIntoView({ behavior: "smooth", block: "end" });

    } catch (err) {
        removeTypingIndicator();
        if (err.code === 1) {
            appendAssistantMessage("Location access was denied. You can find your nearest pharmacy on [Google Maps](https://www.google.com/maps/search/Dischem+OR+Clicks+pharmacy+near+me).");
        } else {
            appendAssistantMessage("We couldn't retrieve nearby pharmacies at this time. Please search on [Google Maps](https://www.google.com/maps/search/Dischem+OR+Clicks+pharmacy+near+me).");
        }
    }
}

function hideFormPanel() {
    if (formPanel) {
        formPanel.style.display = "none";
        formPanel.innerHTML = "";
    }
    // Only show input bar for inputNode type — keep hidden otherwise
    if (currentNodeType !== "inputNode") {
        document.getElementById("chatInputArea").style.display = "none";
    }
}

// ===== Back navigation =====
async function goBack() {
    formHistory = [];
    previousOptionsState = null;
    hideFormPanel();
    clearButtonGrid();
    chatMessages.innerHTML = "";
    document.getElementById("chatInputArea").style.display = "none";

    // Go back one step if history exists, otherwise go to main menu
    const prevNode = nodeHistory.pop();
    if (prevNode && prevNode.node_id && prevNode.node_id !== "start") {
        currentNodeData = null; // prevent re-pushing to history
        renderFlowNode(prevNode, true);
    } else {
        nodeHistory = [];
        try {
            const res = await fetch("/flow/start");
            const data = await res.json();
            renderFlowNode(data, true);
        } catch (err) { /* ignore */ }
    }
}

// ===== Reset =====
function resetToMainMenu() {
    currentNodeId        = null;
    currentNodeType      = null;
    nodeHistory          = [];
    isAiMode             = false;
    aiHistory            = [];
    isStreaming          = false;
    formHistory          = [];
    previousOptionsState = null;

    chatMessages.innerHTML = "";
    hideFormPanel();
    clearButtonGrid();

    // If patient already registered, just restart the flow
    if (patientDetails && patientDetails.name) {
        chatMessages.style.display = "";
        startFlow();
    } else {
        if (landingScreen) landingScreen.style.display = "";
        chatMessages.style.display = "none";
        showPatientDetailsForm();
    }
}

function returnToLanding() {
    currentNodeId        = null;
    currentNodeType      = null;
    isAiMode             = false;
    aiHistory            = [];
    isStreaming          = false;
    formHistory          = [];
    previousOptionsState = null;

    chatMessages.innerHTML = "";
    hideFormPanel();
    clearButtonGrid();

    // Show landing screen, hide chat
    if (landingScreen) landingScreen.style.display = "";
    chatMessages.style.display = "none";

    // Show sidebar again
    const sidebar = document.querySelector(".sidebar");
    if (sidebar) sidebar.style.display = "";
}

// ===== DOM Helpers =====
function appendUserMessage(text) {
    const row = document.createElement("div");
    row.className = "message-row user";
    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.textContent = "You";
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.textContent = text;
    row.appendChild(bubble);
    row.appendChild(avatar);
    chatMessages.appendChild(row);
    scrollToBottom();
}

function appendAssistantMessage(text) {
    const { bubble } = createAssistantBubble();
    renderMarkdown(bubble, text);
    scrollToBottom();
}

function createAssistantBubble() {
    const row = document.createElement("div");
    row.className = "message-row assistant";
    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.innerHTML = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path d="M8 2a6 6 0 100 12A6 6 0 008 2z" fill="white" opacity="0.9"/>
        <path d="M8 5v3l2 2" stroke="#2563eb" stroke-width="1.5" stroke-linecap="round"/>
    </svg>`;
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    row.appendChild(avatar);
    row.appendChild(bubble);
    chatMessages.appendChild(row);
    return { bubble, row };
}

function appendTypingIndicator() {
    const row = document.createElement("div");
    row.className = "message-row assistant typing-row";
    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.innerHTML = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path d="M8 2a6 6 0 100 12A6 6 0 008 2z" fill="white" opacity="0.9"/>
        <path d="M8 5v3l2 2" stroke="#2563eb" stroke-width="1.5" stroke-linecap="round"/>
    </svg>`;
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    const indicator = document.createElement("div");
    indicator.className = "typing-indicator";
    indicator.innerHTML = "<span></span><span></span><span></span>";
    bubble.appendChild(indicator);
    row.appendChild(avatar);
    row.appendChild(bubble);
    chatMessages.appendChild(row);
    scrollToBottom();
    return row;
}

function removeTypingIndicator() {
    const el = chatMessages.querySelector(".typing-row");
    if (el) el.remove();
}

function renderMarkdown(el, text) {
    // Process line by line
    const lines = text.split("\n");
    let html = "";
    let inList = false;

    const inlineFormat = s => s
        .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
        .replace(/\*(.*?)\*/g, "<em>$1</em>")
        .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');

    const nhsLink = (condition) => {
        const query = encodeURIComponent(condition.trim());
        return `<a href="https://www.nhs.uk/search/results?q=${query}" target="_blank" class="nhs-link" title="View on NHS.uk">NHS</a>`;
    };

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        // Skip horizontal rules
        if (/^---+$/.test(line.trim())) {
            if (inList) { html += "</ul>"; inList = false; }
            continue;
        }

        // Headings — convert to bold text instead of large headers
        const hMatch = line.match(/^#{1,6}\s+(.*)/);
        if (hMatch) {
            if (inList) { html += "</ul>"; inList = false; }
            const content = inlineFormat(hMatch[1]);
            html += `<p class="md-heading">${content}</p>`;
            continue;
        }

        // List items (-, *, or • bullet characters)
        const listMatch = line.match(/^[-*•]\s+(.*)/);
        if (listMatch) {
            if (!inList) { html += "<ul>"; inList = true; }
            html += `<li>${inlineFormat(listMatch[1])}</li>`;
            continue;
        }

        // Close list if open
        if (inList) { html += "</ul>"; inList = false; }

        // Blank line — skip
        if (line.trim() === "") continue;

        // Numbered list item with a condition name e.g. "1. **Viral Infection**" or "1. Viral Infection"
        const numberedMatch = line.match(/^(\d+)\.\s+\*\*([^*]+)\*\*(.*)/);
        if (numberedMatch) {
            const conditionName = numberedMatch[2].trim();
            const rest = numberedMatch[3] ? inlineFormat(numberedMatch[3]) : "";
            html += `<p><strong>${conditionName}</strong>${rest} ${nhsLink(conditionName)}</p>`;
            continue;
        }

        // Regular paragraph
        html += `<p>${inlineFormat(line)}</p>`;
    }

    if (inList) html += "</ul>";
    el.innerHTML = html;
}

function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function scrollToPanel() {
    // After panel appears, chatMessages shrinks — re-scroll to bottom so last message stays visible
    setTimeout(() => {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }, 60);
}

function setInputEnabled(enabled) {
    chatInput.disabled = !enabled;
    sendBtn.disabled   = !enabled;
}

function updateContextLabel(label) {
    if (contextLabel) contextLabel.textContent = label;
}
