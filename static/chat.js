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
  btn.addEventListener("click", () => showStep(btn.dataset.back));
});

// ---------------------------------------------------------------------
// AI Provider settings - lets ANY normal user plug in their own API key
// for ANY provider (not just Gemini/Claude) from the frontend, so it's
// not hardcoded by a developer in config.json ahead of time. The
// dropdown itself is built from whatever the backend's PROVIDER_REGISTRY
// currently knows about, plus an "Other / Custom..." option for
// anything else (DeepSeek, Groq, a local model server, etc - "koi bhi
// provider daal sako").
// ---------------------------------------------------------------------

let knownProviders = {}; // { providerKey: { label, api_style, default_model } }
let configuredProviders = {}; // { providerKey: { has_key, model } } - whatever already has a saved key

function populateAiProviderSelect(selectedProvider) {
  const select = el("aiProviderSelect");
  select.innerHTML = "";

  Object.entries(knownProviders).forEach(([key, info]) => {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = info.label || key;
    select.appendChild(opt);
  });

  const customOpt = document.createElement("option");
  customOpt.value = "__custom__";
  customOpt.textContent = "Other / Custom...";
  select.appendChild(customOpt);

  const isKnown = Object.prototype.hasOwnProperty.call(knownProviders, selectedProvider);
  select.value = isKnown ? selectedProvider : "__custom__";
  toggleCustomFields(!isKnown);

  if (!isKnown && selectedProvider) {
    el("aiCustomProviderName").value = selectedProvider;
  }
}

function toggleCustomFields(show) {
  el("aiCustomFields").classList.toggle("hidden", !show);
}

// Refreshes the Model field + status line to match whichever provider is
// CURRENTLY selected in the dropdown - without this, switching from
// Gemini to Claude in the dropdown left Gemini's model/status showing,
// which looked like the save had gone to the wrong provider.
function refreshFieldsForSelectedProvider() {
  const selected = el("aiProviderSelect").value;

  if (selected === "__custom__") {
    el("aiOAuthBlock").classList.add("hidden");
    el("aiApiKeyWrapper").classList.remove("hidden");
    const customName = el("aiCustomProviderName").value.trim().toLowerCase();
    const saved = customName ? configuredProviders[customName] : null;
    el("aiModel").value = saved ? saved.model || "" : "";
    el("aiSettingsStatus").textContent = saved && saved.has_key
      ? `An API key is already saved for '${customName}' on this server.`
      : "Enter this custom provider's details below.";
    return;
  }

  const label = knownProviders[selected]?.label || selected;
  const saved = configuredProviders[selected];
  el("aiModel").value = saved ? saved.model || "" : "";

  // Check if preset supports OAuth
  const supportsOAuth = ["gemini", "claude", "openai"].includes(selected);

  if (supportsOAuth) {
    el("aiOAuthBlock").classList.remove("hidden");
    const oauthBtn = el("aiOAuthBtn");
    
    let providerLabel = "Google";
    if (selected === "claude") providerLabel = "Anthropic";
    if (selected === "openai") providerLabel = "OpenAI";
    
    oauthBtn.textContent = `Sign in with ${providerLabel}`;
    oauthBtn.className = `btn-oauth btn-${selected}`;
    
    if (saved && saved.has_oauth) {
      el("oauthLinkedBadge").classList.remove("hidden");
      oauthBtn.classList.add("hidden");
      el("aiApiKeyWrapper").classList.add("hidden");
      el("aiSettingsStatus").textContent = `${label} is connected via OAuth.`;
    } else {
      el("oauthLinkedBadge").classList.add("hidden");
      oauthBtn.classList.remove("hidden");
      el("aiApiKeyWrapper").classList.remove("hidden");
      
      if (saved && saved.has_key) {
        el("aiSettingsStatus").textContent = `A ${label} API key is already saved on this server, or you can link via OAuth above.`;
      } else {
        el("aiSettingsStatus").textContent = `No ${label} API key or OAuth connected yet. Add one below to use it.`;
      }
    }
  } else {
    el("aiOAuthBlock").classList.add("hidden");
    el("aiApiKeyWrapper").classList.remove("hidden");
    
    if (saved && saved.has_key) {
      el("aiSettingsStatus").textContent = `A ${label} API key is already saved on this server.`;
    } else {
      el("aiSettingsStatus").textContent = `No ${label} API key saved yet — add one below to use it.`;
    }
  }
}

el("aiProviderSelect").addEventListener("change", (e) => {
  toggleCustomFields(e.target.value === "__custom__");
  el("aiApiKey").value = "";
  refreshFieldsForSelectedProvider();
});

el("aiCustomProviderName").addEventListener("input", refreshFieldsForSelectedProvider);

async function loadAiSettings() {
  try {
    const res = await fetch(`${API_BASE}/api/settings/ai`);
    if (!res.ok) return null;
    const data = await res.json();

    knownProviders = data.known_providers || {};
    configuredProviders = data.configured_providers || {};
    populateAiProviderSelect(data.provider);

    if (Object.prototype.hasOwnProperty.call(knownProviders, data.provider) === false) {
      el("aiApiStyle").value = data.api_style || "openai";
      el("aiBaseUrl").value = data.base_url || "";
    }

    refreshFieldsForSelectedProvider();

    return data;
  } catch (e) {
    return null;
  }
}

function openAiSettings() {
  el("aiSettingsModal").classList.remove("hidden");
  clearError("aiSettingsError");
  el("aiApiKey").value = "";
  loadAiSettings();
}

function closeAiSettings() {
  el("aiSettingsModal").classList.add("hidden");
}

el("aiSettingsBtn").addEventListener("click", openAiSettings);
el("closeAiSettingsBtn").addEventListener("click", closeAiSettings);

el("saveAiSettingsBtn").addEventListener("click", async () => {
  clearError("aiSettingsError");

  const selected = el("aiProviderSelect").value;
  const isCustom = selected === "__custom__";
  const provider = isCustom ? el("aiCustomProviderName").value.trim().toLowerCase() : selected;
  const apiKey = el("aiApiKey").value.trim();
  const model = el("aiModel").value.trim();
  const apiStyle = isCustom ? el("aiApiStyle").value : null;
  const baseUrl = isCustom ? el("aiBaseUrl").value.trim() : null;

  if (isCustom && !provider) {
    showError("aiSettingsError", "Please enter a name for the custom provider (e.g. groq, ollama).");
    return;
  }
  if (isCustom && apiStyle === "openai" && !baseUrl) {
    showError("aiSettingsError", "Please enter a base URL for this custom provider.");
    return;
  }
  const alreadyLinked = configuredProviders[provider] && configuredProviders[provider].has_oauth;
  if (!apiKey && !alreadyLinked) {
    showError("aiSettingsError", "Please enter an API key, or use \"Sign in with " + (knownProviders[provider]?.label || provider) + "\" above.");
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/settings/ai`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider,
        api_key: apiKey,
        model: model || null,
        api_style: apiStyle,
        base_url: baseUrl || null,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError("aiSettingsError", err.detail || `Could not save settings (${res.status}).`);
      return;
    }
    el("aiApiKey").value = "";
    await loadAiSettings();
    closeAiSettings();
  } catch (e) {
    showError("aiSettingsError", "Could not reach the server.");
  }
});

// ---------------------------------------------------------------------
// OAuth account linking (Sign in with Google / Anthropic / OpenAI) -
// the buttons exist in index.html and get their text/visibility set by
// refreshFieldsForSelectedProvider() above, but they had no click
// behavior wired up yet. This block adds that.
// ---------------------------------------------------------------------

// Opens the provider's login page (real Google OAuth, or the
// mock_login.html simulation if no Client ID/Secret is configured) in a
// popup window, then polls until the popup closes and refreshes the AI
// settings so the "Linked via OAuth" badge shows up immediately.
el("aiOAuthBtn").addEventListener("click", () => {
  const provider = el("aiProviderSelect").value;
  if (!provider || provider === "__custom__") return;

  const popup = window.open(
    `${API_BASE}/api/auth/${provider}/login`,
    "oauthLogin",
    "width=480,height=680"
  );

  if (!popup) {
    showError("aiSettingsError", "Popup was blocked - please allow popups for this site and try again.");
    return;
  }

  const pollTimer = setInterval(async () => {
    if (popup.closed) {
      clearInterval(pollTimer);
      await loadAiSettings();
    }
  }, 500);
});

el("oauthUnlinkBtn").addEventListener("click", async () => {
  const provider = el("aiProviderSelect").value;
  if (!provider || provider === "__custom__") return;

  try {
    const res = await fetch(`${API_BASE}/api/auth/${provider}/unlink`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError("aiSettingsError", err.detail || `Could not unlink (${res.status}).`);
      return;
    }
    await loadAiSettings();
  } catch (e) {
    showError("aiSettingsError", "Could not reach the server.");
  }
});

// "OAuth Developer Settings" expandable section - lets Sir paste in a
// real Google Client ID/Secret. Left blank, the app just falls back to
// mock_login.html simulation mode (see aiOAuthBtn above).
el("toggleOAuthConfigBtn").addEventListener("click", () => {
  el("oauthConfigSection").classList.toggle("hidden");
  if (!el("oauthConfigSection").classList.contains("hidden")) {
    loadOAuthConfig();
  }
});

async function loadOAuthConfig() {
  try {
    const res = await fetch(`${API_BASE}/api/auth/config`);
    if (!res.ok) return;
    const data = await res.json();
    el("googleClientId").value = data.google_client_id || "";
    el("googleClientSecret").value = "";
    el("googleClientSecret").placeholder = data.has_google_secret
      ? "Google Client Secret (already saved - leave blank to keep it)"
      : "Google Client Secret";
    el("oauthConfigStatus").textContent = data.is_demo_mode
      ? "Currently running in simulation/demo mode."
      : "Real Google OAuth is configured.";
  } catch (e) {
    // silent - this is a secondary, expandable section
  }
}

el("saveOAuthConfigBtn").addEventListener("click", async () => {
  const clientId = el("googleClientId").value.trim();
  const clientSecret = el("googleClientSecret").value.trim();

  try {
    const res = await fetch(`${API_BASE}/api/auth/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        google_client_id: clientId || null,
        google_client_secret: clientSecret || null,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      el("oauthConfigStatus").textContent = err.detail || `Could not save (${res.status}).`;
      return;
    }
    el("googleClientSecret").value = "";
    await loadOAuthConfig();
  } catch (e) {
    el("oauthConfigStatus").textContent = "Could not reach the server.";
  }
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

// Picking a database now goes straight to naming it - the AI figures out
// which layout to use on its own, per question, so the end user never
// has to see or choose FileMaker layout names.
function selectDatabase(dbName) {
  clearError("step3Error");
  wizardState.database = dbName;

  if (wizardState.editing) {
    el("wProfileKey").value = wizardState.editing.key;
    el("wProfileName").value = wizardState.editing.name;
  } else {
    el("wProfileKey").value = dbName.toLowerCase().replace(/\s+/g, "_");
    el("wProfileName").value = dbName;
  }
  showStep(3);
}

// ---------------------------------------------------------------------
// Step 3: save profile, then move on to the next queued database (if any)
// ---------------------------------------------------------------------

el("saveProfileBtn").addEventListener("click", async () => {
  clearError("step3Error");
  const profileKey = el("wProfileKey").value.trim();
  const profileName = el("wProfileName").value.trim();

  if (!profileKey || !profileName) {
    showError("step3Error", "Both a profile key and a display name are required.");
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
        profile_key: profileKey,
        profile_name: profileName,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError("step3Error", err.detail || `Could not save profile (${res.status}).`);
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
    showError("step3Error", "An error occurred while saving.");
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
      <small>${profile.database}</small>
    `;
    label.querySelector("input").addEventListener("change", onActiveDatabasesChanged);
    listBox.appendChild(label);
  });

  updateActiveProfileLabel(data.profiles, Array.from(activeKeys));
  loadSavedChat();
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
  saveCurrentChat();

  // Show a "Searching..." placeholder immediately, and disable Send so
  // the user gets instant feedback instead of wondering if the click
  // registered - this doesn't make the AI itself faster, but the wait
  // no longer feels broken/stuck.
  const sendBtn = el("chatForm").querySelector("button[type=submit]");
  const loadingWrap = document.createElement("div");
  loadingWrap.className = "msg bot loading-msg";
  loadingWrap.innerHTML = `<span class="typing-dots"><span></span><span></span><span></span></span> Searching...`;
  el("messages").appendChild(loadingWrap);
  el("messages").scrollTop = el("messages").scrollHeight;
  if (sendBtn) sendBtn.disabled = true;

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
      // If the server is telling us there's no API key set up yet, pop
      // the AI Settings modal open so the user can fix it right away.
      if (res.status === 400 && /API key/i.test(detail)) {
        openAiSettings();
      }
      return;
    }

    const data = await res.json();
    dailyCount++;
    updateQuotaBadge();

    const answer = data.data;
    appendMessage(formatBotText(answer), "bot");
    conversationHistory.push({ role: "bot", text: answer });
    saveCurrentChat();
  } catch (err) {
    appendMessage("Something went wrong reaching the server. (network/parse error)", "bot");
  } finally {
    loadingWrap.remove();
    if (sendBtn) sendBtn.disabled = false;
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

// Load saved chat history from localStorage
function loadSavedChat() {
  const select = el("projectSelect");
  if (!select) return;
  const projectKey = select.value;
  if (!projectKey) return;

  const checkedProfiles = Array.from(
    el("profileCheckList").querySelectorAll("input[type=checkbox]:checked")
  ).map((c) => c.value);

  const key = `fm_chat_history_${projectKey}_${checkedProfiles.sort().join('_')}`;
  const saved = localStorage.getItem(key);

  el("messages").innerHTML = "";
  conversationHistory = [];

  if (saved) {
    try {
      conversationHistory = JSON.parse(saved);
      conversationHistory.forEach((msg) => {
        appendMessage(formatBotText(msg.text), msg.role === "bot" ? "bot" : "user");
      });
    } catch (e) {
      console.error("Error parsing saved chat history:", e);
      conversationHistory = [];
    }
  }
}

// Save current chat history to localStorage
function saveCurrentChat() {
  const select = el("projectSelect");
  if (!select) return;
  const projectKey = select.value;
  if (!projectKey) return;

  const checkedProfiles = Array.from(
    el("profileCheckList").querySelectorAll("input[type=checkbox]:checked")
  ).map((c) => c.value);

  const key = `fm_chat_history_${projectKey}_${checkedProfiles.sort().join('_')}`;
  localStorage.setItem(key, JSON.stringify(conversationHistory));
}

// Clear chat history
el("clearChatBtn").addEventListener("click", () => {
  if (!confirm("Are you sure you want to clear the chat history for this session?")) return;
  const select = el("projectSelect");
  if (!select) return;
  const projectKey = select.value;
  if (!projectKey) return;

  const checkedProfiles = Array.from(
    el("profileCheckList").querySelectorAll("input[type=checkbox]:checked")
  ).map((c) => c.value);

  const key = `fm_chat_history_${projectKey}_${checkedProfiles.sort().join('_')}`;
  localStorage.removeItem(key);

  el("messages").innerHTML = "";
  conversationHistory = [];
});

// Run sequential initialization so elements like projectSelect are fully loaded
async function init() {
  updateQuotaBadge();

  // If no AI provider key has been set up yet, open the Settings modal
  // right away so a brand-new user isn't stuck with a chat that silently
  // fails - "koi bhi user apna Claude ya Gemini API key daal sake".
  const aiStatus = await loadAiSettings();
  if (!aiStatus || !aiStatus.has_key) {
    openAiSettings();
  }

  await loadProjects();
  await loadProfiles();
}
init();
