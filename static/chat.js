const API_BASE = "";
const el = (id) => document.getElementById(id);

let dailyCount = 0;
const DAILY_LIMIT = 20;

// Conversation memory: sent with every /api/chat request so the model
// can understand follow-up questions like "show me those sorted by name".
let conversationHistory = [];

// Wizard state, carried across steps
const wizardState = {
  server_url: "",
  username: "",
  password: "", // left "" when reusing already-saved shared credentials
  verify_ssl: false,
  database: "",
  layout: "",
  schema: null,
  // Databases the user ticked in Step 2 that still need a layout picked
  // and saved, one at a time (Zeeshan's TAILORINGDEV + LaundryPOS case).
  dbQueue: [],
  // Set when using the "Change Layout" shortcut on an EXISTING profile -
  // { key, name } of the profile being edited, so Save overwrites it
  // instead of creating a new one, and Back cancels instead of going to
  // the (skipped) database-selection step.
  editing: null,
};

// ---------------------------------------------------------------------
// Wizard navigation helpers
// ---------------------------------------------------------------------

function showStep(n) {
  document.querySelectorAll(".wizard-step").forEach((s) => s.classList.add("hidden"));
  document.querySelectorAll(".wizard-steps .step").forEach((s) => s.classList.remove("active"));
  el(`step${n}`).classList.remove("hidden");
  document.querySelector(`.step[data-step="${n}"]`).classList.add("active");
}

function showError(stepId, msg) {
  const box = el(stepId);
  box.textContent = msg;
  box.classList.remove("hidden");
}

function clearError(stepId) {
  el(stepId).classList.add("hidden");
}

document.querySelectorAll("[data-back]").forEach((btn) => {
  btn.addEventListener("click", () => {
    // In "Change Layout" edit mode, Step 2 was skipped entirely (we
    // already know the database) - so Back from Step 3 should just
    // cancel the edit and close the wizard, not jump to an empty Step 2.
    if (wizardState.editing && btn.dataset.back === "2") {
      wizardState.editing = null;
      el("wizard").classList.add("hidden");
      return;
    }
    showStep(btn.dataset.back);
  });
});

// ---------------------------------------------------------------------
// Shared saved credentials ("Zeeshan already set these up" shortcut)
// ---------------------------------------------------------------------

async function checkSavedCredentials() {
  try {
    const res = await fetch(`${API_BASE}/api/credentials`);
    const data = await res.json();
    const btn = el("useSavedCredsBtn");
    if (data.has_credentials) {
      btn.textContent = `Use saved login (${data.username} @ ${data.server_url})`;
      btn.classList.remove("hidden");
    } else {
      btn.classList.add("hidden");
    }
  } catch (e) {
    el("useSavedCredsBtn").classList.add("hidden");
  }
}

el("useSavedCredsBtn").addEventListener("click", async () => {
  clearError("step1Error");
  wizardState.server_url = "";
  wizardState.username = "";
  wizardState.password = ""; // empty -> backend falls back to saved credentials
  wizardState.verify_ssl = false;

  try {
    const res = await fetch(`${API_BASE}/api/discover/databases/refresh`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError("step1Error", err.detail || `Could not use saved login (${res.status}).`);
      return;
    }
    const data = await res.json();
    renderDatabaseList(data.databases);
    showStep(2);
  } catch (e) {
    showError("step1Error", "Could not reach the server.");
  }
});

// ---------------------------------------------------------------------
// Step 1 -> 2: credentials -> list databases
// ---------------------------------------------------------------------

el("toStep2Btn").addEventListener("click", async () => {
  clearError("step1Error");
  wizardState.server_url = el("wServer").value.trim();
  wizardState.username = el("wUsername").value.trim();
  wizardState.password = el("wPassword").value;
  wizardState.verify_ssl = el("wVerifySsl").checked;

  if (!wizardState.server_url || !wizardState.username || !wizardState.password) {
    showError("step1Error", "Server, username, and password are all required.");
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/discover/databases`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        server_url: wizardState.server_url,
        username: wizardState.username,
        password: wizardState.password,
        verify_ssl: wizardState.verify_ssl,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError("step1Error", err.detail || `Connection failed (${res.status}).`);
      return;
    }
    const data = await res.json();
    renderDatabaseList(data.databases);
    showStep(2);
  } catch (e) {
    showError("step1Error", "Could not reach the server. Please check the URL.");
  }
});

// ---------------------------------------------------------------------
// Step 2: pick ONE OR MORE databases (checkboxes), then walk through
// layout selection + schema preview for each one, one at a time.
// ---------------------------------------------------------------------

let allDatabases = [];
let checkedDatabases = new Set();
let currentFilteredDatabases = [];

function renderDatabaseList(databases) {
  allDatabases = databases;
  checkedDatabases = new Set();
  applyDatabaseFilter("");
  const search = el("databaseSearch");
  if (search) {
    search.value = "";
    search.oninput = () => applyDatabaseFilter(search.value);
    search.onkeydown = (e) => {
      if (e.key === "Enter" && currentFilteredDatabases.length > 0) {
        e.preventDefault();
        const db = currentFilteredDatabases[0];
        if (checkedDatabases.has(db)) checkedDatabases.delete(db);
        else checkedDatabases.add(db);
        applyDatabaseFilter(search.value);
      }
    };
  }
}

function applyDatabaseFilter(filterText) {
  const list = el("databaseList");
  list.innerHTML = "";
  const filtered = allDatabases.filter((db) =>
    db.toLowerCase().includes(filterText.trim().toLowerCase())
  );
  currentFilteredDatabases = filtered;
  if (!filtered.length) {
    list.innerHTML = "<em>No matching databases.</em>";
  } else {
    filtered.forEach((db) => {
      const label = document.createElement("label");
      label.className = "option-item option-item-checkbox";
      label.innerHTML = `<input type="checkbox" value="${db}" ${checkedDatabases.has(db) ? "checked" : ""} /> <span>${db}</span>`;
      label.querySelector("input").addEventListener("change", (e) => {
        if (e.target.checked) checkedDatabases.add(db);
        else checkedDatabases.delete(db);
      });
      list.appendChild(label);
    });
  }
  ensureDatabaseContinueButton();
}

// "Continue" button appears once at least one database is ticked -
// selection is tracked in checkedDatabases so it survives re-filtering.
function ensureDatabaseContinueButton() {
  let continueBtn = el("databaseContinueBtn");
  if (!continueBtn) {
    continueBtn = document.createElement("button");
    continueBtn.id = "databaseContinueBtn";
    continueBtn.className = "btn-primary";
    continueBtn.textContent = "Continue";
    continueBtn.addEventListener("click", () => {
      const checked = Array.from(checkedDatabases);
      if (!checked.length) {
        showError("step2Error", "Tick at least one database to continue.");
        return;
      }
      clearError("step2Error");
      wizardState.dbQueue = checked;
      startNextDatabaseInQueue();
    });
    el("databaseList").parentElement.insertBefore(continueBtn, el("step2Error"));
  }
}

el("refreshInWizardBtn").addEventListener("click", async () => {
  clearError("step2Error");
  try {
    // If we have a live password in memory, refresh with it; otherwise
    // fall back to the saved shared credentials (same as "Use saved login").
    const res = wizardState.password
      ? await fetch(`${API_BASE}/api/discover/databases`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            server_url: wizardState.server_url,
            username: wizardState.username,
            password: wizardState.password,
            verify_ssl: wizardState.verify_ssl,
          }),
        })
      : await fetch(`${API_BASE}/api/discover/databases/refresh`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError("step2Error", err.detail || `Could not refresh (${res.status}).`);
      return;
    }
    const data = await res.json();
    renderDatabaseList(data.databases);
  } catch (e) {
    showError("step2Error", "Could not reach the server.");
  }
});

// Pulls the next database out of the queue and starts its layout step.
// Once the queue is empty, the wizard is done and the app view loads.
function startNextDatabaseInQueue() {
  if (!wizardState.dbQueue.length) {
    el("wizard").classList.add("hidden");
    resetWizardForm();
    loadProfiles();
    return;
  }
  const dbName = wizardState.dbQueue.shift();
  selectDatabase(dbName);
}

async function selectDatabase(dbName) {
  clearError("step2Error");
  wizardState.database = dbName;

  try {
    const res = await fetch(`${API_BASE}/api/discover/layouts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        server_url: wizardState.server_url,
        username: wizardState.username,
        password: wizardState.password,
        verify_ssl: wizardState.verify_ssl,
        database: wizardState.database,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError("step2Error", err.detail || `Could not fetch layouts (${res.status}).`);
      return;
    }
    const data = await res.json();
    renderLayoutList(data.layouts);
    showStep(3);
  } catch (e) {
    showError("step2Error", "An error occurred while fetching layouts.");
  }
}

// ---------------------------------------------------------------------
// "Change Layout" shortcut: jumps straight to Step 3 (layout list) for
// an EXISTING profile's database, reusing its already-saved credentials.
// No need to re-enter server/username/password or re-pick the database -
// this only fixes a wrong layout choice, in one click.
// ---------------------------------------------------------------------

async function startEditLayout(profileKey, profile) {
  wizardState.server_url = profile.server_url;
  wizardState.username = profile.username;
  wizardState.password = ""; // blank -> backend reuses the saved shared credentials
  wizardState.verify_ssl = profile.verify_ssl || false;
  wizardState.database = profile.database;
  wizardState.dbQueue = [];
  wizardState.editing = { key: profileKey, name: profile.profile_name };

  el("wizard").classList.remove("hidden");
  clearError("step3Error");
  el("layoutList").innerHTML = "<em>Loading layouts...</em>";
  showStep(3);

  try {
    const res = await fetch(`${API_BASE}/api/discover/layouts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        server_url: wizardState.server_url,
        username: wizardState.username,
        password: wizardState.password,
        verify_ssl: wizardState.verify_ssl,
        database: wizardState.database,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError("step3Error", err.detail || `Could not fetch layouts (${res.status}).`);
      return;
    }
    const data = await res.json();
    renderLayoutList(data.layouts);
  } catch (e) {
    showError("step3Error", "An error occurred while fetching layouts.");
  }
}

let allLayouts = [];
let currentFilteredLayouts = [];

function renderLayoutList(layouts) {
  allLayouts = layouts;
  applyLayoutFilter("");
  const search = el("layoutSearch");
  if (search) {
    search.value = "";
    search.oninput = () => applyLayoutFilter(search.value);
    search.onkeydown = (e) => {
      if (e.key === "Enter" && currentFilteredLayouts.length > 0) {
        e.preventDefault();
        selectLayout(currentFilteredLayouts[0]);
      }
    };
  }
}

function applyLayoutFilter(filterText) {
  const list = el("layoutList");
  list.innerHTML = "";
  const filtered = allLayouts.filter((layout) =>
    layout.toLowerCase().includes(filterText.trim().toLowerCase())
  );
  currentFilteredLayouts = filtered;
  if (!filtered.length) {
    list.innerHTML = "<em>No matching layouts.</em>";
    return;
  }
  filtered.forEach((layout) => {
    const item = document.createElement("div");
    item.className = "option-item";
    item.textContent = layout;
    item.addEventListener("click", () => selectLayout(layout));
    list.appendChild(item);
  });
}

// ---------------------------------------------------------------------
// Step 3 -> 4: pick layout -> fetch schema preview
// ---------------------------------------------------------------------

async function selectLayout(layoutName) {
  clearError("step3Error");
  wizardState.layout = layoutName;

  try {
    const res = await fetch(`${API_BASE}/api/discover/schema`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        server_url: wizardState.server_url,
        username: wizardState.username,
        password: wizardState.password,
        verify_ssl: wizardState.verify_ssl,
        database: wizardState.database,
        layout: wizardState.layout,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError("step3Error", err.detail || `Could not fetch schema (${res.status}).`);
      return;
    }
    const schema = await res.json();
    wizardState.schema = schema;
    renderSchemaPreview(schema);
    if (wizardState.editing) {
      // Editing an existing profile - keep its original key/name so Save
      // overwrites it instead of creating a duplicate entry.
      el("wProfileKey").value = wizardState.editing.key;
      el("wProfileName").value = wizardState.editing.name;
    } else {
      el("wProfileKey").value = wizardState.database.toLowerCase().replace(/\s+/g, "_");
      el("wProfileName").value = wizardState.database;
    }
    showStep(4);
  } catch (e) {
    showError("step3Error", "An error occurred while fetching the schema.");
  }
}

function renderSchemaPreview(schema) {
  const box = el("schemaPreview");
  const fieldsHtml = schema.fields.length
    ? `<ul>${schema.fields.map((f) => `<li>${f}</li>`).join("")}</ul>`
    : "<em>No fields found.</em>";
  const tablesHtml = schema.related_tables.length
    ? `<ul>${schema.related_tables.map((t) => `<li>${t}</li>`).join("")}</ul>`
    : "<em>No related tables (portals) on this layout.</em>";

  box.innerHTML = `
    <h4>Fields (${schema.fields.length})</h4>
    ${fieldsHtml}
    <h4>Related Tables (${schema.related_tables.length})</h4>
    ${tablesHtml}
  `;
}

// ---------------------------------------------------------------------
// Step 4: save profile, then move on to the next queued database (if any)
// ---------------------------------------------------------------------

el("saveProfileBtn").addEventListener("click", async () => {
  clearError("step4Error");
  const profileKey = el("wProfileKey").value.trim();
  const profileName = el("wProfileName").value.trim();

  if (!profileKey || !profileName) {
    showError("step4Error", "Both a profile key and a display name are required.");
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/profiles/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        server_url: wizardState.server_url,
        username: wizardState.username,
        password: wizardState.password,
        verify_ssl: wizardState.verify_ssl,
        database: wizardState.database,
        layout: wizardState.layout,
        profile_key: profileKey,
        profile_name: profileName,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError("step4Error", err.detail || `Could not save profile (${res.status}).`);
      return;
    }
    // Move on to the next ticked database, if this was a multi-select.
    if (wizardState.dbQueue.length) {
      showStep(2);
      startNextDatabaseInQueue();
    } else {
      wizardState.editing = null;
      el("wizard").classList.add("hidden");
      resetWizardForm();
      await loadProfiles();
    }
  } catch (e) {
    showError("step4Error", "An error occurred while saving.");
  }
});

function resetWizardForm() {
  el("wServer").value = "";
  el("wUsername").value = "";
  el("wPassword").value = "";
  el("wVerifySsl").checked = false;
  wizardState.dbQueue = [];
  wizardState.editing = null;
  showStep(1);
  checkSavedCredentials();
}

// ---------------------------------------------------------------------
// Projects: group databases + chat per client. Switching projects loads
// that project's own database list and starts a fresh conversation.
// ---------------------------------------------------------------------

async function loadProjects() {
  const res = await fetch(`${API_BASE}/api/projects`);
  const data = await res.json();

  const select = el("projectSelect");
  select.innerHTML = "";
  Object.entries(data.projects).forEach(([key, project]) => {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = `${project.name} (${project.database_count})`;
    if (key === data.active_project) opt.selected = true;
    select.appendChild(opt);
  });
}

el("projectSelect").addEventListener("change", async (e) => {
  await fetch(`${API_BASE}/api/projects/active/${e.target.value}`, { method: "POST" });
  // Switching clients/projects - old messages belong to a different
  // client's data, so start a clean conversation.
  conversationHistory = [];
  el("messages").innerHTML = "";
  await loadProjects();
  await loadProfiles();
});

el("addProjectBtn").addEventListener("click", async () => {
  const name = prompt("Name this project (e.g. the client's name):");
  if (!name || !name.trim()) return;
  const res = await fetch(`${API_BASE}/api/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || "Could not create the project.");
    return;
  }
  conversationHistory = [];
  el("messages").innerHTML = "";
  await loadProjects();
  await loadProfiles(); // new project has no databases yet -> opens the wizard
});

el("deleteProjectBtn").addEventListener("click", async () => {
  const select = el("projectSelect");
  const key = select.value;
  const label = select.options[select.selectedIndex]?.textContent || key;
  if (!key) return;
  if (!confirm(`Delete project "${label}" and all its saved databases? This cannot be undone.`)) return;

  const res = await fetch(`${API_BASE}/api/projects/${key}`, { method: "DELETE" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || "Could not delete the project.");
    return;
  }
  conversationHistory = [];
  el("messages").innerHTML = "";
  await loadProjects();
  await loadProfiles();
});

// ---------------------------------------------------------------------
// Main app: profile list, multi-select active databases, chat
// ---------------------------------------------------------------------

async function loadProfiles() {
  const res = await fetch(`${API_BASE}/api/config/profiles`);
  const data = await res.json();

  if (Object.keys(data.profiles).length === 0) {
    el("wizard").classList.remove("hidden");
    showStep(1);
    checkSavedCredentials();
    return;
  }
  el("wizard").classList.add("hidden");

  const activeKeys = new Set(data.active_profiles || []);
  const listBox = el("profileCheckList");
  listBox.innerHTML = "";
  Object.entries(data.profiles).forEach(([key, profile]) => {
    const label = document.createElement("label");
    label.className = "profile-check-item";
    label.innerHTML = `
      <input type="checkbox" value="${key}" ${activeKeys.has(key) ? "checked" : ""} />
      <span>${profile.profile_name || key}</span>
      <small>${profile.database} &rarr; ${profile.layout}</small>
      <button type="button" class="change-layout-btn" data-key="${key}">Change Layout</button>
    `;
    label.querySelector("input").addEventListener("change", onActiveDatabasesChanged);
    label.querySelector(".change-layout-btn").addEventListener("click", (e) => {
      e.preventDefault();
      startEditLayout(key, profile);
    });
    listBox.appendChild(label);
  });

  updateActiveProfileLabel(data.profiles, Array.from(activeKeys));
}

function updateActiveProfileLabel(profiles, activeKeys) {
  if (!activeKeys.length) {
    el("activeProfileLabel").textContent = "No database selected";
    return;
  }
  const names = activeKeys.map((k) => (profiles[k] ? profiles[k].profile_name || k : k));
  el("activeProfileLabel").textContent =
    names.length === 1 ? `Connected: ${names[0]}` : `Connected: ${names.join(" + ")}`;
}

async function onActiveDatabasesChanged() {
  const checked = Array.from(
    el("profileCheckList").querySelectorAll("input[type=checkbox]:checked")
  ).map((c) => c.value);

  await fetch(`${API_BASE}/api/config/active`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile_keys: checked }),
  });

  // Switching which database(s) are active starts a fresh conversation,
  // since old messages may reference data from a database no longer active.
  conversationHistory = [];
  el("messages").innerHTML = "";
  loadProfiles();
}

el("refreshDatabasesBtn").addEventListener("click", async () => {
  try {
    const res = await fetch(`${API_BASE}/api/discover/databases/refresh`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || `Could not refresh (${res.status}).`);
      return;
    }
    const data = await res.json();
    // We don't know profile keys vs raw db names 1:1 here, so just tell
    // the user what's on the server now - "+ Add New Database" is where
    // any newly-appeared database actually gets set up as a profile.
    alert(`Server now reports ${data.databases.length} database(s): ${data.databases.join(", ")}`);
  } catch (e) {
    alert("Could not reach the server to refresh.");
  }
});

el("addProfileBtn").addEventListener("click", () => {
  el("wizard").classList.remove("hidden");
  resetWizardForm();
});

el("deleteProfileBtn").addEventListener("click", async () => {
  const checked = Array.from(
    el("profileCheckList").querySelectorAll("input[type=checkbox]:checked")
  ).map((c) => c.value);
  if (!checked.length) {
    alert("Tick the database(s) you want to remove first.");
    return;
  }
  if (!confirm(`Remove ${checked.length} selected database(s)?`)) return;
  for (const key of checked) {
    await fetch(`${API_BASE}/api/config/profiles/${key}`, { method: "DELETE" });
  }
  loadProfiles();
});

function appendMessage(html, sender) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${sender}`;
  wrap.innerHTML = html;
  el("messages").appendChild(wrap);
  el("messages").scrollTop = el("messages").scrollHeight;
}

// Converts a markdown pipe table (e.g. "| A | B |\n|---|---|\n| 1 | 2 |")
// into an actual HTML table. If the text isn't a table, it's returned as-is.
// If the table has headers but zero data rows, shows a clear message
// instead of a confusing empty table.
function formatBotText(text) {
  const looksLikeTable = text.includes("|") && text.includes("---");
  if (!looksLikeTable) return text;

  const lines = text.trim().split("\n").filter((line) => line.includes("|"));
  if (lines.length < 2) return text;

  const isSeparatorRow = (line) => line.replace(/[-|:\s]/g, "") === "";

  const dataLines = lines.filter((line, index) => index !== 0 && !isSeparatorRow(line));

  if (dataLines.length === 0) {
    return "No matching records were found.";
  }

  let html = '<table>';
  lines.forEach((line, index) => {
    if (isSeparatorRow(line)) return;
    let cells = line.split("|").map((c) => c.trim());
    if (cells[0] === "") cells.shift();
    if (cells[cells.length - 1] === "") cells.pop();
    const tag = index === 0 ? "th" : "td";
    html += "<tr>";
    cells.forEach((cell) => (html += `<${tag}>${cell}</${tag}>`));
    html += "</tr>";
  });
  html += "</table>";
  return html;
}

function updateQuotaBadge() {
  el("quotaBadge").textContent = `${dailyCount} / ${DAILY_LIMIT} today`;
}

async function sendMessage(message) {
  appendMessage(message, "user");
  conversationHistory.push({ role: "user", text: message });

  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        history: conversationHistory.slice(0, -1).slice(-10),
      }),
    });

    if (res.status === 429) {
      appendMessage("Daily quota reached. Please try again tomorrow.", "bot");
      return;
    }

    if (!res.ok) {
      let detail = `Server error (${res.status})`;
      try {
        const err = await res.json();
        detail = err.detail || detail;
      } catch (_) {
        // response wasn't JSON - keep the generic status-code message
      }
      appendMessage(detail, "bot");
      return;
    }

    const data = await res.json();
    dailyCount++;
    updateQuotaBadge();

    const answer = data.data;
    appendMessage(formatBotText(answer), "bot");
    conversationHistory.push({ role: "bot", text: answer });
  } catch (err) {
    appendMessage("Something went wrong reaching the server. (network/parse error)", "bot");
  }
}

el("chatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = el("chatInput");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  sendMessage(message);
});

updateQuotaBadge();
loadProjects();
loadProfiles();
