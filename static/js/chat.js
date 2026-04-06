// ===== State =====
let currentNodeId = null;
let currentNodeType = null;
let aiHistory = [];
let isStreaming = false;
let isAiMode = false;
let formHistory = [];

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

// ===== Init =====
document.addEventListener("DOMContentLoaded", () => {
    setupInputHandlers();
    // Show landing screen first; flow starts on "Get started"
    getStartedBtn.addEventListener("click", () => {
        landingScreen.style.display = "none";
        chatMessages.style.display  = "";
        startFlow();
    });
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
function renderFlowNode(node) {
    if (!node) return;

    currentNodeId   = node.node_id;
    currentNodeType = node.type;
    isAiMode        = (node.type === "ai" || node.type === "question");

    // Show message
    if (node.message) {
        appendAssistantMessage(node.message);
    }

    // Update sidebar label
    const labelMap = {
        "options": "Please choose an option",
        "form":    "Please fill in the form",
        "ai":      "AI Assistant",
        "question":"Please answer",
        "end":     "Complete",
    };
    updateContextLabel(labelMap[node.type] || "Navigation");

    // Hide both panels first
    hideFormPanel();
    clearButtonGrid();

    if (node.is_end) {
        // Show only "Back to Main Menu"
        showEndButtons();
        setInputEnabled(false);
        return;
    }

    if (node.type === "form") {
        showFormPanel(node.fields, node.node_id, {}, node.form_title || "");
        setInputEnabled(false);
        return;
    }

    if (node.type === "options") {
        showOptionButtons(node.options);
        setInputEnabled(false);
        buttonPanelLabel.textContent = "Please select an option:";
        return;
    }

    if (node.type === "ai" || node.type === "question") {
        // Free text — user types
        buttonPanelLabel.textContent = "Type your response below:";
        showBackButton();
        setInputEnabled(true);
        chatInput.focus();
        aiHistory = [];
        return;
    }

    // Fallback
    setInputEnabled(true);
}

// ===== Option Buttons =====
function showOptionButtons(options) {
    buttonPanel.style.display = "";
    buttonGrid.innerHTML = "";

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

        const icon = document.createElement("span");
        icon.style.cssText = "font-size:16px;flex-shrink:0;";
        icon.textContent = getIcon(opt.label);

        const text = document.createElement("span");
        text.textContent = opt.label;

        btn.appendChild(icon);
        btn.appendChild(text);

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
    btn.textContent = "← Back to Main Menu";
    btn.addEventListener("click", resetToMainMenu);
    buttonGrid.appendChild(btn);
}

function showBackButton() {
    buttonPanel.style.display = "";
    buttonGrid.innerHTML = "";
    addBackButtonToGrid();
    buttonPanelLabel.textContent = "";
}

function showEndButtons() {
    buttonPanel.style.display = "";
    buttonGrid.innerHTML = "";
    const btn = document.createElement("button");
    btn.className = "menu-btn back-btn";
    btn.textContent = "← Back to Main Menu";
    btn.addEventListener("click", resetToMainMenu);
    buttonGrid.appendChild(btn);
    buttonPanelLabel.textContent = "What would you like to do next?";
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
    } else {
        // Advance flow with free text (question node)
        handleFlowTextInput(text);
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
            body: JSON.stringify({ node_id: currentNodeId, option_handle: "" }),
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
async function streamAiResponse(userText) {
    if (isStreaming) return;
    isStreaming = true;

    appendUserMessage(userText);
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
        advanceAfterAi();

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

function showFormPanel(fields, nodeId, savedData = {}, formTitle = "") {
    if (!formPanel) return;

    formPanel.innerHTML = "";
    formPanel.style.display = "";
    document.getElementById("chatInputArea").style.display = "none";

    // Form title
    if (formTitle) {
        const titleEl = document.createElement("h2");
        titleEl.className = "form-panel-title";
        titleEl.textContent = formTitle;
        formPanel.appendChild(titleEl);
    }

    // Go Back button — shown when there's a previous form in history
    if (formHistory.length > 0) {
        const goBackBtn = document.createElement("button");
        goBackBtn.type = "button";
        goBackBtn.className = "form-go-back-btn";
        goBackBtn.textContent = "← Go Back";
        goBackBtn.addEventListener("click", () => {
            // Save current form values before going back
            const currentData = captureCurrentFormData(fields);
            const prev = formHistory.pop();
            // Push current form back so user can come forward again
            formHistory.push({ fields, nodeId, savedData: currentData, formTitle });
            showFormPanel(prev.fields, prev.nodeId, prev.savedData || {}, prev.formTitle || "");
        });
        formPanel.appendChild(goBackBtn);
    }

    const form = document.createElement("form");
    form.id = "flowForm";

    fields.forEach(field => {
        const group = document.createElement("div");
        group.className = "form-group";

        const label = document.createElement("label");
        label.textContent = field.label + (field.required ? " *" : "");
        label.htmlFor = "field_" + field.id;

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
        const formData = {};
        fields.forEach(f => {
            const el = document.getElementById("field_" + f.id);
            if (!el) return;
            if (f.type === "multi_select") {
                const checked = [...el.querySelectorAll("input[type=checkbox]:checked")].map(c => c.value);
                formData[f.id] = checked.join(", ") || "None selected";
            } else if (f.type === "checkbox") {
                formData[f.id] = el.checked ? "Yes" : "No";
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

        // Save this form (with raw captured data for field restoration) to history
        formHistory.push({ fields, nodeId, savedData: captureCurrentFormData(fields), formTitle });
        hideFormPanel();
        appendTypingIndicator();

        try {
            const res = await fetch("/flow/step", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ node_id: nodeId, option_handle: "", form_data: formData }),
            });
            const data = await res.json();
            removeTypingIndicator();
            renderFlowNode(data);
        } catch (err) {
            removeTypingIndicator();
            appendAssistantMessage("Your form was submitted. Thank you!");
            showEndButtons();
        }
    });

    formPanel.appendChild(form);
}

function hideFormPanel() {
    if (formPanel) {
        formPanel.style.display = "none";
        formPanel.innerHTML = "";
    }
    document.getElementById("chatInputArea").style.display = "";
}

// ===== Reset =====
function resetToMainMenu() {
    currentNodeId   = null;
    currentNodeType = null;
    isAiMode        = false;
    aiHistory       = [];
    isStreaming      = false;

    // Clear chat and panels
    chatMessages.innerHTML = "";
    hideFormPanel();
    clearButtonGrid();

    // Hide landing, show chat
    if (landingScreen) landingScreen.style.display = "none";
    chatMessages.style.display = "";

    // Restart flow from beginning
    startFlow();
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

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        // Skip horizontal rules
        if (/^---+$/.test(line.trim())) {
            if (inList) { html += "</ul>"; inList = false; }
            continue;
        }

        // Headings — convert to bold text instead of large headers
        const h3 = line.match(/^###\s+(.*)/);
        const h2 = line.match(/^##\s+(.*)/);
        const h1 = line.match(/^#\s+(.*)/);
        if (h1 || h2 || h3) {
            if (inList) { html += "</ul>"; inList = false; }
            const content = inlineFormat((h3 || h2 || h1)[1]);
            html += `<p class="md-heading">${content}</p>`;
            continue;
        }

        // List items
        const listMatch = line.match(/^[-*]\s+(.*)/);
        if (listMatch) {
            if (!inList) { html += "<ul>"; inList = true; }
            html += `<li>${inlineFormat(listMatch[1])}</li>`;
            continue;
        }

        // Close list if open
        if (inList) { html += "</ul>"; inList = false; }

        // Blank line — skip
        if (line.trim() === "") continue;

        // Regular paragraph
        html += `<p>${inlineFormat(line)}</p>`;
    }

    if (inList) html += "</ul>";
    el.innerHTML = html;
}

function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function setInputEnabled(enabled) {
    chatInput.disabled = !enabled;
    sendBtn.disabled   = !enabled;
}

function updateContextLabel(label) {
    if (contextLabel) contextLabel.textContent = label;
}
