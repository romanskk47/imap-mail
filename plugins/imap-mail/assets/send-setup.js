"use strict";
(() => {
  const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
  history.replaceState(null, "", location.pathname);
  const $ = (id) => document.getElementById(id);
  let checkId = null;
  let busy = false;
  const messages = {
    HOST_RETYPE_REQUIRED: "Type the exact SMTP hostname again before checking.",
    CREDENTIAL_REUSE_CONSENT_REQUIRED: "Confirm reuse of this account's existing credential.",
    SEND_ENABLE_CONSENT_REQUIRED: "Confirm that you want to enable sending for this account.",
    SENT_FOLDER_UNAVAILABLE: "The account's existing Sent folder could not be verified. Ask your agent to check the folder configuration; do not guess a folder.",
    SENT_FOLDER_AMBIGUOUS: "More than one Sent folder was found. Ask your agent to check the account's folder configuration.",
    SMTP_AUTHENTICATION_FAILED: "SMTP sign-in failed. Check the provider's SMTP access or app-password requirements. OAuth is not supported in this setup.",
    CHECK_ATTEMPT_LIMIT: "This setup has reached its three-check limit. Cancel and ask the agent to open a new setup.",
    LOGIN_ATTEMPT_LIMIT: "This setup has reached its three-check limit. Cancel and ask the agent to open a new setup.",
    SETUP_CONFIGURATION_CHANGED: "This account changed after the check. Start again so the latest configuration is preserved.",
    SESSION_EXPIRED: "This private setup expired. Ask your agent to open a fresh one."
  };
  function status(text) { $("status").textContent = text; }
  async function api(path, data) {
    const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Setup-Token": token}, body: JSON.stringify(data), credentials: "omit", cache: "no-store", redirect: "error"});
    const value = await response.json();
    if (!response.ok || value.status === "error") {
      const code = value.error_code || "SETUP_OPERATION_FAILED";
      throw new Error(messages[code] || "The check could not be completed. Verify SMTP settings with your provider and try again. No email was sent.");
    }
    return value;
  }
  function invalidate() {
    if (checkId) status("Settings changed. Check the connection again before saving.");
    checkId = null; $("review").hidden = true;
  }
  $("setup-form").addEventListener("input", invalidate);
  $("setup-form").addEventListener("change", invalidate);
  $("setup-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !$("setup-form").reportValidity()) return;
    invalidate();
    const host = $("host").value.trim().toLowerCase();
    const typed = $("host-confirmation").value.trim();
    if (typed !== host) { status(messages.HOST_RETYPE_REQUIRED); return; }
    const payload = {host, port: Number($("port").value), security: $("security").value, sent_copy: $("sent-copy").value,
      host_confirmed: true, host_confirmation: typed, agent_host_confirmation: typed,
      credential_reuse_consent: $("reuse-consent").checked, enable_sending_consent: $("enable-consent").checked};
    busy = true; $("check").disabled = true; $("fields").disabled = true; status("Checking TLS and sign-in only. No email will be sent…");
    try {
      const value = await api("/api/check", payload);
      checkId = value.check_id;
      $("summary").textContent = `${value.email} → ${value.smtp.host}:${value.smtp.port} (${value.smtp.security}). Sent copy: ${value.smtp.sent_copy === "server" ? "saved by provider" : value.sent_mailbox}.`;
      $("review").hidden = false; status("Review the settings and choose Save to enable sending.");
    } catch (error) { status(error.message); }
    finally { busy = false; $("check").disabled = false; $("fields").disabled = false; }
  });
  $("save").addEventListener("click", async () => {
    if (busy || !checkId || !$("reuse-consent").checked || !$("enable-consent").checked) return;
    busy = true; $("save").disabled = true; $("fields").disabled = true;
    try {
      await api("/api/save", {check_id: checkId, storage_consent: true});
      checkId = null; $("review").hidden = true;
      status("Saved. Sending is enabled for this account only. Nothing was sent. You can close this tab and return to your agent.");
    } catch (error) { status(error.message); $("save").disabled = false; $("fields").disabled = false; }
    finally { busy = false; }
  });
  $("cancel").addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    try { await api("/api/cancel", {}); $("fields").disabled = true; invalidate(); status("Cancelled. Sending settings were not changed. Nothing was sent."); }
    catch (error) { status(error.message); }
    finally { busy = false; }
  });
  (async () => {
    if (!token) { status("This private setup link is missing or expired. Ask your agent to open a new setup."); return; }
    try {
      const value = await api("/api/context", {});
      const p = value.prefill;
      $("identity").textContent = `${p.email}\n${p.entity}\nAccount: ${p.account}`;
      for (const key of ["host", "port", "security"]) if (p[key] !== undefined) $(key).value = String(p[key]);
      if (p.sent_copy !== undefined) $("sent-copy").value = p.sent_copy;
      $("fields").disabled = false;
      status("Confirm the SMTP host and permissions below. Setup does not send messages.");
    } catch (error) { status(error.message); }
  })();
})();
