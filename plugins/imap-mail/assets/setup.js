"use strict";
(() => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  let token = fragment.get("token") || "";
  history.replaceState(null, "", location.pathname);
  const $ = id => document.getElementById(id);
  const form = $("setup-form"), error = $("error"), status = $("status");
  let step = 1, checkId = null, busy = false, accountEdited = false;
  let setupContext = {prefill:{}, host_source:"manual", require_host_retype:false};
  const messages = {
    REQUEST_REJECTED: "Ta sesja nie ma już ważnego dostępu. Otwórz nowy formularz z poziomu IMAP Mail.",
    SESSION_EXPIRED: "Sesja wygasła. Otwórz nowy formularz — niczego nie zapisano automatycznie.",
    LOGIN_ATTEMPT_LIMIT: "W tej sesji wykorzystano trzy próby. Otwórz nowy formularz po sprawdzeniu danych dostawcy.",
    CHECK_ATTEMPT_LIMIT: "W tej sesji wykorzystano trzy próby. Sprawdź dane dostawcy i otwórz nowy formularz.",
    HOST_CONFIRMATION_REQUIRED: "Potwierdź dokładny adres serwera przed sprawdzeniem dostępu.",
    HOST_RETYPE_REQUIRED: "Ten serwer pochodzi z propozycji agenta. Sprawdź go niezależnie, a następnie przepisz dokładny aktualny adres serwera.",
    STORAGE_CONSENT_REQUIRED: "Potwierdź lokalny zapis hasła i włączenie szkiców, aby zapisać konto.",
    ACCOUNT_ALREADY_EXISTS: "Ten identyfikator jest już zajęty. Wybierz inną nazwę — istniejących kont nie nadpisujemy.",
    EMAIL_ALREADY_CONFIGURED: "Ten adres ma już skonfigurowane konto. Nie utworzymy drugiego profilu ani nie nadpiszemy ustawień.",
    ACCOUNT_FILES_EXIST: "Pliki tego konta już istnieją. Niczego nie nadpisujemy. Wybierz inny identyfikator albo sprawdź istniejący profil.",
    INVALID_IMAP_HOST: "Wpisz samą, dokładną nazwę serwera IMAP — bez https://, ścieżki i numeru portu.",
    CHECK_EXPIRED_OR_UNAVAILABLE: "Sprawdzenie wygasło lub zostało już wykorzystane. Wróć do danych dostępu i sprawdź połączenie ponownie.",
    SETUP_CONFIGURATION_CHANGED: "Ustawienia poczty zmieniły się w międzyczasie. Nie nadpisaliśmy ich. Otwórz nowy formularz, aby sprawdzić aktualny stan.",
    AUTHENTICATION_FAILED: "Nie udało się zalogować. Sprawdź serwer, login i hasło aplikacji u dostawcy.",
    CONNECTION_CHECK_FAILED: "Nie udało się połączyć. Sprawdź serwer, port TLS i dostęp do sieci.",
    SETUP_CONNECTION_CHECK_FAILED: "Logowanie nie powiodło się. Sprawdź dokładny serwer, adres e-mail i hasło aplikacji; niczego nie zapisano.",
    TLS_993_REQUIRED: "Ten formularz obsługuje wyłącznie szyfrowane połączenie IMAP na porcie 993. Sprawdź ustawienia dostawcy.",
    DRAFTS_FOLDER_UNAVAILABLE: "Serwer nie wskazał folderu szkiców. Sprawdź jego dokładną nazwę w opcjach dodatkowych.",
    DRAFTS_FOLDER_AMBIGUOUS: "Serwer wskazał kilka folderów szkiców. Najpierw wyjaśnij ich konfigurację u dostawcy.",
    SIGNATURE_REQUIRED: "Wpisz podpis albo świadomie wybierz konto bez podpisu.",
    SIGNATURE_CHOICE_REQUIRED: "Wpisz podpis albo świadomie zaznacz konto bez podpisu.",
    UNSAFE_ATTACHMENT_ROOT: "Wybierz jeden konkretny folder z załącznikami, nie cały dysk, katalog domowy ani konfigurację.",
    ATTACHMENT_ROOT_TOO_BROAD: "Wybierz jeden konkretny folder z załącznikami. Nie można udostępnić całego dysku, katalogu domowego ani ustawień.",
    ATTACHMENT_ROOT_UNAVAILABLE: "Ten folder nie istnieje lub nie jest dostępny. Popraw ścieżkę albo zostaw pole załączników puste.",
    PRIVATE_DIRECTORY_PERMISSIONS_REQUIRE_700: "Folder ustawień ma zbyt szerokie uprawnienia. Niczego nie zapisano. Poproś o sprawdzenie prywatności lokalnego folderu konfiguracji.",
    PRIVATE_FILE_PERMISSIONS_REQUIRE_600: "Plik ustawień ma zbyt szerokie uprawnienia. Niczego nie nadpisano. Najpierw sprawdź prywatność lokalnej konfiguracji.",
    SETUP_OPERATION_FAILED: "Nie udało się dokończyć operacji. Sprawdź dane i spróbuj ponownie. Hasła nie pokażemy w komunikacie."
  };
  function fail(code) {
    const safe = /^[A-Z][A-Z0-9_]{0,100}$/.test(code) ? code : "SETUP_OPERATION_FAILED";
    error.textContent = (messages[safe] || "Nie udało się sprawdzić ustawień. Popraw dane lub rozpocznij nową sesję.") + " Kod: " + safe;
    error.hidden = false;
    error.tabIndex = -1;
    error.focus();
  }
  function clearError() { error.hidden = true; error.textContent = ""; }
  async function api(path, payload) {
    if (!token) throw new Error("SESSION_EXPIRED");
    const response = await fetch(path, {method:"POST", credentials:"omit", cache:"no-store", headers:{"Content-Type":"application/json", "X-Setup-Token":token}, body:JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok || result.status === "error") throw new Error(result.error_code || "SETUP_OPERATION_FAILED");
    return result;
  }
  function showStep(next) {
    step = next;
    for (let n=1;n<=3;n++) $("step-"+n).hidden = n !== step;
    document.querySelectorAll(".steps li").forEach((item,index) => index+1===step ? item.setAttribute("aria-current","step") : item.removeAttribute("aria-current"));
    $("back").hidden = step === 1;
    $("next").textContent = step < 3 ? "Dalej →" : checkId ? "Zapisz na tym komputerze" : "Sprawdź połączenie";
    $("confirmed-host").textContent = $("host").value.trim() + ":" + $("port").value;
    updateHostGuard();
    if (step === 3) review();
    clearError();
    $("title-"+step).focus();
  }
  function validate(ids) {
    for (const id of ids) {
      const input = $(id);
      input.removeAttribute("aria-invalid");
      if (!input.checkValidity()) { const disclosure=input.closest("details"); if(disclosure) disclosure.open=true; input.setAttribute("aria-invalid","true"); input.reportValidity(); input.focus(); return false; }
    }
    return true;
  }
  function signaturePreview() {
    $("signature").disabled = $("signature-none").checked;
    $("signature-preview").textContent = $("signature-none").checked ? "Bez podpisu — Twój świadomy wybór." : $("signature").value || "Wpisz podpis powyżej.";
  }
  function needsHostRetype() {
    return Boolean(setupContext.prefill.host) && (setupContext.require_host_retype || $("host").value.trim() !== setupContext.prefill.host || $("email").value.trim() !== setupContext.prefill.email);
  }
  function updateHostGuard() {
    const required=needsHostRetype();
    $("agent-host-warning").hidden=!required;
    $("agent-host-confirmation").disabled=!required;
    $("agent-host-confirmation").required=required;
    $("agent-host-value").textContent=$("host").value.trim();
    $("known-provider").hidden=required || setupContext.host_source!=="known_provider";
  }
  function row(label, value) {
    const group = document.createElement("div"), key = document.createElement("dt"), data = document.createElement("dd");
    key.textContent = label; data.textContent = value; group.append(key,data); $("review").append(group);
  }
  function review(checked) {
    $("review").replaceChildren();
    row("Adres", $("email").value.trim()); row("Konto", $("account").value.trim()); row("Właściciel", $("entity").value.trim());
    row("Serwer", $("host").value.trim() + ":" + $("port").value + " · TLS");
    row("Podpis", $("signature-none").checked ? "Świadomie bez podpisu" : $("signature").value);
    row("Załączniki", $("attachment-root").value.trim() || "Wyłączone — nie wskazano folderu");
    if (checked) row("Folder szkiców", checked.drafts_mailbox || "Sprawdzony");
  }
  function pending(value, text) {
    busy = value;
    for (const button of form.querySelectorAll("button")) button.disabled = value;
    status.textContent = text || "";
  }
  function payload() {
    const value = {account:$("account").value.trim(), email:$("email").value.trim(), entity:$("entity").value.trim(), host:$("host").value.trim(), port:Number($("port").value), tls:true, password:$("password").value, host_confirmed:$("host-confirmed").checked};
    if ($("signature-none").checked) value.signature_none = true; else value.signature_text = $("signature").value;
    if ($("attachment-root").value.trim()) value.attachment_root = $("attachment-root").value.trim();
    if ($("drafts-mailbox").value.trim()) value.drafts_mailbox = $("drafts-mailbox").value.trim();
    if (needsHostRetype()) value.agent_host_confirmation=$("agent-host-confirmation").value.trim();
    return value;
  }
  $("account").addEventListener("input", () => { accountEdited = true; });
  $("email").addEventListener("blur", async () => {
    if (!$("email").checkValidity()) return;
    if (!accountEdited) $("account").value = $("email").value.split("@")[0].replace(/[^a-zA-Z0-9_-]/g,"-").slice(0,80);
    if ($("host").value) return;
    try { const result = await api("/api/suggest", {email:$("email").value.trim()});
      if (result.host) { $("host").value = result.host; $("port").value = result.port || 993; $("suggestion").textContent = "Podpowiedź dostawcy. Sprawdź ją i potwierdź serwer w następnym kroku."; }
    } catch (_) { /* Unknown providers stay manual; no credentials have been sent. */ }
  });
  $("signature").addEventListener("input", signaturePreview);
  $("signature-none").addEventListener("change", signaturePreview);
  form.addEventListener("input", event => { if (event.target.matches("input,textarea")) event.target.removeAttribute("aria-invalid"); });
  form.addEventListener("change", event => { if (event.target.matches("input,textarea")) event.target.removeAttribute("aria-invalid"); });
  $("agent-host-confirmation").addEventListener("input", () => $("agent-host-confirmation").setCustomValidity(""));
  $("back").addEventListener("click", () => {
    if (busy) return;
    if (checkId) { checkId = null; $("checked-banner").hidden = true; $("storage-consent").checked = false; }
    showStep(Math.max(1,step-1));
  });
  $("cancel").addEventListener("click", async () => {
    if (busy) return;
    try { await api("/api/cancel", {}); } catch (_) {}
    token = ""; $("password").value = ""; form.reset(); form.hidden = true;
    $("finished-title").textContent = "Konfiguracja anulowana";
    $("finished").querySelector("p").textContent = "Nie zapisano nowych ustawień. Możesz zamknąć tę kartę.";
    $("finished").hidden = false; $("finished-title").focus();
  });
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (busy) return; clearError();
    if (step === 1) { if (validate(["email","account","entity","host","port"])) { $("host-confirmed").checked = false; showStep(2); } return; }
    if (step === 2) {
      if (needsHostRetype() && $("agent-host-confirmation").value.trim() !== $("host").value.trim()) { $("agent-host-confirmation").setCustomValidity("Przepisz dokładnie aktualny adres serwera z komunikatu powyżej."); $("agent-host-confirmation").setAttribute("aria-invalid","true"); $("agent-host-confirmation").reportValidity(); $("agent-host-confirmation").focus(); return; }
      if (!validate(["agent-host-confirmation","password","host-confirmed"])) return;
      if (!$("signature-none").checked && !$("signature").value.trim()) { $("signature").setAttribute("aria-invalid","true"); fail("SIGNATURE_REQUIRED"); return; }
      showStep(3); return;
    }
    if (checkId) {
      if (!$("storage-consent").checked) { $("storage-consent").setAttribute("aria-invalid","true"); fail("STORAGE_CONSENT_REQUIRED"); return; }
      pending(true,"Zapisuję ustawienia wyłącznie na tym komputerze…");
      try { await api("/api/save", {check_id:checkId, storage_consent:true}); token=""; $("password").value=""; form.reset(); form.hidden=true; $("finished").hidden=false; $("finished-title").focus(); }
      catch (exc) { fail(exc.message); }
      finally { pending(false,""); }
      return;
    }
    pending(true,"Sprawdzam logowanie i folder szkiców. Niczego nie zapisuję…");
    try { const result = await api("/api/check", payload()); checkId=result.check_id; if (!checkId) throw new Error("SETUP_OPERATION_FAILED"); $("password").value=""; review(result); $("checked-banner").hidden=false; $("next").textContent="Zapisz na tym komputerze"; status.textContent="Dostęp sprawdzony. Ostatni krok: świadoma zgoda na lokalny zapis."; }
    catch (exc) { $("password").value=""; showStep(2); fail(exc.message); }
    finally { pending(false, checkId ? "Dostęp sprawdzony. Ustawienia czekają na Twój zapis." : ""); }
  });
  signaturePreview();
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) { token=""; for (const control of form.querySelectorAll("input,textarea,button")) control.disabled=true; fail("SESSION_EXPIRED"); }
  else {
    let contextReady=false;
    pending(true,"Przygotowuję dane konta…");
    api("/api/context", {}).then(async result => {
      const prefill=result.prefill || {};
      setupContext={prefill,host_source:result.host_source || "manual",require_host_retype:Boolean(result.require_host_retype)};
      for (const id of ["email","account","entity","host"]) if (typeof prefill[id] === "string") $(id).value=prefill[id];
      accountEdited=Boolean(prefill.account);
      $("host-confirmed").checked=false;
      $("agent-context").hidden=Object.keys(prefill).length===0;
      $("account-details").open=!prefill.account || !prefill.entity;
      contextReady=true;
      if (prefill.email && !prefill.host) {
        try { const suggestion=await api("/api/suggest", {email:prefill.email}); if (suggestion.host) { $("host").value=suggestion.host; $("suggestion").textContent="Podpowiedź dostawcy — nadal wymaga Twojego potwierdzenia przed połączeniem."; } } catch (_) {}
      }
    }).catch(exc => { fail(exc.message); }).finally(() => { pending(false,""); if(!contextReady) for (const control of form.querySelectorAll("input,textarea,button")) control.disabled=true; });
  }
})();
