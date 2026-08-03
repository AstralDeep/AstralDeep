/* Thin server-driven UI client.
 * The orchestrator renders astralprims primitives to HTML (ROTE-adapted) and
 * pushes it over the WebSocket protocol. This client inserts the
 * server-rendered `html`, merges streamed chunks (keyed by component_id when
 * bridged to a workspace identity, else stream_id), initializes
 * Plotly charts, and posts user actions back as {type:"ui_event", action, payload}.
 * No build step. */
(function () {
  "use strict";
  if (window.self !== window.top) return; // don't connect inside auth-renew iframes

  var WS_URL = (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + "/ws";
  var API_URL = location.origin;
  var TOKEN_KEY = "astraldeep.token";
  // The shell-injected token bootstraps the first connect; every reconnect
  // re-fetches /auth/session (which silently refreshes server-side) instead of
  // reusing a stale token. Mock-auth dev works because /auth/session answers
  // for it.
  // A full shell load is an authentication boundary: its server-injected
  // token reflects the current signed-cookie session and MUST win over a
  // token left in this tab by a prior principal. The sessionStorage value is
  // only a fallback for legacy/test shells that do not inject a token.
  var token = window.__ASTRAL_TOKEN__ || sessionStorage.getItem(TOKEN_KEY) || "";

  var ws = null, attempts = 0, activeChatId = null, streamSeq = {}, firstConnect = true;
  var timelineMode = false; // read-only workspace history view
  var authRetried = false;  // one silent auth_required recovery per connection
  // The server says whether this page load resumes an existing session (false
  // only right after interactive sign-in). Echoed into the first register_ui;
  // reconnects within a page are always resumes.
  var serverResumed = (window.__ASTRAL_RESUMED__ !== false);

  // Feature 060 conversation continuity. Only the small active-chat locator is
  // durable; committed transcript/canvas remain server authoritative.
  var activeChatLocatorKey = null;
  var accountIdentityInitialized = false;
  var connectionGeneration = null;
  var requestState = null;
  var committedRevisionByChat = Object.create(null);
  var lastSnapshotIdByChat = Object.create(null);
  var seenSnapshotIdsByChat = Object.create(null);
  var transientOverlay = null;
  // Feature 060 server-owned status projections. These maps retain the highest
  // accepted sequence/pair so reordered WebSocket delivery cannot regress the
  // visible fallback or replace the first durable operation terminal.
  var operationStatusById = Object.create(null);
  var operationSubmissionByGeneration = Object.create(null);
  var operationSubmissionById = Object.create(null);
  var operationSubmissionOrdinal = 0;
  var agentLifecycleById = Object.create(null);
  var ACCOUNT_SESSION_KEY = "astraldeep.active_chat.account.v1";
  var VOICE_DEVICE_KEY = "astraldeep.voice.device.v1";
  var voiceDeviceId = loadVoiceDeviceId();
  var voicePermissionState = "not_determined";
  var voiceBinding = null;
  var voiceComposer = null;
  var voiceComposerRevision = -1;
  var voiceSession = null;
  var voiceLastSession = null;
  var voiceGrant = null;
  var voiceRoom = null;
  var voiceMediaJoined = false;
  var voiceMediaJoining = false;
  var voiceStream = null;
  var voiceActivation = null;
  var voiceTakeover = null;
  var voiceExpectedWorker = null;
  var voiceIntentionalDisconnect = false;
  var voiceSdkLoggingConfigured = false;
  var voiceTranscriptSequence = Object.create(null);
  // Final transcripts remain memory-only until the ordinary chat dispatcher
  // returns a fully correlated acknowledgement or terminal rejection. The
  // worker owns the other bounded replay copy; neither side uses storage.
  var voicePendingSubmissions = Object.create(null);
  var voicePendingSubmissionBytes = 0;
  var voiceCurrentResultId = null;
  var voicePendingTracks = Object.create(null);
  var voicePublishedTracks = Object.create(null);
  var voiceAnnouncementByTrack = Object.create(null);
  var voiceActivePlayout = Object.create(null);
  var voicePlayoutQueue = [];
  var voiceSubscribingTrackSid = null;
  var voiceAudioContext = null;
  var voiceLastAnnouncementSequence = 0;
  var voiceResultReservation = Object.create(null);
  var voiceResultQuantumIndex = Object.create(null);
  var voiceMediaTimers = [];
  var voiceLeaseTimer = null;
  var voiceBindingRenewTimer = null;
  var voiceRecovery = null;
  var voiceVisibleChatSync = null;
  var voiceVisibleChatTarget = null;
  var voicePendingEndFence = null;
  var voiceLifecycleSuspended = false;
  var voiceSuspensionPromise = null;
  var voiceRecoverySuppressed = false;
  var voiceStateEpoch = 0;
  var voicePlayoutSequence = 0;
  var voiceIgnoringTrackEnd = false;
  var VOICE_MAX_PENDING_SUBMISSIONS = 4;
  var VOICE_MAX_PENDING_BYTES = 48 * 1024;
  var VOICE_SUBMISSION_RETRY_MS = 2500;
  var VOICE_RECOVERY_DEADLINE_MS = 30000;
  var VOICE_RECOVERY_MAX_ATTEMPTS = 4;
  var ALLOWED_LOCATOR_CLEAR_REASONS = Object.freeze({
    explicit_new_chat: true,
    definitive_sign_out: true,
    account_switch: true,
    confirmed_deletion: true,
  });

  /** Return a cryptographically random canonical UUID4. */
  function randomUuid4() {
    if (crypto.randomUUID) return crypto.randomUUID();
    var bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    var hex = Array.prototype.map.call(bytes, function (value) {
      return value.toString(16).padStart(2, "0");
    }).join("");
    return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16)
      + "-" + hex.slice(16, 20) + "-" + hex.slice(20);
  }

  function isCanonicalUuid4(value) {
    return typeof value === "string"
      && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
  }

  /** Load or create the non-secret installation identity used for voice fencing. */
  function loadVoiceDeviceId() {
    var current;
    try { current = localStorage.getItem(VOICE_DEVICE_KEY); } catch (e) {}
    if (isCanonicalUuid4(current)) return current;
    current = randomUuid4();
    try { localStorage.setItem(VOICE_DEVICE_KEY, current); } catch (e) {}
    return current;
  }

  function isRfc3339Utc(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value)
      && !Number.isNaN(Date.parse(value));
  }

  /**
   * Build the browser-store key from the authenticated Keycloak issuer and
   * subject. The separator prevents ambiguous concatenations; only the digest
   * enters storage, so account display identity is never persisted.
   */
  async function activeChatStorageKey(issuer, subject) {
    if (typeof issuer !== "string" || !issuer || typeof subject !== "string" || !subject) return null;
    var encoder = new TextEncoder();
    var issuerBytes = encoder.encode(issuer);
    var subjectBytes = encoder.encode(subject);
    var input = new Uint8Array(issuerBytes.length + 1 + subjectBytes.length);
    input.set(issuerBytes, 0);
    input[issuerBytes.length] = 0;
    input.set(subjectBytes, issuerBytes.length + 1);
    var digest = await crypto.subtle.digest("SHA-256", input);
    var hex = Array.prototype.map.call(new Uint8Array(digest), function (value) {
      return value.toString(16).padStart(2, "0");
    }).join("");
    return "astraldeep.active_chat.v1." + hex;
  }

  function decodeTokenIdentity(rawToken, fallbackSubject) {
    if (fallbackSubject && (typeof rawToken !== "string" || rawToken.split(".").length !== 3)) {
      return { issuer: "astraldeep://mock-keycloak", subject: fallbackSubject || "test_user" };
    }
    if (typeof rawToken !== "string") return null;
    var pieces = rawToken.split(".");
    if (pieces.length !== 3) return null;
    try {
      var encoded = pieces[1].replace(/-/g, "+").replace(/_/g, "/");
      encoded += "=".repeat((4 - encoded.length % 4) % 4);
      var binary = atob(encoded);
      var bytes = new Uint8Array(binary.length);
      for (var index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
      var claims = JSON.parse(new TextDecoder().decode(bytes));
      if (typeof claims.iss !== "string" || !claims.iss || typeof claims.sub !== "string" || !claims.sub) return null;
      // These unverified claims namespace local storage only. Authorization
      // remains exclusively the server's verified-token responsibility.
      return { issuer: claims.iss, subject: claims.sub };
    } catch (e) { return null; }
  }

  function readActiveChatLocator() {
    if (!activeChatLocatorKey) return null;
    var raw;
    try { raw = localStorage.getItem(activeChatLocatorKey); } catch (e) { return null; }
    if (!raw) return null;
    try {
      var value = JSON.parse(raw);
      if (!value || value.schema_version !== 1 || Object.keys(value).sort().join(",") !== "chat_id,schema_version,updated_at") return null;
      if (!isCanonicalUuid4(value.chat_id) || !isRfc3339Utc(value.updated_at)) return null;
      return value.chat_id;
    } catch (e) { return null; }
  }

  /** Atomically persist the intentionally active non-credential locator. */
  function persistActiveChatLocator(chatId) {
    if (!activeChatLocatorKey || !isCanonicalUuid4(chatId)) return false;
    var value = { schema_version: 1, chat_id: chatId, updated_at: new Date().toISOString() };
    try { localStorage.setItem(activeChatLocatorKey, JSON.stringify(value)); return true; }
    catch (e) { return false; }
  }

  /**
   * Clear a locator only for the four definitive contract events. Transient
   * socket/auth/provider failures never call this function.
   */
  function clearActiveChatLocator(reason, chatId, storageKey) {
    // explicit_new_chat | definitive_sign_out | account_switch | confirmed_deletion
    if (!ALLOWED_LOCATOR_CLEAR_REASONS[reason]) return false;
    if (reason === "confirmed_deletion" && chatId !== activeChatId) return false;
    var key = storageKey || activeChatLocatorKey;
    if (key) { try { localStorage.removeItem(key); } catch (e) {} }
    if (!storageKey || storageKey === activeChatLocatorKey) {
      var clearedChatId = activeChatId;
      activeChatId = null;
      requestState = null;
      clearTransientOverlay();
      clearCommittedConversationView(reason, clearedChatId);
    }
    return true;
  }

  function clearCommittedConversationView(reason, chatId) {
    if (chat) chat.replaceChildren();
    if (canvas) { canvas.replaceChildren(); showCanvasEmpty(); }
    timelineMode = false;
    if (reason === "account_switch" || reason === "definitive_sign_out") {
      committedRevisionByChat = Object.create(null);
      lastSnapshotIdByChat = Object.create(null);
      seenSnapshotIdsByChat = Object.create(null);
      operationStatusById = Object.create(null);
      operationSubmissionByGeneration = Object.create(null);
      operationSubmissionById = Object.create(null);
      agentLifecycleById = Object.create(null);
    } else if (chatId) {
      delete committedRevisionByChat[chatId];
      delete lastSnapshotIdByChat[chatId];
      delete seenSnapshotIdsByChat[chatId];
    }
  }

  async function prepareAccountIdentity(rawToken, fallbackSubject) {
    var identity = decodeTokenIdentity(rawToken, fallbackSubject);
    if (!identity || !crypto.subtle) return false;
    var nextKey = await activeChatStorageKey(identity.issuer, identity.subject);
    if (!nextKey) return false;
    var previousKey = activeChatLocatorKey;
    if (!previousKey) {
      try { previousKey = sessionStorage.getItem(ACCOUNT_SESSION_KEY); } catch (e) {}
    }
    if (previousKey && previousKey !== nextKey) {
      clearActiveChatLocator("account_switch", null, previousKey);
      activeChatId = null;
      requestState = null;
    }
    var changed = activeChatLocatorKey !== nextKey;
    activeChatLocatorKey = nextKey;
    try { sessionStorage.setItem(ACCOUNT_SESSION_KEY, nextKey); } catch (e) {}
    if (!accountIdentityInitialized || changed) {
      accountIdentityInitialized = true;
      var selected = new URLSearchParams(location.search).get("chat");
      activeChatId = isCanonicalUuid4(selected) ? selected : readActiveChatLocator();
      if (activeChatId) persistActiveChatLocator(activeChatId);
    }
    return true;
  }

  function lastCommittedRenderRevision() {
    if (!activeChatId) return 0;
    return committedRevisionByChat[activeChatId] || 0;
  }

  function openRequest(purpose, chatId, suppliedGeneration) {
    requestState = {
      chatId: chatId || null,
      generation: isCanonicalUuid4(suppliedGeneration) ? suppliedGeneration : randomUuid4(),
      purpose: purpose,
      hydrationApplied: false,
      acceptedSnapshotId: null,
      acceptedSemantic: null,
      acceptedPresentation: null,
      lastFrameSequence: 0,
      snapshotApplied: false,
    };
    clearTransientOverlay();
    return requestState;
  }

  function selectActiveChat(chatId, purpose) {
    if (!isCanonicalUuid4(chatId)) return false;
    persistActiveChatLocator(chatId);
    activeChatId = chatId;
    syncVoiceVisibleChat(chatId);
    if (purpose) openRequest(purpose, chatId);
    return true;
  }

  /** Redirect to the server-side Keycloak login, preserving the destination. */
  function gotoLogin() {
    var next = encodeURIComponent(location.pathname + location.search);
    location.href = "/auth/login?next=" + next;
  }

  /** Refresh the session token via /auth/session (server refreshes silently).
   * Calls cb(true) when authenticated; redirects to login when the session
   * is truly gone and `redirect` is set. */
  function refreshToken(redirect, cb) {
    fetch(API_URL + "/auth/session", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.authenticated && j.access_token) {
          token = j.access_token;
          try { sessionStorage.setItem(TOKEN_KEY, token); } catch (e) {}
          prepareAccountIdentity(token, j.user_id).then(function () {
            if (cb) cb(true);
          }).catch(function () { if (cb) cb(true); });
        } else if (redirect) { gotoLogin(); }
        else if (cb) cb(false);
      })
      .catch(function () { if (cb) cb(false); });
  }

  var canvas = document.getElementById("astral-canvas");
  var chat = document.getElementById("astral-chat");
  // Shared cross-client canvas empty state: the node ships in shell.html; it is
  // detached on the first render with content and re-attached on canvas clears.
  var canvasEmpty = document.getElementById("astral-canvas-empty");
  function hideCanvasEmpty() {
    if (canvasEmpty && canvasEmpty.parentNode) canvasEmpty.parentNode.removeChild(canvasEmpty);
  }
  function showCanvasEmpty() {
    if (canvasEmpty && !canvasEmpty.parentNode) canvas.insertBefore(canvasEmpty, canvas.firstChild);
  }
  var statusEl = document.getElementById("astral-status");
  // Identifies the operation/submission that currently owns the shared status
  // line. A successful terminal frame may clear only its own progress; it
  // must not erase a different operation or an unrelated persistent notice.
  var statusOwner = null;
  var input = document.getElementById("astral-input");
  var form = document.getElementById("astral-form");
  var voiceControlsEl = document.getElementById("astral-voice-controls");
  var voiceFeedbackEl = document.getElementById("astral-voice-feedback");
  var voiceStatusEl = document.getElementById("astral-voice-status");
  var voiceTranscriptEl = document.getElementById("astral-voice-transcript");
  var voiceAudioResumeEl = document.getElementById("astral-voice-audio-resume");
  var voiceAudioHostEl = document.getElementById("astral-voice-audio");
  var voiceTurnNoticeEl = document.getElementById("astral-voice-turn-notice");
  var voiceTurnNoticeTitleEl = document.getElementById("astral-voice-turn-notice-title");
  var voiceTurnNoticeMessageEl = document.getElementById("astral-voice-turn-notice-message");
  var voiceTurnNoticeGuidanceEl = document.getElementById("astral-voice-turn-notice-guidance");
  var voiceTurnNoticeState = null;

  // ---- device detection (verbatim from useWebSocket.ts) ----
  function detectDeviceType() {
    var ua = navigator.userAgent.toLowerCase(), vw = window.innerWidth;
    if (/watch|watchos/.test(ua)) return "watch";
    if (/smart-?tv|hbbtv|netcast|viera|nettv|roku|web0s/.test(ua)) return "tv";
    if (vw <= 200) return "watch";
    if (/ipad|tablet|playbook|silk/.test(ua) || (vw > 480 && vw <= 1024 && /android/.test(ua))) return "tablet";
    if (/android|iphone|ipod|blackberry|iemobile|opera mini/.test(ua) || vw <= 480) return "mobile";
    if (vw <= 1024) return "tablet";
    return "browser";
  }
  function detectDeviceCapabilities() {
    var nav = navigator;
    return {
      device_type: detectDeviceType(),
      screen_width: window.screen.width, screen_height: window.screen.height,
      viewport_width: window.innerWidth, viewport_height: window.innerHeight,
      pixel_ratio: window.devicePixelRatio || 1,
      has_touch: (nav.maxTouchPoints || 0) > 0,
      has_geolocation: "geolocation" in navigator,
      has_microphone: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
      has_audio_output: typeof Audio !== "undefined",
      microphone_permission: voicePermissionState,
      full_duplex: true,
      transport: "livekit",
      has_camera: !!navigator.mediaDevices,
      has_file_system: true,
      connection_type: (nav.connection && nav.connection.effectiveType) || "unknown",
      user_agent: navigator.userAgent,
      // 066 additive capability envelope fields (server defaults are safe
      // when an older client omits them).
      reduced_motion: !!(window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches),
      pointer_type: (window.matchMedia
        && window.matchMedia("(pointer: coarse)").matches) ? "coarse" : "fine",
    };
  }

  // 066: live capability envelope — re-report on material change (resize
  // settle wired into the layout debounce; permission and connection changes
  // below) so server-side adaptation never goes stale. Rides the existing
  // `update_device` action (the server's live-viewport path: it diffs the
  // profile and re-adapts the persisted canvas). Debounced + de-duped.
  var lastCapabilitySignature = "";
  function maybeReportCapabilities(force) {
    if (!isSocketReady()) return;
    var caps = detectDeviceCapabilities();
    var sig = [caps.viewport_width, caps.viewport_height, caps.device_type,
      caps.pixel_ratio, caps.microphone_permission, caps.connection_type,
      caps.reduced_motion, caps.pointer_type].join("|");
    if (!force && sig === lastCapabilitySignature) return;
    lastCapabilitySignature = sig;
    action("update_device", { device: caps }, false);
  }
  if (navigator.connection && navigator.connection.addEventListener) {
    navigator.connection.addEventListener("change", function () {
      setTimeout(function () { maybeReportCapabilities(); }, 50);
    });
  }
  if (window.matchMedia) {
    var rmQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (rmQuery.addEventListener) rmQuery.addEventListener("change", function () {
      maybeReportCapabilities();
    });
  }
  if (navigator.permissions && navigator.permissions.query) {
    try {
      navigator.permissions.query({ name: "microphone" }).then(function (st) {
        if (st && st.addEventListener) st.addEventListener("change", function () {
          setTimeout(function () { maybeReportCapabilities(); }, 50);
        });
      }).catch(function () {});
    } catch (e) {}
  }

  // ROTE ↔ shell cooperation, split exactly like the Android client:
  // ROTE owns per-device COMPONENT adaptation — its authoritative
  // DeviceProfile (rote_config, after register_ui) is stamped on
  // body[data-rote-device], provisionally seeded from local detection so
  // phones never flash the desktop arrangement. The SHELL owns the
  // ARRANGEMENT via body[data-astral-layout]: "stacked" below 600 CSS px
  // (Android's COMPACT window-width class → StackedShell), "split"
  // otherwise — recomputed live on resize, like Compose recomputes its
  // windowSizeClass on every configuration change.
  function applyDeviceProfile(dt) {
    if (dt) document.body.setAttribute("data-rote-device", String(dt));
  }
  // 066 canvas-first modes: "stacked" (<700), "collapsed" (700-1023 default —
  // canvas full width, floating composer, transcript drawer), "split" (>=1024
  // default — right rail). The user's explicit choice persists per device and
  // wins over the width default at >=700px.
  var LAYOUT_PREF_KEY = "astral-chat-pref"; // "open" | "closed" | absent=auto
  function chatLayoutPref() {
    try {
      var v = localStorage.getItem(LAYOUT_PREF_KEY);
      return v === "open" || v === "closed" ? v : "auto";
    } catch (e) { return "auto"; }
  }
  function setChatLayoutPref(v) {
    try {
      if (v === "auto") localStorage.removeItem(LAYOUT_PREF_KEY);
      else localStorage.setItem(LAYOUT_PREF_KEY, v);
    } catch (e) {}
    applyLayoutClass();
  }
  function applyLayoutClass() {
    var w = window.innerWidth, mode;
    if (w < 700) mode = "stacked";
    else {
      var pref = chatLayoutPref();
      // The rail is only offerable where it leaves a usable composer: below
      // 1024 a persisted "keep it open" would crush the input to a few
      // characters (066 FR-004), so the width bound wins over the preference.
      if (pref === "closed") mode = "collapsed";
      else if (pref === "open" && w >= 1024) mode = "split";
      else mode = w >= 1024 ? "split" : "collapsed";
    }
    if (document.body.getAttribute("data-astral-layout") !== mode) {
      document.body.setAttribute("data-astral-layout", mode);
      if (mode !== "stacked") { // stacked-only chrome state must not linger
        document.body.classList.remove("astral-history-open", "astral-msgs-open");
      }
      if (mode !== "collapsed") document.body.classList.remove("astral-chat-open");
      if (mode === "split") clearChatUnread();
    }
  }
  applyDeviceProfile(detectDeviceType());
  applyLayoutClass();
  var layoutResizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(layoutResizeTimer);
    layoutResizeTimer = setTimeout(function () {
      applyLayoutClass();
      maybeReportCapabilities();
    }, 220);
  });

  // ---- 066: chat visibility controls + unread accounting ----
  var collapseBtn = document.getElementById("astral-collapse-btn");
  var chatToggleBtn = document.getElementById("astral-chat-toggle");
  var chatUnreadEl = document.getElementById("astral-chat-unread");
  var chatUnread = 0;
  function clearChatUnread() {
    chatUnread = 0;
    if (chatUnreadEl) { chatUnreadEl.hidden = true; chatUnreadEl.textContent = "0"; }
  }
  function noteAssistantActivity() {
    var layout = document.body.getAttribute("data-astral-layout");
    var hidden = (layout === "collapsed" && !document.body.classList.contains("astral-chat-open"))
      || (layout === "stacked" && !document.body.classList.contains("astral-msgs-open"));
    if (!hidden) return;
    chatUnread++;
    if (chatUnreadEl) {
      chatUnreadEl.hidden = false;
      chatUnreadEl.textContent = chatUnread > 9 ? "9+" : String(chatUnread);
    }
    if (chatToggleBtn && window.matchMedia
        && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      chatToggleBtn.classList.remove("astral-peek");
      void chatToggleBtn.offsetWidth;
      chatToggleBtn.classList.add("astral-peek");
    }
  }
  if (collapseBtn) collapseBtn.addEventListener("click", function () {
    setChatLayoutPref("closed");
  });
  if (chatToggleBtn) chatToggleBtn.addEventListener("click", function () {
    var open = document.body.classList.toggle("astral-chat-open");
    chatToggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
    chatToggleBtn.setAttribute("title", open ? "Hide conversation" : "Show conversation");
    if (open) clearChatUnread();
  });
  // Re-pin the rail from collapsed mode: double-click the transcript toggle.
  if (chatToggleBtn) chatToggleBtn.addEventListener("dblclick", function () {
    setChatLayoutPref("open");
  });
  // Coarse-pointer component chrome: tap a component to reveal its actions.
  document.addEventListener("click", function (e) {
    if (!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches)) return;
    if (e.target.closest && e.target.closest("button, a, input, select, textarea")) return;
    var comp = e.target.closest && e.target.closest(".astral-component");
    if (!comp) return;
    var open = comp.classList.toggle("astral-chrome-open");
    if (open) {
      var others = document.querySelectorAll(".astral-component.astral-chrome-open");
      for (var i = 0; i < others.length; i++) {
        if (others[i] !== comp) others[i].classList.remove("astral-chrome-open");
      }
    }
  });

  function configureStatusElement(node) {
    if (!node) return null;
    node.setAttribute("role", "status");
    node.setAttribute("aria-label", "Application status");
    node.setAttribute("aria-live", "polite");
    node.setAttribute("aria-atomic", "true");
    node.setAttribute("aria-busy", "false");
    return node;
  }
  configureStatusElement(statusEl);

  function setStatus(s, busy, owner) {
    var current = document.getElementById("astral-status");
    if (current !== statusEl) statusEl = configureStatusElement(current);
    if (!statusEl) return;
    statusOwner = owner || null;
    statusEl.textContent = s || "";
    statusEl.setAttribute("aria-busy", busy === true ? "true" : "false");
    statusEl.setAttribute("data-status-state", s ? (busy === true ? "busy" : "settled") : "idle");
    // 066: mirror turn status beside the composer — the topbar is too far
    // from the conversation to carry progress/failure alone.
    var turnStatus = document.getElementById("astral-turn-status");
    if (turnStatus) {
      turnStatus.textContent = s || "";
      turnStatus.hidden = !s;
      turnStatus.setAttribute("data-busy", busy === true ? "true" : "false");
    }
  }

  // ---- Feature 065: server-owned conversational voice + local media adapter ----
  var VOICE_ACTIONS = Object.freeze({
    voice_session_start: true,
    voice_session_takeover: true,
    voice_session_end: true,
    voice_microphone_set: true,
    voice_speech_stop: true,
    voice_speech_mute_set: true,
    voice_visible_chat_update: true,
    voice_sensitive_recap_request: true,
  });
  var VOICE_CONTROL_ORDER = Object.freeze([
    "voice_session_start",
    "voice_session_takeover",
    "voice_session_end",
    "voice_microphone_set",
    "voice_speech_stop",
    "voice_speech_mute_set",
    "voice_visible_chat_update",
    "voice_sensitive_recap_request",
  ]);
  var VOICE_STATES = Object.freeze({
    off: true, unavailable: true, connecting: true, greeting: true,
    listening: true, speech_detected: true, transcribing: true,
    acknowledging: true, processing: true, waiting_on_user: true,
    speaking_progress: true, speaking_result: true, muted: true,
    suspended: true, reconnecting: true, error: true, ended: true,
  });
  var VOICE_TURN_STATES = Object.freeze({
    recognizing: true, submitting: true, accepted: true, processing: true,
    waiting_on_user: true, succeeded: true, failed: true, refused: true,
    cancelled: true, abandoned: true,
  });
  var VOICE_SPEECH_OUTCOMES = Object.freeze({
    source_finished: true,
    failed: true,
    suppressed: true,
  });
  var VOICE_REQUEST_TERMINAL_TITLES = Object.freeze({
    failed: "Voice request did not complete.",
    refused: "Voice request did not start.",
    cancelled: "Voice request did not complete because it was cancelled.",
    abandoned: "Voice request did not complete.",
    speech_error: "Speech playback failed.",
  });
  var VOICE_REQUEST_NOTICE_CLEAR_STATES = Object.freeze({
    recognizing: true,
    submitting: true,
    accepted: true,
    processing: true,
    succeeded: true,
  });
  var VOICE_SUBMISSION_REJECTION_REASONS = Object.freeze({
    capacity_exhausted: true,
    chat_unavailable: true,
    invalid_binding: true,
    invalid_proof: true,
    proof_expired: true,
    permission_denied: true,
    stale_session: true,
    malformed_final: true,
  });
  var VOICE_STATE_TEXT = Object.freeze({
    off: "Voice conversation is off.",
    unavailable: "Voice is unavailable. You can keep typing messages.",
    connecting: "Connecting voice conversation…",
    greeting: "AstralDeep is greeting you.",
    listening: "Listening…",
    speech_detected: "I hear you.",
    transcribing: "Turning your speech into text…",
    acknowledging: "On it.",
    processing: "Working on your request…",
    waiting_on_user: "Waiting for your response.",
    speaking_progress: "Speaking a progress update…",
    speaking_result: "Speaking the completed result…",
    muted: "Assistant speech is muted.",
    suspended: "Voice is paused while this page is not active.",
    reconnecting: "Voice connection was interrupted. Typed chat is still available.",
    error: "Voice stopped because of an error. You can keep typing messages.",
    ended: "Voice conversation ended. Accepted requests will keep running.",
  });
  var VOICE_REASON_TEXT = Object.freeze({
    permission_denied: "Microphone permission was denied. Allow it in browser settings or keep typing.",
    permission_restricted: "Microphone permission is restricted. You can keep typing messages.",
    no_microphone: "No microphone is available. Connect one or keep typing messages.",
    no_audio_output: "No audio output is available. You can keep typing messages.",
    media_unavailable: "Browser audio is unavailable. You can keep typing messages.",
    takeover_required: "Voice is active on another device. Choose Take over to continue here.",
    idle_expired: "Voice ended after being idle. Accepted requests will keep running.",
    auth_expired: "Voice ended because your session expired. Typed chat is still available.",
    backgrounded: "Voice is paused while this page is hidden.",
    audio_interrupted: "Voice paused because audio was interrupted.",
    network_interrupted: "Voice connection was interrupted. Typed chat is still available.",
    stale_generation: "This voice connection is no longer current.",
    ended_by_user: "Voice conversation ended. Accepted requests will keep running.",
    speech_error: "Assistant speech failed. The text result may still be available in chat. You can keep typing messages.",
    media_error: "Voice media failed. You can retry or keep typing.",
  });

  function voiceCapability() {
    return {
      has_microphone: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
      has_audio_output: typeof Audio !== "undefined",
      microphone_permission: voicePermissionState,
      full_duplex: true,
      transport: "livekit",
    };
  }

  function voiceMessage(state, reason, message) {
    if (typeof message === "string" && message.trim()) {
      if (reason === "speech_error"
          && !/text result may still be available/i.test(message)) {
        return message.trim() + " The text result may still be available in chat.";
      }
      return message.trim();
    }
    return VOICE_REASON_TEXT[reason] || VOICE_STATE_TEXT[state] || VOICE_STATE_TEXT.error;
  }

  function setVoiceFeedback(state, reason, message, forceVisible) {
    if (!VOICE_STATES[state]) state = "error";
    reason = typeof reason === "string" && reason ? reason : "internal_error";
    if (voiceFeedbackEl) {
      voiceFeedbackEl.setAttribute("data-state", state);
      voiceFeedbackEl.setAttribute("data-reason", reason);
      voiceFeedbackEl.hidden = !forceVisible && state === "off" && !message;
    }
    if (voiceControlsEl) {
      voiceControlsEl.setAttribute("data-state", state);
      voiceControlsEl.setAttribute("data-reason", reason);
    }
    if (voiceStatusEl) voiceStatusEl.textContent = voiceMessage(state, reason, message);
  }

  function validVoiceTurnMessage(frame) {
    if (!Object.prototype.hasOwnProperty.call(frame, "message")) return true;
    return typeof frame.message === "string" && Array.from(frame.message).length <= 240;
  }

  function showVoiceRequestTerminal(frame, guidance) {
    var title = VOICE_REQUEST_TERMINAL_TITLES[frame.state];
    if (!title || !voiceTurnNoticeEl || !voiceTurnNoticeTitleEl
        || !voiceTurnNoticeMessageEl) return;
    if (voiceTurnNoticeState && isCanonicalUuid4(frame.turn_id)
        && isCanonicalUuid4(voiceTurnNoticeState.turn_id)
        && isRfc3339Utc(frame.occurred_at)
        && isRfc3339Utc(voiceTurnNoticeState.occurred_at)
        && Date.parse(frame.occurred_at) < Date.parse(voiceTurnNoticeState.occurred_at)) return;
    voiceTurnNoticeEl.setAttribute("data-state", frame.state);
    voiceTurnNoticeTitleEl.textContent = title;
    if (Object.prototype.hasOwnProperty.call(frame, "message")) {
      // The server-owned safe message is contract-bounded and rendered only
      // as text. Keep its wording verbatim rather than paraphrasing it.
      voiceTurnNoticeMessageEl.textContent = frame.message;
      voiceTurnNoticeMessageEl.hidden = false;
    } else {
      voiceTurnNoticeMessageEl.textContent = "";
      voiceTurnNoticeMessageEl.hidden = true;
    }
    voiceTurnNoticeState = {
      turn_id: frame.turn_id,
      occurred_at: frame.occurred_at,
    };
    if (voiceTurnNoticeGuidanceEl) {
      voiceTurnNoticeGuidanceEl.textContent = guidance || "Typed chat remains available.";
    }
    voiceTurnNoticeEl.hidden = false;
  }

  function clearVoiceRequestTerminal(preserveFence) {
    if (preserveFence !== true) voiceTurnNoticeState = null;
    if (voiceTurnNoticeEl) {
      voiceTurnNoticeEl.hidden = true;
      voiceTurnNoticeEl.removeAttribute("data-state");
    }
    if (voiceTurnNoticeTitleEl) voiceTurnNoticeTitleEl.textContent = "";
    if (voiceTurnNoticeMessageEl) {
      voiceTurnNoticeMessageEl.textContent = "";
      voiceTurnNoticeMessageEl.hidden = true;
    }
    if (voiceTurnNoticeGuidanceEl) {
      voiceTurnNoticeGuidanceEl.textContent = "Typed chat remains available.";
    }
  }

  function clearVoiceRequestTerminalForNewerTurn(frame) {
    if (!VOICE_REQUEST_NOTICE_CLEAR_STATES[frame.state]
        || !isCanonicalUuid4(frame.turn_id) || !isRfc3339Utc(frame.occurred_at)) return;
    var previous = voiceTurnNoticeState;
    if (previous && isCanonicalUuid4(previous.turn_id)
        && isRfc3339Utc(previous.occurred_at)
        && Date.parse(frame.occurred_at) < Date.parse(previous.occurred_at)) return;
    voiceTurnNoticeState = {
      turn_id: frame.turn_id,
      occurred_at: frame.occurred_at,
    };
    if (!previous || frame.turn_id !== previous.turn_id) clearVoiceRequestTerminal(true);
  }

  function showVoiceResultSpeechFailure(manifest) {
    if (!manifest || manifest.kind !== "result" || !isCanonicalUuid4(manifest.turn_id)) return false;
    if (voiceTurnNoticeState && isCanonicalUuid4(voiceTurnNoticeState.turn_id)
        && voiceTurnNoticeState.turn_id !== manifest.turn_id) return false;
    var occurredAt = new Date().toISOString();
    if (voiceTurnNoticeState && voiceTurnNoticeState.turn_id === manifest.turn_id
        && isRfc3339Utc(voiceTurnNoticeState.occurred_at)) {
      occurredAt = voiceTurnNoticeState.occurred_at;
    }
    showVoiceRequestTerminal({
      state: "speech_error",
      turn_id: manifest.turn_id,
      occurred_at: occurredAt,
      message: "The result audio could not be delivered.",
    }, "The text result is still available in the conversation. Typed chat remains available.");
    return true;
  }

  function showVoiceAudioResume(message) {
    if (voiceAudioResumeEl) voiceAudioResumeEl.hidden = false;
    var state = voiceFeedbackEl && voiceFeedbackEl.getAttribute("data-state") || "connecting";
    var reason = voiceFeedbackEl && voiceFeedbackEl.getAttribute("data-reason") || "media_unavailable";
    setVoiceFeedback(state, reason, message || "Voice audio needs permission to play. Choose Enable voice audio.", true);
  }

  function hideVoiceAudioResume() {
    if (voiceAudioResumeEl) voiceAudioResumeEl.hidden = true;
  }

  function voiceControlHeaders(contentType) {
    var headers = {
      Authorization: "Bearer " + token,
      "X-Astral-Device-Id": voiceDeviceId,
      "X-Astral-Connection-Generation": connectionGeneration || "",
      "X-Astral-Voice-Control-Binding": voiceBinding ? voiceBinding.binding : "",
    };
    if (contentType) headers["Content-Type"] = contentType;
    return headers;
  }

  async function voiceRequest(path, method, body) {
    var response;
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timeout = controller ? setTimeout(function () { controller.abort(); }, 20000) : null;
    try {
      response = await fetch(API_URL + path, {
        method: method,
        headers: voiceControlHeaders(body === undefined ? null : "application/json"),
        credentials: "same-origin",
        cache: "no-store",
        signal: controller ? controller.signal : undefined,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      if (timeout) clearTimeout(timeout);
      return { ok: false, status: 0, body: null, reason: "network_interrupted" };
    }
    if (timeout) clearTimeout(timeout);
    var payload = null;
    if (response.status !== 204) {
      try { payload = await response.json(); } catch (e) {}
    }
    return {
      ok: response.ok,
      status: response.status,
      body: payload,
      reason: response.status === 401 ? "auth_expired" : null,
    };
  }

  function voiceBindingIsCurrent() {
    return voiceBinding
      && voiceBinding.device_id === voiceDeviceId
      && voiceBinding.connection_generation === connectionGeneration
      && Date.parse(voiceBinding.expires_at) > Date.now();
  }

  function clearVoiceBindingRenewal() {
    if (voiceBindingRenewTimer != null) clearTimeout(voiceBindingRenewTimer);
    voiceBindingRenewTimer = null;
  }

  function requestFreshVoiceBinding(bindingId) {
    if (bindingId && (!voiceBinding || voiceBinding.binding_id !== bindingId)) return;
    clearVoiceBindingRenewal();
    voiceBinding = null;
    if (ws && ws.readyState === 1) {
      try { ws.close(); } catch (e) {}
    }
  }

  function scheduleVoiceBindingRenewal(binding) {
    clearVoiceBindingRenewal();
    var lifetimeMs = Date.parse(binding.expires_at) - Date.now();
    var leadMs = Math.min(30000, Math.max(1000, Math.floor(lifetimeMs / 10)));
    var delayMs = Math.max(250, lifetimeMs - leadMs);
    voiceBindingRenewTimer = setTimeout(function () {
      requestFreshVoiceBinding(binding.binding_id);
    }, delayMs);
  }

  function consumeVoiceControlBinding(frame) {
    if (!frame || frame.type !== "voice_control_binding" || frame.schema_version !== "1"
        || frame.device_id !== voiceDeviceId || frame.connection_generation !== connectionGeneration
        || !isCanonicalUuid4(frame.binding_id) || typeof frame.binding !== "string"
        || frame.binding.length < 32 || frame.binding.length > 512
        || !/^[A-Za-z0-9._~-]+$/.test(frame.binding)
        || !isRfc3339Utc(frame.expires_at) || Date.parse(frame.expires_at) <= Date.now()
        || Date.parse(frame.expires_at) > Date.now() + 10 * 60 * 1000) return false;
    voiceBinding = {
      device_id: frame.device_id,
      connection_generation: frame.connection_generation,
      binding_id: frame.binding_id,
      binding: frame.binding,
      expires_at: frame.expires_at,
    };
    scheduleVoiceBindingRenewal(voiceBinding);
    if (voicePendingEndFence) {
      var pendingEnd = voicePendingEndFence;
      voicePendingEndFence = null;
      bestEffortEndVoice(pendingEnd).then(function () { voiceLastSession = null; });
    } else {
      maybeBeginVoiceRecovery("network_interrupted");
    }
    if (activeChatId && !voiceRecovery) syncVoiceVisibleChat(activeChatId);
    resendPendingVoiceSubmissions();
    return true;
  }

  function hasExactKeys(value, required, optional) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    var allowed = Object.create(null);
    required.forEach(function (key) { allowed[key] = true; });
    (optional || []).forEach(function (key) { allowed[key] = true; });
    var keys = Object.keys(value);
    return required.every(function (key) { return Object.prototype.hasOwnProperty.call(value, key); })
      && keys.every(function (key) { return allowed[key] === true; });
  }

  function validComposerControl(control) {
    return control && typeof control === "object" && !Array.isArray(control)
      && typeof control.key === "string" && control.key
      && VOICE_ACTIONS[control.action]
      && typeof control.label === "string" && control.label
      && typeof control.icon === "string" && control.icon
      && typeof control.visible === "boolean" && typeof control.enabled === "boolean"
      && typeof control.pressed === "boolean" && typeof control.busy === "boolean";
  }

  function validComposerFrame(frame) {
    if (!hasExactKeys(frame, ["type", "schema_version", "revision", "connection_generation", "voice"])
        || frame.type !== "composer_state" || frame.schema_version !== "1"
        || frame.connection_generation !== connectionGeneration
        || !Number.isSafeInteger(frame.revision) || frame.revision < 0
        || !frame.voice || typeof frame.voice !== "object" || Array.isArray(frame.voice)
        || !VOICE_STATES[frame.voice.state] || !Array.isArray(frame.voice.controls)
        || typeof frame.voice.available !== "boolean"
        || typeof frame.voice.reason !== "string" || frame.voice.output_locale !== "en-US") return false;
    if (frame.voice.controls.length !== VOICE_CONTROL_ORDER.length) return false;
    var seen = Object.create(null);
    for (var index = 0; index < frame.voice.controls.length; index++) {
      var control = frame.voice.controls[index];
      if (!validComposerControl(control) || control.action !== VOICE_CONTROL_ORDER[index]
          || seen[control.key]) return false;
      seen[control.key] = true;
    }
    return true;
  }

  // 066: real SVG icons for the composer voice controls (static trusted
  // markup keyed by the server's data-icon contract).
  var VOICE_ICONS = {
    "microphone": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="23"></line><line x1="8" y1="23" x2="16" y2="23"></line></svg>',
    "device-transfer": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>',
    "stop": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"></rect></svg>',
    "speaker-stop": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><line x1="23" y1="9" x2="17" y2="15"></line><line x1="17" y1="9" x2="23" y2="15"></line></svg>',
    "speaker-muted": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><line x1="22" y1="3" x2="3" y2="22"></line></svg>',
    "chat": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>',
    "speaker-consent": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M15.54 8.46a5 5 0 0 1 0 7.07"></path><path d="M19.07 4.93a10 10 0 0 1 0 14.14"></path></svg>',
  };

  // 066: the voice affordance is ALWAYS present — this client-local default
  // renders before any composer_state frame arrives (and again on socket
  // teardown) so an absent/failed server projection can never leave the
  // composer without a voice control.
  function renderDefaultVoiceControl(reasonText) {
    if (!voiceControlsEl) return;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "astral-voice-control";
    button.setAttribute("data-voice-key", "voice-start");
    button.setAttribute("data-voice-action", "voice_session_start");
    button.setAttribute("data-icon", "microphone");
    button.setAttribute("data-default", "1");
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-busy", "false");
    button.setAttribute("aria-label", "Start voice conversation");
    button.setAttribute("title", reasonText || "Checking voice availability…");
    button.disabled = true;
    button.innerHTML = VOICE_ICONS.microphone;
    var label = document.createElement("span");
    label.className = "astral-sr-only";
    label.textContent = "Start voice conversation";
    button.appendChild(label);
    voiceControlsEl.replaceChildren(button);
  }

  function renderVoiceControls(voice) {
    if (!voiceControlsEl) return;
    var fragment = document.createDocumentFragment();
    voice.controls.forEach(function (control) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "astral-voice-control";
      button.setAttribute("data-voice-key", control.key);
      button.setAttribute("data-voice-action", control.action);
      button.setAttribute("data-icon", control.icon);
      button.setAttribute("aria-label", control.label);
      button.setAttribute("title", control.label);
      button.setAttribute("aria-pressed", control.pressed ? "true" : "false");
      button.setAttribute("aria-busy", control.busy ? "true" : "false");
      button.disabled = !control.enabled;
      button.hidden = !control.visible;
      button.innerHTML = VOICE_ICONS[control.icon] || VOICE_ICONS.microphone;
      var label = document.createElement("span");
      label.className = "astral-sr-only";
      label.textContent = control.label;
      button.appendChild(label);
      button.addEventListener("click", function () { onVoiceControlClick(control); });
      fragment.appendChild(button);
    });
    voiceControlsEl.replaceChildren(fragment);
  }

  function consumeComposerState(frame) {
    if (!validComposerFrame(frame) || frame.revision <= voiceComposerRevision) return false;
    voiceComposerRevision = frame.revision;
    voiceComposer = frame.voice;
    renderVoiceControls(frame.voice);
    setVoiceFeedback(frame.voice.state, frame.voice.reason, frame.voice.message, frame.voice.state !== "off");
    if (frame.voice.reason === "takeover_required" && frame.voice.session_id) {
      voiceTakeover = {
        session_id: frame.voice.session_id,
        generation: frame.voice.generation,
        media_grant_revision: frame.voice.media_grant_revision,
      };
    }
    if (frame.voice.state === "ended") {
      voiceRecoverySuppressed = true;
      teardownVoiceMedia(true);
      voiceLastSession = null;
      voicePendingEndFence = null;
    } else if (frame.voice.state === "off" && !frame.voice.session_id) {
      clearVoiceRecovery();
      clearVoiceRequestTerminal();
      voiceLastSession = null;
      voicePendingEndFence = null;
    } else {
      maybeBeginVoiceRecovery(frame.voice.reason === "backgrounded"
        ? "backgrounded" : "network_interrupted");
    }
    return true;
  }

  function roomEventName(name) {
    return window.LivekitClient && window.LivekitClient.RoomEvent
      ? window.LivekitClient.RoomEvent[name] : null;
  }

  /** Keep credentialed signaling/SDP out of browser diagnostics. */
  function configureVoiceSdkLogging() {
    if (voiceSdkLoggingConfigured) return true;
    if (!window.LivekitClient || typeof window.LivekitClient.setLogLevel !== "function") return false;
    try {
      window.LivekitClient.setLogLevel("silent");
      voiceSdkLoggingConfigured = true;
      return true;
    } catch (e) {
      return false;
    }
  }

  function ensureVoiceAudioContext() {
    if (voiceAudioContext && voiceAudioContext.state !== "closed") return voiceAudioContext;
    var AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (typeof AudioContextClass !== "function") return null;
    try {
      voiceAudioContext = new AudioContextClass({ latencyHint: "interactive", sampleRate: 24000 });
    } catch (e) {
      try { voiceAudioContext = new AudioContextClass({ latencyHint: "interactive" }); }
      catch (_error) { voiceAudioContext = null; }
    }
    return voiceAudioContext;
  }

  function wireVoiceRoom(room) {
    var dataReceived = roomEventName("DataReceived");
    var trackPublished = roomEventName("TrackPublished");
    var trackSubscribed = roomEventName("TrackSubscribed");
    var trackUnpublished = roomEventName("TrackUnpublished");
    var trackUnsubscribed = roomEventName("TrackUnsubscribed");
    var participantDisconnected = roomEventName("ParticipantDisconnected");
    var disconnected = roomEventName("Disconnected");
    if (dataReceived) room.on(dataReceived, function (payload, participant, _kind, topic) {
      consumeVoiceRoomData(payload, participant, topic);
    });
    if (trackPublished) room.on(trackPublished, function (publication, participant) {
      consumeVoicePublishedTrack(publication, participant);
    });
    if (trackSubscribed) room.on(trackSubscribed, function (track, publication, participant) {
      consumeVoiceAudioTrack(track, publication, participant);
    });
    if (trackUnpublished) room.on(trackUnpublished, function (publication) {
      removeVoicePublishedTrack(publication && publication.trackSid);
    });
    if (trackUnsubscribed) room.on(trackUnsubscribed, function (track, publication) {
      interruptVoiceAudioTrack(track, publication);
    });
    if (participantDisconnected) room.on(participantDisconnected, function (participant) {
      if (voiceRoom === room && !voiceIntentionalDisconnect
          && participant && participant.identity === voiceExpectedWorker) {
        beginVoiceRecovery("network_interrupted");
      }
    });
    if (disconnected) room.on(disconnected, function () {
      if (voiceRoom === room && !voiceIntentionalDisconnect) beginVoiceRecovery("network_interrupted");
    });
  }

  function createVoiceRoom(startAudio) {
    if (voiceRoom) return voiceRoom;
    if (!window.LivekitClient || typeof window.LivekitClient.Room !== "function"
        || !configureVoiceSdkLogging()) {
      setVoiceFeedback("unavailable", "media_unavailable", "Voice media is unavailable in this browser. You can keep typing.", true);
      return null;
    }
    try {
      voiceRoom = new window.LivekitClient.Room({ adaptiveStream: false, dynacast: false });
      voiceMediaJoined = false;
      voiceMediaJoining = false;
      wireVoiceRoom(voiceRoom);
      if (startAudio) ensureVoiceAudioContext();
      if (startAudio && typeof voiceRoom.startAudio === "function") {
        Promise.resolve(voiceRoom.startAudio()).then(hideVoiceAudioResume).catch(function () {
          showVoiceAudioResume();
        });
      }
      return voiceRoom;
    } catch (e) {
      voiceRoom = null;
      setVoiceFeedback("unavailable", "media_unavailable", null, true);
      return null;
    }
  }

  function createVoiceRoomFromGesture() {
    return createVoiceRoom(true);
  }

  function stopVoiceCapture() {
    if (!voiceStream) return;
    voiceIgnoringTrackEnd = true;
    try {
      voiceStream.getTracks().forEach(function (track) {
        track.enabled = false;
        track.stop();
      });
    } catch (e) {}
    voiceIgnoringTrackEnd = false;
    voiceStream = null;
  }

  function clearVoiceMediaTimers() {
    voiceMediaTimers.forEach(function (timer) { clearTimeout(timer); });
    voiceMediaTimers = [];
  }

  function clearVoiceAudioElements() {
    clearVoiceMediaTimers();
    voicePlayoutQueue = [];
    voiceSubscribingTrackSid = null;
    Object.keys(voiceActivePlayout).forEach(function (announcementId) {
      var active = voiceActivePlayout[announcementId];
      if (active) finishVoiceTrack(active, active.started ? "interrupted" : null, true);
    });
    Object.keys(voicePublishedTracks).forEach(function (sid) {
      var published = voicePublishedTracks[sid];
      try { if (published && published.publication) published.publication.setSubscribed(false); } catch (e) {}
    });
    Object.keys(voicePendingTracks).forEach(function (sid) {
      stopVoiceAudioTrack(voicePendingTracks[sid] && voicePendingTracks[sid].track);
    });
    if (voiceAudioHostEl) {
      Array.prototype.forEach.call(voiceAudioHostEl.querySelectorAll("audio"), function (audio) {
        try { audio.pause(); } catch (e) {}
        audio.removeAttribute("src");
        try { audio.load(); } catch (e) {}
      });
      voiceAudioHostEl.replaceChildren();
    }
    voicePendingTracks = Object.create(null);
    voicePublishedTracks = Object.create(null);
    voiceAnnouncementByTrack = Object.create(null);
    voiceActivePlayout = Object.create(null);
    voiceResultReservation = Object.create(null);
    voiceResultQuantumIndex = Object.create(null);
    if (voiceAudioContext) {
      try { Promise.resolve(voiceAudioContext.close()).catch(function () {}); } catch (e) {}
      voiceAudioContext = null;
    }
  }

  function teardownVoiceMedia(clearSession) {
    // Invalidate every in-flight media/control continuation, including joins
    // that may resolve after a replacement room has already been installed.
    voiceStateEpoch += 1;
    voiceVisibleChatSync = null;
    if (clearSession) clearVoiceRecovery();
    stopVoiceLeaseHeartbeat();
    stopVoiceCapture();
    clearVoiceAudioElements();
    if (voiceRoom) {
      voiceIntentionalDisconnect = true;
      try { voiceRoom.disconnect(); } catch (e) {}
      voiceIntentionalDisconnect = false;
    }
    voiceRoom = null;
    voiceMediaJoined = false;
    voiceMediaJoining = false;
    voiceGrant = null;
    voiceExpectedWorker = null;
    if (voiceActivation && voiceActivation.timeout) clearTimeout(voiceActivation.timeout);
    voiceActivation = null;
    hideVoiceAudioResume();
    if (clearSession) {
      if (voiceSession) voiceLastSession = voiceSession;
      voiceSession = null;
      voiceVisibleChatTarget = null;
      voiceTranscriptSequence = Object.create(null);
      voiceLastAnnouncementSequence = 0;
      voiceResultReservation = Object.create(null);
      voiceResultQuantumIndex = Object.create(null);
      voiceCurrentResultId = null;
    }
  }

  function stopVoiceLeaseHeartbeat() {
    if (voiceLeaseTimer != null) clearInterval(voiceLeaseTimer);
    voiceLeaseTimer = null;
  }

  function startVoiceLeaseHeartbeat() {
    if (voiceLeaseTimer != null || !voiceSession || !voiceSession.foreground_active) return;
    voiceLeaseTimer = setInterval(function () {
      if (!voiceSession || !voiceSession.foreground_active || !voiceBindingIsCurrent()
          || document.visibilityState === "hidden") {
        stopVoiceLeaseHeartbeat();
        return;
      }
      // A generation-fenced semantic no-op renews only the crash/reconnect
      // lease. It is deliberately not an interaction and cannot postpone the
      // server-owned five-minute true-idle deadline.
      patchVoiceSession({
        foreground_active: true,
        foreground_reason: "foreground",
      });
    }, 20000);
  }

  function voicePermissionReason(error) {
    if (!error) return "media_error";
    if (error.name === "NotAllowedError" || error.name === "SecurityError") return "permission_denied";
    if (error.name === "NotFoundError" || error.name === "DevicesNotFoundError") return "no_microphone";
    return "media_error";
  }

  async function acquireVoiceMicrophone() {
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
      voicePermissionState = "restricted";
      throw Object.assign(new Error("microphone unavailable"), { name: "NotFoundError" });
    }
    var stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
    var tracks = stream && stream.getAudioTracks ? stream.getAudioTracks() : [];
    if (tracks.length !== 1 || tracks[0].readyState === "ended") {
      try { stream.getTracks().forEach(function (track) { track.stop(); }); } catch (e) {}
      throw Object.assign(new Error("microphone unavailable"), { name: "NotFoundError" });
    }
    voicePermissionState = "authorized";
    voiceStream = stream;
    tracks[0].enabled = false;
    tracks[0].addEventListener("ended", function () {
      if (!voiceIgnoringTrackEnd) handleVoiceMediaLoss("permission_denied");
    }, { once: true });
    return stream;
  }

  function sessionFence(source) {
    if (!source || !isCanonicalUuid4(source.session_id)
        || !Number.isSafeInteger(source.generation) || source.generation < 1
        || !Number.isSafeInteger(source.media_grant_revision)
        || source.media_grant_revision < 1) return null;
    return {
      session_id: source.session_id,
      generation: source.generation,
      media_grant_revision: source.media_grant_revision,
    };
  }

  function currentVoiceFence() {
    return sessionFence(voiceSession) || sessionFence(voiceComposer) || sessionFence(voiceLastSession);
  }

  function voiceComposerOwnsSession() {
    return voiceComposer && sessionFence(voiceComposer)
      && voiceComposer.owner_device
      && voiceComposer.owner_device.device_id === voiceDeviceId
      && voiceComposer.state !== "off" && voiceComposer.state !== "ended"
      && voiceComposer.state !== "unavailable";
  }

  function voiceRecoverableFence() {
    if (voicePendingEndFence || voiceRecoverySuppressed) return null;
    if (voiceSession && voiceSession.state !== "ended" && voiceSession.state !== "ending"
        && voiceSession.state !== "off" && voiceSession.state !== "unavailable") {
      return sessionFence(voiceSession);
    }
    return voiceComposerOwnsSession() ? sessionFence(voiceComposer) : null;
  }

  function voiceEndPath(fence) {
    return "/api/voice/sessions/" + encodeURIComponent(fence.session_id)
      + "?expected_generation=" + encodeURIComponent(fence.generation)
      + "&expected_media_grant_revision=" + encodeURIComponent(fence.media_grant_revision);
  }

  async function bestEffortEndVoice(fence, retryCurrentFence) {
    if (!fence || !voiceBindingIsCurrent()) return false;
    var result = await voiceRequest(voiceEndPath(fence), "DELETE");
    if (result.ok || result.status === 404) return true;
    if (result.status === 409 && retryCurrentFence !== false && voiceBindingIsCurrent()) {
      var state = await voiceRequest("/api/voice/sessions/"
        + encodeURIComponent(fence.session_id) + "/media-grants", "GET");
      if (state.status === 404) return true;
      if (state.ok && mediaGrantStateIsValid(state.body, fence.session_id)) {
        if (state.body.session.state === "ended" || state.body.session.state === "ending") return true;
        return bestEffortEndVoice(sessionFence(state.body.session), false);
      }
      result = state;
    }
    if ((result.status === 0 || result.status === 403) && fence) {
      voicePendingEndFence = fence;
      requestFreshVoiceBinding(voiceBinding && voiceBinding.binding_id);
    }
    return false;
  }

  function handleVoiceMediaLoss(reason) {
    var fence = currentVoiceFence();
    voiceRecoverySuppressed = true;
    clearVoiceRecovery();
    teardownVoiceMedia(true);
    setVoiceFeedback("error", reason, null, true);
    bestEffortEndVoice(fence).then(function () { voiceLastSession = null; });
  }

  function voiceSessionProjectionIsValid(session, sessionId, requireActiveChat) {
    return session && typeof session === "object" && !Array.isArray(session)
      && session.session_id === sessionId
      && isCanonicalUuid4(session.session_id)
      && session.device_id === voiceDeviceId
      && session.owner_connection_generation === connectionGeneration
      && (!requireActiveChat || session.visible_chat_id === activeChatId)
      && isCanonicalUuid4(session.visible_chat_id)
      && Number.isSafeInteger(session.generation) && session.generation > 0
      && Number.isSafeInteger(session.media_grant_revision) && session.media_grant_revision > 0
      && typeof session.chat_context_synced === "boolean"
      && typeof session.foreground_active === "boolean"
      && typeof session.microphone_enabled === "boolean"
      && typeof session.speech_muted === "boolean";
  }

  function sessionGrantIsValid(payload, requireActiveChat) {
    if (!payload || !payload.session || !payload.grant) return false;
    var session = payload.session;
    var grant = payload.grant;
    return voiceSessionProjectionIsValid(
      session, session.session_id, requireActiveChat !== false
    )
      && grant.transport === "livekit" && grant.session_id === session.session_id
      && grant.generation === session.generation
      && grant.media_grant_revision === session.media_grant_revision
      && typeof grant.grant_id === "string" && grant.grant_id
      && typeof grant.url === "string" && /^wss?:\/\//.test(grant.url)
      && typeof grant.join_token === "string" && grant.join_token.length >= 32
      && typeof grant.room_name === "string" && grant.room_name
      && typeof grant.participant_identity === "string" && grant.participant_identity
      && typeof grant.worker_identity === "string" && grant.worker_identity
      && isRfc3339Utc(grant.expires_at) && Date.parse(grant.expires_at) > Date.now();
  }

  function mediaGrantStateIsValid(payload, sessionId) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)
        || Object.keys(payload).sort().join(",") !== "grant_state,session") return false;
    var state = payload.grant_state;
    var session = payload.session;
    if (!state || typeof state !== "object" || Array.isArray(state)
        || Object.keys(state).sort().join(",") !== "expires_at,media_grant_revision,status,transport"
        || state.transport !== "livekit"
        || ["pending_worker", "active", "expired", "unavailable"].indexOf(state.status) === -1
        || !Number.isSafeInteger(state.media_grant_revision)
        || state.media_grant_revision < 1
        || (state.expires_at !== null && !isRfc3339Utc(state.expires_at))) return false;
    if (["grant", "join_token", "participant_identity", "room_name", "ticket", "url"].some(
      function (key) { return Object.prototype.hasOwnProperty.call(session || {}, key); }
    )) return false;
    return voiceSessionProjectionIsValid(session, sessionId, false)
      && session.media_grant_revision === state.media_grant_revision;
  }

  function clearVoiceRecovery() {
    if (!voiceRecovery) return;
    if (voiceRecovery.timer) clearTimeout(voiceRecovery.timer);
    if (voiceRecovery.deadline_timer) clearTimeout(voiceRecovery.deadline_timer);
    voiceRecovery = null;
  }

  function terminalVoiceRecoveryFailure(recovery, reason, message) {
    if (!recovery || voiceRecovery !== recovery) return;
    var fence = sessionFence(voiceSession) || {
      session_id: recovery.session_id,
      generation: recovery.expected_generation,
      media_grant_revision: recovery.expected_media_grant_revision,
    };
    voiceRecoverySuppressed = true;
    clearVoiceRecovery();
    teardownVoiceMedia(true);
    setVoiceFeedback("error", reason || "media_error", message
      || "Voice media could not reconnect. Accepted requests will keep running, and typed chat is still available.", true);
    if (voiceBindingIsCurrent()) {
      bestEffortEndVoice(fence).then(function () { voiceLastSession = null; });
    } else if (fence && reason !== "auth_expired") {
      voicePendingEndFence = fence;
    }
  }

  function scheduleVoiceRecovery(recovery, delayMs) {
    if (!recovery || voiceRecovery !== recovery) return;
    if (Date.now() >= recovery.deadline_at || recovery.attempts >= VOICE_RECOVERY_MAX_ATTEMPTS) {
      terminalVoiceRecoveryFailure(recovery, "network_interrupted");
      return;
    }
    if (recovery.timer) clearTimeout(recovery.timer);
    recovery.timer = setTimeout(function () {
      recovery.timer = null;
      runVoiceRecovery(recovery);
    }, delayMs || 0);
  }

  function retryVoiceRecovery(recovery) {
    if (!recovery || voiceRecovery !== recovery) return;
    recovery.attempts += 1;
    var delays = [250, 750, 1500, 3000];
    scheduleVoiceRecovery(recovery, delays[Math.min(recovery.attempts - 1, delays.length - 1)]);
  }

  function resetVoiceRecoveryForConnection(recovery) {
    recovery.epoch = (recovery.epoch || 0) + 1;
    recovery.connection_generation = connectionGeneration;
    recovery.refresh_id = randomUuid4();
    recovery.refresh_body = null;
    recovery.state = null;
    recovery.grant = null;
    recovery.attempts = 0;
    recovery.deadline_at = Date.now() + VOICE_RECOVERY_DEADLINE_MS;
    if (recovery.deadline_timer) clearTimeout(recovery.deadline_timer);
    recovery.deadline_timer = setTimeout(function () {
      terminalVoiceRecoveryFailure(recovery, "network_interrupted");
    }, VOICE_RECOVERY_DEADLINE_MS);
  }

  function beginVoiceRecovery(reason) {
    var fence = voiceRecoverableFence();
    if (!fence || voicePendingEndFence || voiceActivation
        || document.visibilityState === "hidden" || navigator.onLine === false) return false;
    if (voiceRecovery && voiceRecovery.session_id === fence.session_id) {
      if (voiceRecovery.connection_generation !== connectionGeneration) {
        resetVoiceRecoveryForConnection(voiceRecovery);
      }
      setVoiceFeedback("reconnecting", reason || "network_interrupted", null, true);
      if (voiceBindingIsCurrent()) scheduleVoiceRecovery(voiceRecovery, 0);
      return true;
    }
    clearVoiceRecovery();
    teardownVoiceMedia(false);
    voiceRecovery = {
      session_id: fence.session_id,
      expected_generation: fence.generation,
      expected_media_grant_revision: fence.media_grant_revision,
      reason: reason || "network_interrupted",
      timer: null,
      deadline_timer: null,
      epoch: 0,
      running: false,
      rerun_requested: false,
    };
    resetVoiceRecoveryForConnection(voiceRecovery);
    setVoiceFeedback("reconnecting", voiceRecovery.reason, null, true);
    if (voiceBindingIsCurrent()) scheduleVoiceRecovery(voiceRecovery, 0);
    return true;
  }

  function maybeBeginVoiceRecovery(reason) {
    if (voiceLifecycleSuspended || voiceMediaJoined || voiceMediaJoining
        || voiceGrant || voiceActivation || voicePendingEndFence
        || document.visibilityState === "hidden" || navigator.onLine === false) return false;
    return beginVoiceRecovery(reason || "network_interrupted");
  }

  function completeVoiceRecovery(recovery) {
    if (!recovery || voiceRecovery !== recovery || !voiceMediaJoined) return false;
    clearVoiceRecovery();
    voiceLifecycleSuspended = false;
    startVoiceLeaseHeartbeat();
    resendPendingVoiceSubmissions();
    return true;
  }

  async function runVoiceRecovery(recovery) {
    if (!recovery || voiceRecovery !== recovery) return;
    if (recovery.running) {
      recovery.rerun_requested = true;
      return;
    }
    recovery.running = true;
    recovery.rerun_requested = false;
    var epoch = recovery.epoch;
    try {
      await performVoiceRecovery(recovery, epoch);
    } finally {
      recovery.running = false;
      if (voiceRecovery === recovery && recovery.rerun_requested) {
        recovery.rerun_requested = false;
        scheduleVoiceRecovery(recovery, 0);
      }
    }
  }

  async function performVoiceRecovery(recovery, epoch) {
    if (!recovery || voiceRecovery !== recovery) return;
    if (document.visibilityState === "hidden" || voiceLifecycleSuspended
        || navigator.onLine === false) return;
    if (!voiceBindingIsCurrent() || !ws || ws.readyState !== 1) {
      setVoiceFeedback("reconnecting", "network_interrupted", null, true);
      return;
    }
    if (recovery.connection_generation !== connectionGeneration) {
      resetVoiceRecoveryForConnection(recovery);
    }
    if (!activeChatId || !isCanonicalUuid4(activeChatId)) {
      terminalVoiceRecoveryFailure(recovery, "chat_context_unavailable",
        "Voice ended because there is no authorized active chat. Typed chat is still available.");
      return;
    }

    if (!recovery.state) {
      var stateResult = await voiceRequest("/api/voice/sessions/"
        + encodeURIComponent(recovery.session_id) + "/media-grants", "GET");
      if (voiceRecovery !== recovery || recovery.epoch !== epoch) return;
      if (stateResult.status === 403) {
        requestFreshVoiceBinding(voiceBinding && voiceBinding.binding_id);
        return;
      }
      if (stateResult.status === 401) {
        terminalVoiceRecoveryFailure(recovery, "auth_expired");
        return;
      }
      if (stateResult.status === 404) {
        terminalVoiceRecoveryFailure(recovery, "ended_by_user",
          "Voice session ended while reconnecting. Accepted requests will keep running.");
        return;
      }
      if (!stateResult.ok || !mediaGrantStateIsValid(stateResult.body, recovery.session_id)) {
        retryVoiceRecovery(recovery);
        return;
      }
      if (stateResult.body.grant_state.status === "unavailable"
          || stateResult.body.session.state === "ended"
          || stateResult.body.session.state === "ending") {
        terminalVoiceRecoveryFailure(recovery, "ended_by_user",
          "Voice session ended while reconnecting. Accepted requests will keep running.");
        return;
      }
      recovery.state = stateResult.body;
      recovery.expected_generation = stateResult.body.session.generation;
      recovery.expected_media_grant_revision = stateResult.body.session.media_grant_revision;
      recovery.refresh_body = {
        refresh_id: recovery.refresh_id,
        expected_generation: recovery.expected_generation,
        expected_media_grant_revision: recovery.expected_media_grant_revision,
        device_id: voiceDeviceId,
      };
      voiceSession = stateResult.body.session;
      voiceLastSession = voiceSession;
    }

    if (!voiceCapability().has_audio_output) {
      terminalVoiceRecoveryFailure(recovery, "no_audio_output");
      return;
    }
    if (!voiceStream) {
      try {
        await acquireVoiceMicrophone();
      } catch (error) {
        var permissionReason = voicePermissionReason(error);
        voicePermissionState = permissionReason === "permission_denied" ? "denied" : "restricted";
        terminalVoiceRecoveryFailure(recovery, permissionReason);
        return;
      }
      if (voiceRecovery !== recovery || recovery.epoch !== epoch) return;
    }

    if (!recovery.grant) {
      var refreshResult = await voiceRequest("/api/voice/sessions/"
        + encodeURIComponent(recovery.session_id) + "/media-grants", "POST", recovery.refresh_body);
      if (voiceRecovery !== recovery || recovery.epoch !== epoch) return;
      if (refreshResult.status === 403) {
        requestFreshVoiceBinding(voiceBinding && voiceBinding.binding_id);
        return;
      }
      if (refreshResult.status === 401) {
        terminalVoiceRecoveryFailure(recovery, "auth_expired");
        return;
      }
      if (refreshResult.status === 404) {
        terminalVoiceRecoveryFailure(recovery, "ended_by_user");
        return;
      }
      if (refreshResult.status === 409) {
        recovery.refresh_id = randomUuid4();
        recovery.refresh_body = null;
        recovery.state = null;
        recovery.grant = null;
        retryVoiceRecovery(recovery);
        return;
      }
      if (!refreshResult.ok || !sessionGrantIsValid(refreshResult.body, false)
          || refreshResult.body.refresh_id !== recovery.refresh_id
          || typeof refreshResult.body.replayed !== "boolean"
          || !isRfc3339Utc(refreshResult.body.replay_expires_at)
          || Date.parse(refreshResult.body.replay_expires_at) <= Date.now()
          || refreshResult.body.session.generation !== recovery.expected_generation
          || refreshResult.body.session.media_grant_revision
            !== recovery.expected_media_grant_revision + 1) {
        retryVoiceRecovery(recovery);
        return;
      }
      recovery.grant = refreshResult.body.grant;
      recovery.state = { session: refreshResult.body.session };
      recovery.expected_media_grant_revision = refreshResult.body.session.media_grant_revision;
      voiceSession = refreshResult.body.session;
      voiceLastSession = voiceSession;
      voiceGrant = refreshResult.body.grant;
      voiceExpectedWorker = refreshResult.body.grant.worker_identity;
      voiceLastAnnouncementSequence = 0;
      voiceResultReservation = Object.create(null);
      voiceResultQuantumIndex = Object.create(null);
    } else {
      voiceGrant = recovery.grant;
      voiceExpectedWorker = recovery.grant.worker_identity;
    }

    var updateBody = {
      expected_generation: recovery.expected_generation,
      expected_media_grant_revision: recovery.expected_media_grant_revision,
      foreground_active: true,
      foreground_reason: "foreground",
      microphone_enabled: true,
    };
    if (voiceSession.visible_chat_id !== activeChatId) updateBody.visible_chat_id = activeChatId;
    var updateResult = await voiceRequest("/api/voice/sessions/"
      + encodeURIComponent(recovery.session_id), "PATCH", updateBody);
    if (voiceRecovery !== recovery || recovery.epoch !== epoch) return;
    if (updateResult.status === 403) {
      requestFreshVoiceBinding(voiceBinding && voiceBinding.binding_id);
      return;
    }
    if (updateResult.status === 401) {
      terminalVoiceRecoveryFailure(recovery, "auth_expired");
      return;
    }
    if (updateResult.status === 409) {
      recovery.refresh_id = randomUuid4();
      recovery.refresh_body = null;
      recovery.state = null;
      recovery.grant = null;
      voiceGrant = null;
      retryVoiceRecovery(recovery);
      return;
    }
    if (!updateResult.ok || !voiceSessionProjectionIsValid(
      updateResult.body, recovery.session_id, true
    ) || !updateResult.body.foreground_active || !updateResult.body.microphone_enabled
        || updateResult.body.generation !== recovery.expected_generation
        || updateResult.body.media_grant_revision !== recovery.expected_media_grant_revision) {
      retryVoiceRecovery(recovery);
      return;
    }
    voiceSession = updateResult.body;
    voiceLastSession = voiceSession;
    if (!createVoiceRoom(false)) {
      terminalVoiceRecoveryFailure(recovery, "media_unavailable");
      return;
    }
    if (!voiceSession.chat_context_synced) {
      setVoiceFeedback("connecting", "chat_context_unavailable", "Waiting for the voice chat context…", true);
      return;
    }
    var joined = await joinVoiceMedia();
    if (voiceRecovery !== recovery || recovery.epoch !== epoch) return;
    if (joined) completeVoiceRecovery(recovery);
    else retryVoiceRecovery(recovery);
  }

  async function joinVoiceMedia() {
    if (!voiceRoom || !voiceStream || !voiceSession || !voiceGrant
        || voiceMediaJoined || voiceMediaJoining) return false;
    if (!voiceSession.chat_context_synced
        || voiceSession.applied_visible_chat_id !== voiceSession.visible_chat_id
        || voiceSession.visible_chat_id !== activeChatId
        || voiceSession.owner_connection_generation !== connectionGeneration) {
      setVoiceFeedback("connecting", "chat_context_unavailable", "Waiting for the voice chat context…", true);
      return false;
    }
    var room = voiceRoom;
    var stream = voiceStream;
    var grant = voiceGrant;
    var joinEpoch = voiceStateEpoch;
    var activation = voiceActivation;
    function joinIsCurrent() {
      return voiceStateEpoch === joinEpoch && voiceRoom === room
        && voiceStream === stream && voiceGrant === grant && voiceSession
        && voiceSession.session_id === grant.session_id
        && voiceSession.generation === grant.generation
        && voiceSession.media_grant_revision === grant.media_grant_revision
        && voiceSession.owner_connection_generation === connectionGeneration;
    }
    voiceMediaJoining = true;
    try {
      await room.connect(grant.url, grant.join_token, { autoSubscribe: false });
      if (!joinIsCurrent()) {
        try { room.disconnect(); } catch (e) {}
        return false;
      }
      reconcileVoiceRemotePublications(room);
      var track = stream.getAudioTracks()[0];
      track.enabled = true;
      var source = window.LivekitClient.Track && window.LivekitClient.Track.Source
        ? window.LivekitClient.Track.Source.Microphone : "microphone";
      await room.localParticipant.publishTrack(track, {
        source: source,
        name: "astraldeep-microphone",
      });
      if (!joinIsCurrent()) {
        try { room.disconnect(); } catch (e) {}
        return false;
      }
      voiceMediaJoined = true;
      voiceMediaJoining = false;
      if (voiceActivation === activation) {
        if (activation && activation.timeout) clearTimeout(activation.timeout);
        voiceActivation = null;
      }
      setVoiceFeedback("connecting", "ready", "Connected. Waiting for the greeting…", true);
      return true;
    } catch (e) {
      if (!joinIsCurrent()) {
        try { room.disconnect(); } catch (_error) {}
        return false;
      }
      voiceMediaJoining = false;
      teardownVoiceMedia(!voiceRecovery);
      setVoiceFeedback(voiceRecovery ? "reconnecting" : "error",
        voiceRecovery ? "network_interrupted" : "media_error", null, true);
      return false;
    }
  }

  function sendCorrelatedVoiceNewChat(activation) {
    activation.chat_submission_id = randomUuid4();
    activation.chat_request_generation = randomUuid4();
    activation.awaiting_chat = true;
    var payload = {
      schema_version: "1",
      connection_generation: connectionGeneration,
      submission_id: activation.chat_submission_id,
      request_generation: activation.chat_request_generation,
    };
    send({
      type: "ui_event",
      action: "new_chat",
      schema_version: "1",
      connection_generation: connectionGeneration,
      submission_id: activation.chat_submission_id,
      request_generation: activation.chat_request_generation,
      payload: payload,
    });
  }

  function correlatedVoiceChatCreated(frame) {
    var pending = voiceActivation;
    if (!pending || !pending.awaiting_chat) return false;
    if (!frame || frame.type !== "chat_created" || frame.schema_version !== "1") return true;
    var payload = frame.payload;
    if (!payload || payload.schema_version !== "1" || payload.from_message !== false
        || frame.connection_generation !== pending.connection_generation
        || frame.connection_generation !== connectionGeneration
        || frame.submission_id !== pending.chat_submission_id
        || frame.request_generation !== pending.chat_request_generation
        || payload.connection_generation !== frame.connection_generation
        || payload.submission_id !== frame.submission_id
        || payload.request_generation !== frame.request_generation
        || !isCanonicalUuid4(payload.chat_id)
        || activeChatId !== pending.initial_chat_id) return true;
    pending.awaiting_chat = false;
    pending.awaiting_hydration = true;
    loadActiveChat(payload.chat_id);
    pending.chat_id = payload.chat_id;
    pending.hydration_generation = requestState && requestState.generation;
    return true;
  }

  function continueVoiceAfterHydration(frame) {
    var pending = voiceActivation;
    if (!pending || !pending.awaiting_hydration || frame.chat_id !== pending.chat_id
        || frame.request_generation !== pending.hydration_generation
        || frame.connection_generation !== pending.connection_generation) return;
    pending.awaiting_hydration = false;
    continueVoiceActivation(pending);
  }

  function voiceActivationRequest(pending, capability) {
    var body = {
      device_id: voiceDeviceId,
      device_kind: "web",
      visible_chat_id: activeChatId,
      activation_id: pending.activation_id,
      capability: capability,
      foreground_active: true,
    };
    if (pending.kind === "takeover") {
      body.expected_generation = pending.takeover.generation;
      body.expected_media_grant_revision = pending.takeover.media_grant_revision;
      return {
        path: "/api/voice/sessions/" + encodeURIComponent(pending.takeover.session_id) + "/takeover",
        body: body,
      };
    }
    return { path: "/api/voice/sessions", body: body };
  }

  async function continueVoiceActivation(pending) {
    if (voiceActivation !== pending || pending.connection_generation !== connectionGeneration
        || !activeChatId || (pending.chat_id && pending.chat_id !== activeChatId)) return;
    if (!voiceBindingIsCurrent()) {
      teardownVoiceMedia(false);
      setVoiceFeedback("error", "auth_expired", "Voice controls are reconnecting. Try again in a moment.", true);
      return;
    }
    try {
      await acquireVoiceMicrophone();
    } catch (error) {
      var permissionReason = voicePermissionReason(error);
      voicePermissionState = permissionReason === "permission_denied" ? "denied" : "restricted";
      teardownVoiceMedia(false);
      setVoiceFeedback("error", permissionReason, null, true);
      return;
    }
    if (voiceActivation !== pending || pending.connection_generation !== connectionGeneration
        || activeChatId !== (pending.chat_id || pending.initial_chat_id)) {
      teardownVoiceMedia(false);
      return;
    }
    var request = voiceActivationRequest(pending, voiceCapability());
    var result = await voiceRequest(request.path, "POST", request.body);
    if (result.status === 0 && voiceActivation === pending
        && pending.connection_generation === connectionGeneration) {
      result = await voiceRequest(request.path, "POST", request.body);
    }
    if (voiceActivation !== pending) return;
    if (pending.connection_generation !== connectionGeneration
        || activeChatId !== (pending.chat_id || pending.initial_chat_id)) {
      if (result.ok && result.body && result.body.session
          && result.body.session.device_id === voiceDeviceId
          && result.body.session.owner_connection_generation === connectionGeneration) {
        bestEffortEndVoice(sessionFence(result.body.session));
      }
      teardownVoiceMedia(false);
      setVoiceFeedback("error", "chat_context_unavailable", "Voice did not start because the active chat changed.", true);
      return;
    }
    if (result.status === 409 && result.body && result.body.code === "voice_takeover_required"
        && result.body.owner && isCanonicalUuid4(result.body.owner.session_id)) {
      voiceTakeover = {
        session_id: result.body.owner.session_id,
        generation: result.body.owner.generation,
        media_grant_revision: result.body.owner.media_grant_revision,
      };
      teardownVoiceMedia(false);
      setVoiceFeedback("off", "takeover_required", result.body.message, true);
      return;
    }
    if (!result.ok || !sessionGrantIsValid(result.body)) {
      var reason = result.reason || result.body && result.body.code || "voice_unavailable";
      teardownVoiceMedia(false);
      setVoiceFeedback("error", reason, result.body && result.body.message, true);
      return;
    }
    voiceSession = result.body.session;
    voiceLastSession = result.body.session;
    voiceGrant = result.body.grant;
    voiceExpectedWorker = result.body.grant.worker_identity;
    voiceLastAnnouncementSequence = 0;
    voiceResultReservation = Object.create(null);
    voiceResultQuantumIndex = Object.create(null);
    voiceTakeover = null;
    voiceLifecycleSuspended = false;
    startVoiceLeaseHeartbeat();
    if (voiceSession.chat_context_synced) await joinVoiceMedia();
    else setVoiceFeedback("connecting", "chat_context_unavailable", "Waiting for the voice chat context…", true);
  }

  function beginVoiceActivation(kind) {
    if (voiceActivation) return;
    if (!voiceBindingIsCurrent()) {
      setVoiceFeedback("error", "auth_expired", "Voice controls are reconnecting. Try again in a moment.", true);
      return;
    }
    voiceRecoverySuppressed = false;
    var room = createVoiceRoomFromGesture();
    if (!room) return;
    var takeover = kind === "takeover"
      ? voiceTakeover || sessionFence(voiceComposer) : null;
    if (kind === "takeover" && (!takeover || !isCanonicalUuid4(takeover.session_id))) {
      teardownVoiceMedia(false);
      setVoiceFeedback("error", "stale_generation", "The other voice session is no longer available.", true);
      return;
    }
    voiceActivation = {
      kind: kind,
      activation_id: randomUuid4(),
      connection_generation: connectionGeneration,
      initial_chat_id: activeChatId,
      chat_id: activeChatId,
      takeover: takeover,
    };
    var pending = voiceActivation;
    pending.timeout = setTimeout(function () {
      if (voiceActivation !== pending) return;
      teardownVoiceMedia(false);
      setVoiceFeedback("error", "network_interrupted", "Voice activation timed out. You can retry or keep typing.", true);
    }, 30000);
    setVoiceFeedback("connecting", "ready", null, true);
    if (!activeChatId) sendCorrelatedVoiceNewChat(voiceActivation);
    else continueVoiceActivation(voiceActivation);
  }

  function patchVoiceSession(fields, optimistic) {
    var fence = currentVoiceFence();
    var stateEpoch = voiceStateEpoch;
    if (!fence || !voiceBindingIsCurrent()) {
      setVoiceFeedback("error", "stale_generation", null, true);
      return Promise.resolve(false);
    }
    if (optimistic) optimistic();
    var body = Object.assign({
      expected_generation: fence.generation,
      expected_media_grant_revision: fence.media_grant_revision,
    }, fields);
    return voiceRequest("/api/voice/sessions/" + encodeURIComponent(fence.session_id), "PATCH", body)
      .then(function (result) {
        if (!result.ok) {
          setVoiceFeedback("error", result.reason || result.body && result.body.code || "stale_generation",
            result.body && result.body.message, true);
          applyVoiceCaptureState();
          return false;
        }
        if (stateEpoch === voiceStateEpoch && result.body
            && result.body.session_id === fence.session_id) {
          voiceSession = result.body;
          voiceLastSession = result.body;
          if (voiceSession.foreground_active) startVoiceLeaseHeartbeat();
          else stopVoiceLeaseHeartbeat();
          applyVoiceCaptureState();
        }
        return true;
      });
  }

  function applyVoiceCaptureState() {
    if (!voiceStream) return;
    var enabled = !!(voiceMediaJoined && voiceSession && voiceSession.foreground_active
      && voiceSession.microphone_enabled && voiceSession.chat_context_synced
      && voiceSession.visible_chat_id === activeChatId
      && ["off", "unavailable", "suspended", "reconnecting", "error", "ending", "ended"]
        .indexOf(voiceSession.state) === -1);
    try {
      voiceStream.getAudioTracks().forEach(function (track) { track.enabled = enabled; });
    } catch (e) {}
  }

  function pauseVoiceCaptureForChatTransition() {
    if (!voiceStream) return;
    try {
      voiceStream.getAudioTracks().forEach(function (track) { track.enabled = false; });
    } catch (e) {}
  }

  function syncVoiceVisibleChat(chatId) {
    if (!isCanonicalUuid4(chatId) || !voiceSession
        || voiceSession.state === "ending" || voiceSession.state === "ended") return;
    voiceVisibleChatTarget = chatId;
    if (voiceSession.visible_chat_id === chatId) {
      if (voiceSession.chat_context_synced) voiceVisibleChatTarget = null;
      applyVoiceCaptureState();
      return;
    }
    pauseVoiceCaptureForChatTransition();
    setVoiceFeedback("connecting", "chat_context_unavailable", "Updating the voice chat context…", true);
    if (voiceVisibleChatSync || !voiceBindingIsCurrent()) return;
    var sync = { target: chatId, succeeded: false };
    voiceVisibleChatSync = sync;
    patchVoiceSession({ visible_chat_id: chatId }).then(function (ok) {
      sync.succeeded = ok;
    }).finally(function () {
      if (voiceVisibleChatSync !== sync) return;
      voiceVisibleChatSync = null;
      var latest = voiceVisibleChatTarget;
      if (sync.succeeded && latest && voiceSession
          && voiceSession.visible_chat_id !== latest) {
        syncVoiceVisibleChat(latest);
        return;
      }
      voiceVisibleChatTarget = null;
      applyVoiceCaptureState();
    });
  }

  function stopVoiceSessionExplicitly() {
    var fence = currentVoiceFence();
    voiceRecoverySuppressed = true;
    clearVoiceRecovery();
    clearVoiceRequestTerminal();
    teardownVoiceMedia(true);
    setVoiceFeedback("ended", "ended_by_user", null, true);
    if (voiceBindingIsCurrent()) {
      bestEffortEndVoice(fence).then(function () { voiceLastSession = null; });
    } else if (fence) {
      voicePendingEndFence = fence;
    }
  }

  function stopVoiceSpeech() {
    var fence = currentVoiceFence();
    if (!fence || !voiceBindingIsCurrent()) return;
    // Stop is a realtime local action first.  Purge the active/queued Web
    // Audio graph synchronously before starting the generation-fenced server
    // request so a slow or failed network path cannot leave stale speech
    // audible.  The server request below still owns the authoritative speech
    // epoch and existing error/state semantics.
    clearVoiceAudioElements();
    voiceRequest("/api/voice/sessions/" + encodeURIComponent(fence.session_id) + "/speech/stop", "POST", {
      expected_generation: fence.generation,
      expected_media_grant_revision: fence.media_grant_revision,
    }).then(function (result) {
      if (!result.ok) setVoiceFeedback("error", result.body && result.body.code || "speech_error",
        result.body && result.body.message, true);
    });
  }

  function consentSensitiveVoiceResult() {
    var fence = currentVoiceFence();
    if (!fence || !voiceCurrentResultId || !voiceBindingIsCurrent()) {
      setVoiceFeedback("error", "stale_generation", "That spoken result is no longer available.", true);
      return;
    }
    voiceRequest("/api/voice/sessions/" + encodeURIComponent(fence.session_id)
      + "/results/" + encodeURIComponent(voiceCurrentResultId) + "/read-consent", "POST", {
      expected_generation: fence.generation,
      expected_media_grant_revision: fence.media_grant_revision,
      turn_id: voiceComposer && voiceComposer.foreground_turn_id,
      consent_method: "tap",
    }).then(function (result) {
      if (!result.ok) setVoiceFeedback("error", result.body && result.body.code || "speech_error",
        result.body && result.body.message, true);
    });
  }

  function onVoiceControlClick(control) {
    if (!control || !control.enabled) return;
    switch (control.action) {
      case "voice_session_start": beginVoiceActivation("start"); break;
      case "voice_session_takeover": beginVoiceActivation("takeover"); break;
      case "voice_session_end": stopVoiceSessionExplicitly(); break;
      case "voice_microphone_set": {
        var enable = !control.pressed;
        patchVoiceSession({ microphone_enabled: enable }, function () {
          if (voiceStream) voiceStream.getAudioTracks().forEach(function (track) { track.enabled = enable; });
        });
        break;
      }
      case "voice_speech_stop": stopVoiceSpeech(); break;
      case "voice_speech_mute_set": patchVoiceSession({ speech_muted: !control.pressed }); break;
      case "voice_visible_chat_update":
        if (activeChatId) syncVoiceVisibleChat(activeChatId);
        break;
      case "voice_sensitive_recap_request": consentSensitiveVoiceResult(); break;
      default: break;
    }
  }

  function consumeVoiceSessionState(frame) {
    if (!frame || frame.type !== "voice_session_state" || frame.schema_version !== "1"
        || frame.connection_generation !== connectionGeneration || !isCanonicalUuid4(frame.session_id)
        || !VOICE_STATES[frame.state] || !Number.isSafeInteger(frame.generation)
        || !Number.isSafeInteger(frame.media_grant_revision)
        || typeof frame.foreground_active !== "boolean"
        || typeof frame.microphone_enabled !== "boolean"
        || (!frame.foreground_active && frame.microphone_enabled)
        || (!frame.foreground_active
          && ["suspended", "reconnecting", "error", "ended"].indexOf(frame.state) === -1)
        || (frame.foreground_active
          && ["off", "unavailable", "suspended", "ended"].indexOf(frame.state) !== -1)) return false;
    var fence = currentVoiceFence();
    if (fence && (frame.session_id !== fence.session_id || frame.generation !== fence.generation
        || frame.media_grant_revision !== fence.media_grant_revision)) return false;
    if (voiceSession) {
      voiceSession = Object.assign({}, voiceSession, {
        state: frame.state,
        visible_chat_id: frame.visible_chat_id,
        applied_visible_chat_id: frame.chat_context_synced
          ? frame.visible_chat_id : voiceSession.applied_visible_chat_id,
        chat_context_revision: frame.chat_context_revision,
        applied_chat_context_revision: frame.applied_chat_context_revision,
        chat_context_synced: frame.chat_context_synced,
        speech_muted: frame.speech_muted,
        microphone_enabled: frame.microphone_enabled,
        foreground_active: frame.foreground_active,
      });
      voiceLastSession = voiceSession;
    }
    if (frame.state === "ended") {
      voiceRecoverySuppressed = true;
      teardownVoiceMedia(true);
      voiceLastSession = null;
      voicePendingEndFence = null;
      voiceLifecycleSuspended = false;
      if (frame.reason === "ended_by_user") clearVoiceRequestTerminal();
      setVoiceFeedback("ended", frame.reason, frame.message, true);
      return true;
    }
    if (frame.state === "suspended" || frame.state === "reconnecting") {
      if (!voiceRecovery) teardownVoiceMedia(false);
      setVoiceFeedback(frame.state, frame.reason, frame.message, true);
      if (document.visibilityState !== "hidden" && !voiceLifecycleSuspended) {
        maybeBeginVoiceRecovery(frame.reason || "network_interrupted");
      }
      return true;
    }
    setVoiceFeedback(frame.state, frame.reason, frame.message, true);
    applyVoiceCaptureState();
    if (frame.chat_context_synced && voiceSession && voiceGrant && voiceRoom && voiceStream
        && !voiceMediaJoined && !voiceMediaJoining) {
      var recovery = voiceRecovery;
      joinVoiceMedia().then(function (joined) {
        if (joined && recovery) completeVoiceRecovery(recovery);
      });
    }
    return true;
  }

  function consumeVoiceTurnState(frame) {
    var requiredKeys = [
      "type", "schema_version", "session_id", "connection_generation", "generation",
      "media_grant_revision", "turn_id", "client_turn_id", "submission_id",
      "request_generation", "chat_id", "chat_context_revision", "detected_language",
      "spoken_output_policy", "output_reason", "state", "foreground",
      "sensitive_result_pending", "sequence", "occurred_at",
    ];
    var suppliedKeys = requiredKeys.slice();
    ["result_id", "message", "speech_outcome"].forEach(function (key) {
      if (frame && Object.prototype.hasOwnProperty.call(frame, key)) suppliedKeys.push(key);
    });
    if (!frame || frame.type !== "voice_turn_state" || frame.schema_version !== "1"
        || !exactKeys(frame, suppliedKeys)
        || frame.connection_generation !== connectionGeneration || !isCanonicalUuid4(frame.session_id)) return false;
    var fence = currentVoiceFence();
    if (!fence || frame.session_id !== fence.session_id || frame.generation !== fence.generation
        || frame.media_grant_revision !== fence.media_grant_revision
        || !Object.prototype.hasOwnProperty.call(VOICE_TURN_STATES, frame.state)
        || (Object.prototype.hasOwnProperty.call(frame, "speech_outcome")
          && (frame.state !== "succeeded"
            || !Object.prototype.hasOwnProperty.call(
              VOICE_SPEECH_OUTCOMES, frame.speech_outcome)))
        || !validVoiceTurnMessage(frame)
        || !isCanonicalUuid4(frame.turn_id)
        || !isRfc3339Utc(frame.occurred_at)
        || !Number.isSafeInteger(frame.sequence) || frame.sequence < 0) return false;
    var language = frame.detected_language;
    if (language === null) {
      if (["recognizing", "abandoned"].indexOf(frame.state) === -1
          || frame.spoken_output_policy !== "pending"
          || frame.output_reason !== "language_pending") return false;
    } else {
      if (typeof language !== "string"
          || !/^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(language)
          || frame.state === "recognizing") return false;
      var english = /^en(?:-|$)/.test(language);
      if (english ? (frame.spoken_output_policy !== "full_recap"
          || frame.output_reason !== "ready")
        : (frame.spoken_output_policy !== "english_lifecycle_only"
          || frame.output_reason !== "output_language_unsupported")) return false;
    }
    if (typeof frame.result_id === "string" && frame.result_id) voiceCurrentResultId = frame.result_id;
    if (frame.state === "succeeded" && frame.speech_outcome === "failed") {
      showVoiceRequestTerminal({
        state: "speech_error",
        turn_id: frame.turn_id,
        occurred_at: frame.occurred_at,
        message: "The result audio could not be delivered.",
      }, "The text result is still available in the conversation. Typed chat remains available.");
    } else if (VOICE_REQUEST_TERMINAL_TITLES[frame.state]) {
      showVoiceRequestTerminal(frame);
    } else {
      clearVoiceRequestTerminalForNewerTurn(frame);
    }
    return true;
  }

  function removePendingVoiceSubmission(submissionId) {
    var pending = voicePendingSubmissions[submissionId];
    if (!pending) return null;
    if (pending.timer) clearTimeout(pending.timer);
    delete voicePendingSubmissions[submissionId];
    voicePendingSubmissionBytes = Math.max(
      0, voicePendingSubmissionBytes - pending.byte_length
    );
    finishOperationSubmission(pending.request_generation);
    return pending;
  }

  function clearPendingVoiceSubmissions() {
    Object.keys(voicePendingSubmissions).forEach(removePendingVoiceSubmission);
    voicePendingSubmissions = Object.create(null);
    voicePendingSubmissionBytes = 0;
  }

  function expirePendingVoiceSubmission(pending) {
    if (!pending || voicePendingSubmissions[pending.submission_id] !== pending) return;
    removePendingVoiceSubmission(pending.submission_id);
    if (voiceTranscriptEl) {
      voiceTranscriptEl.textContent = "That spoken request expired before it was accepted. Please say it again.";
      voiceTranscriptEl.setAttribute("data-final", "true");
    }
    setVoiceFeedback("error", "proof_expired", "That spoken request was not accepted. Please say it again.", true);
  }

  function voiceChatMessageFrame(pending) {
    var origin = {
      schema_version: "1",
      session_id: pending.session_id,
      generation: pending.generation,
      media_grant_revision: pending.media_grant_revision,
      turn_id: pending.turn_id,
      client_turn_id: pending.client_turn_id,
      chat_context_revision: pending.chat_context_revision,
      source_participant_identity: pending.source_participant_identity,
      detected_language: pending.detected_language,
      text_digest_sha256: pending.text_digest_sha256,
      transcript_proof: pending.transcript_proof,
      proof_expires_at: pending.proof_expires_at,
    };
    return {
      type: "ui_event",
      action: "chat_message",
      session_id: pending.chat_id,
      connection_generation: connectionGeneration,
      submission_id: pending.submission_id,
      request_generation: pending.request_generation,
      payload: {
        message: pending.text,
        chat_id: pending.chat_id,
        connection_generation: connectionGeneration,
        submission_id: pending.submission_id,
        request_generation: pending.request_generation,
        snapshot_purpose: "commit",
        voice_origin: origin,
      },
    };
  }

  function sendPendingVoiceSubmission(pending) {
    if (!pending || voicePendingSubmissions[pending.submission_id] !== pending) return;
    if (Date.parse(pending.proof_expires_at) <= Date.now()) {
      expirePendingVoiceSubmission(pending);
      return;
    }
    if (pending.timer) clearTimeout(pending.timer);
    pending.timer = setTimeout(function () {
      sendPendingVoiceSubmission(pending);
    }, Math.min(
      VOICE_SUBMISSION_RETRY_MS,
      Math.max(1, Date.parse(pending.proof_expires_at) - Date.now())
    ));
    if (!voiceBindingIsCurrent() || !ws || ws.readyState !== 1) return;
    if (!operationSubmissionByGeneration[pending.request_generation]) {
      var local = {
        submission_id: pending.submission_id,
        request_generation: pending.request_generation,
        action: "chat_message",
        chat_id: pending.chat_id,
        state: "submitting",
        label: "Submitting spoken request…",
        status_order: ++operationSubmissionOrdinal,
      };
      operationSubmissionByGeneration[pending.request_generation] = local;
      operationSubmissionById[pending.submission_id] = local;
      if (pending.chat_id === activeChatId) {
        setStatus(local.label, true, "operation-submission:" + pending.request_generation);
      }
    }
    send(voiceChatMessageFrame(pending));
  }

  function resendPendingVoiceSubmissions() {
    Object.keys(voicePendingSubmissions).forEach(function (submissionId) {
      sendPendingVoiceSubmission(voicePendingSubmissions[submissionId]);
    });
  }

  function retainFinalVoiceSubmission(frame) {
    if (voicePendingSubmissions[frame.submission_id]) {
      sendPendingVoiceSubmission(voicePendingSubmissions[frame.submission_id]);
      return true;
    }
    var copy = {
      session_id: frame.session_id,
      generation: frame.generation,
      media_grant_revision: frame.media_grant_revision,
      turn_id: frame.turn_id,
      client_turn_id: frame.client_turn_id,
      submission_id: frame.submission_id,
      request_generation: frame.request_generation,
      chat_id: frame.chat_id,
      chat_context_revision: frame.chat_context_revision,
      source_participant_identity: frame.source_participant_identity,
      detected_language: frame.detected_language,
      text_digest_sha256: frame.text_digest_sha256,
      transcript_proof: frame.transcript_proof,
      proof_expires_at: frame.proof_expires_at,
      text: frame.text,
      timer: null,
    };
    copy.byte_length = new TextEncoder().encode(JSON.stringify(copy)).length;
    if (Object.keys(voicePendingSubmissions).length >= VOICE_MAX_PENDING_SUBMISSIONS
        || copy.byte_length > VOICE_MAX_PENDING_BYTES
        || voicePendingSubmissionBytes + copy.byte_length > VOICE_MAX_PENDING_BYTES) {
      setVoiceFeedback("error", "capacity_exhausted", "Too many spoken requests are awaiting acceptance. Please retry this one.", true);
      return false;
    }
    voicePendingSubmissions[copy.submission_id] = copy;
    voicePendingSubmissionBytes += copy.byte_length;
    sendPendingVoiceSubmission(copy);
    return true;
  }

  function consumeVoiceMessageAcknowledged(frame) {
    if (!frame || frame.type !== "user_message_acked" || frame.schema_version !== "1"
        || frame.connection_generation !== connectionGeneration
        || !isCanonicalUuid4(frame.voice_turn_id) || !isCanonicalUuid4(frame.chat_id)
        || !isCanonicalUuid4(frame.submission_id) || !isCanonicalUuid4(frame.request_generation)
        || !Number.isSafeInteger(frame.message_id) || frame.message_id < 1) return false;
    var pending = voicePendingSubmissions[frame.submission_id];
    if (!pending || pending.turn_id !== frame.voice_turn_id
        || pending.chat_id !== frame.chat_id
        || pending.request_generation !== frame.request_generation) return false;
    removePendingVoiceSubmission(frame.submission_id);
    if (voiceTranscriptEl) voiceTranscriptEl.setAttribute("data-accepted", "true");
    return true;
  }

  function consumeVoiceSubmissionRejected(frame) {
    if (!frame || frame.type !== "voice_submission_rejected" || frame.schema_version !== "1"
        || frame.connection_generation !== connectionGeneration
        || !isCanonicalUuid4(frame.session_id) || !isCanonicalUuid4(frame.turn_id)
        || !isCanonicalUuid4(frame.client_turn_id) || !isCanonicalUuid4(frame.submission_id)
        || !isCanonicalUuid4(frame.request_generation) || !isCanonicalUuid4(frame.chat_id)
        || !Number.isSafeInteger(frame.generation) || frame.generation < 1
        || !Number.isSafeInteger(frame.media_grant_revision) || frame.media_grant_revision < 1
        || !VOICE_SUBMISSION_REJECTION_REASONS[frame.reason]
        || ["explicit_user_retry", "none"].indexOf(frame.retry_policy) === -1
        || (Object.prototype.hasOwnProperty.call(frame, "message")
          && (typeof frame.message !== "string" || Array.from(frame.message).length > 240))
        || !isRfc3339Utc(frame.occurred_at)) return false;
    var pending = voicePendingSubmissions[frame.submission_id];
    if (!pending || pending.session_id !== frame.session_id
        || pending.generation !== frame.generation
        || pending.media_grant_revision !== frame.media_grant_revision
        || pending.turn_id !== frame.turn_id || pending.client_turn_id !== frame.client_turn_id
        || pending.request_generation !== frame.request_generation
        || pending.chat_id !== frame.chat_id) return false;
    removePendingVoiceSubmission(frame.submission_id);
    var serverMessage = typeof frame.message === "string" && frame.message
      ? frame.message : "That spoken request was not accepted.";
    var guidance = frame.retry_policy === "explicit_user_retry"
      ? "Please say it again, or use typed chat."
      : "This request will not retry automatically. Use typed chat to continue.";
    var feedbackMessage = serverMessage + " " + guidance;
    if (voiceTranscriptEl) {
      voiceTranscriptEl.textContent = feedbackMessage;
      voiceTranscriptEl.setAttribute("data-final", "true");
      voiceTranscriptEl.setAttribute("data-rejected", frame.reason);
    }
    showVoiceRequestTerminal({
      state: "refused",
      message: serverMessage,
      turn_id: frame.turn_id,
      occurred_at: frame.occurred_at,
    }, guidance);
    setVoiceFeedback("error", frame.reason, feedbackMessage, true);
    return true;
  }

  function consumeVoiceTranscript(frame, participantIdentity) {
    if (!frame || frame.type !== "voice_transcript" || frame.schema_version !== "1"
        || !voiceSession || frame.session_id !== voiceSession.session_id
        || frame.generation !== voiceSession.generation
        || !Number.isSafeInteger(frame.media_grant_revision)
        || frame.media_grant_revision < 1
        || frame.media_grant_revision > voiceSession.media_grant_revision
        || frame.source_participant_identity !== voiceExpectedWorker
        || participantIdentity && participantIdentity !== voiceExpectedWorker
        || !isCanonicalUuid4(frame.turn_id) || !isCanonicalUuid4(frame.client_turn_id)
        || !isCanonicalUuid4(frame.submission_id) || !isCanonicalUuid4(frame.request_generation)
        || !isCanonicalUuid4(frame.chat_id)
        || !Number.isSafeInteger(frame.chat_context_revision) || frame.chat_context_revision < 1
        || !Number.isSafeInteger(frame.sequence)
        || frame.sequence < 0 || typeof frame.final !== "boolean"
        || typeof frame.text !== "string" || frame.text.length > 8000) return false;
    if (frame.final && (!frame.text.trim()
        || typeof frame.detected_language !== "string"
        || !/^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(frame.detected_language)
        || typeof frame.text_digest_sha256 !== "string"
        || !/^[0-9a-f]{64}$/.test(frame.text_digest_sha256)
        || typeof frame.transcript_proof !== "string"
        || !/^[0-9a-f]{64}$/.test(frame.transcript_proof)
        || !isRfc3339Utc(frame.proof_expires_at)
        || Date.parse(frame.proof_expires_at) <= Date.now())) return false;
    var previous = voiceTranscriptSequence[frame.turn_id];
    if (previous != null && frame.sequence <= previous) return false;
    voiceTranscriptSequence[frame.turn_id] = frame.sequence;
    if (voiceTranscriptEl) {
      voiceTranscriptEl.textContent = (frame.final ? "Heard: " : "Hearing: ") + frame.text;
      voiceTranscriptEl.setAttribute("data-final", frame.final ? "true" : "false");
      voiceTranscriptEl.removeAttribute("data-accepted");
      voiceTranscriptEl.removeAttribute("data-rejected");
    }
    if (voiceFeedbackEl) voiceFeedbackEl.hidden = false;
    if (frame.final) return retainFinalVoiceSubmission(frame);
    return true;
  }

  function validVoiceAnnouncement(frame, participantIdentity) {
    var singleKinds = {
      greeting: true, acknowledgement: true, progress: true, waiting: true,
      sensitive_notice: true, failure: true, refusal: true, cancellation: true,
    };
    if (!frame || frame.type !== "voice_announcement_media" || frame.schema_version !== "1"
        || !voiceSession || frame.session_id !== voiceSession.session_id
        || frame.generation !== voiceSession.generation
        || frame.media_grant_revision !== voiceSession.media_grant_revision
        || frame.transport !== "livekit" || frame.worker_identity !== voiceExpectedWorker
        || participantIdentity !== voiceExpectedWorker || !isCanonicalUuid4(frame.announcement_id)
        || !Number.isSafeInteger(frame.announcement_sequence) || frame.announcement_sequence < 1
        || frame.announcement_sequence <= voiceLastAnnouncementSequence
        || typeof frame.track_sid !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(frame.track_sid)
        || typeof frame.track_name !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(frame.track_name)
        || !Number.isSafeInteger(frame.quantum_index) || frame.quantum_index < 0
        || frame.quantum_index > 31
        || !Number.isSafeInteger(frame.duration_samples) || frame.duration_samples < 1
        || frame.duration_samples > 96000 || frame.sample_rate_hz !== 24000) return false;
    if (frame.kind === "greeting" ? frame.turn_id !== null : !isCanonicalUuid4(frame.turn_id)) return false;
    if (frame.quantum_role === "single") {
      return singleKinds[frame.kind] === true && frame.quantum_index === 0
        && frame.result_reserved_samples_after === undefined;
    }
    if (frame.kind !== "result" || !Number.isSafeInteger(frame.result_reserved_samples_after)
        || frame.result_reserved_samples_after < 1 || frame.result_reserved_samples_after > 720000) return false;
    if (frame.quantum_role === "result_opening") {
      return frame.quantum_index === 0 && frame.duration_samples <= 36000
        && frame.result_reserved_samples_after >= frame.duration_samples
        && frame.result_reserved_samples_after <= 36000;
    }
    return frame.quantum_role === "result_continuation" && frame.quantum_index >= 1;
  }

  function consumeVoiceAnnouncement(frame, participantIdentity) {
    if (!validVoiceAnnouncement(frame, participantIdentity)) return false;
    if (voiceAnnouncementByTrack[frame.track_sid] || voiceActivePlayout[frame.announcement_id]) return false;
    if (Object.keys(voiceAnnouncementByTrack).length >= 8) return false;
    if (frame.kind === "result") {
      var priorReservation = voiceResultReservation[frame.turn_id] || 0;
      var priorQuantum = voiceResultQuantumIndex[frame.turn_id];
      if (frame.quantum_role === "result_opening") {
        if (priorReservation !== 0 || priorQuantum != null) return false;
      } else if (priorReservation < 1 || priorQuantum == null
          || frame.quantum_index !== priorQuantum + 1
          || frame.result_reserved_samples_after < priorReservation + frame.duration_samples) return false;
      voiceResultReservation[frame.turn_id] = frame.result_reserved_samples_after;
      voiceResultQuantumIndex[frame.turn_id] = frame.quantum_index;
    }
    voiceLastAnnouncementSequence = frame.announcement_sequence;
    voiceAnnouncementByTrack[frame.track_sid] = frame;
    voiceMediaTimers.push(setTimeout(function () { expireVoiceAnnouncement(frame.track_sid); }, 1000));
    queueVoiceTrack(frame.track_sid);
    return true;
  }

  function decodeVoicePacket(payload, maximum) {
    var text;
    try {
      if (typeof payload === "string") text = payload;
      else if (payload instanceof Uint8Array) text = new TextDecoder().decode(payload);
      else return null;
      if (new TextEncoder().encode(text).length > maximum) return null;
      var value = JSON.parse(text);
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    } catch (e) { return null; }
  }

  function consumeVoiceRoomData(payload, participant, topic) {
    if (!participant || participant.identity !== voiceExpectedWorker) return false;
    if (topic === "astraldeep.voice.transcript.v1") {
      var transcript = decodeVoicePacket(payload, 12 * 1024);
      return consumeVoiceTranscript(transcript, participant.identity);
    }
    if (topic === "astraldeep.voice.announcement.v1") {
      var announcement = decodeVoicePacket(payload, 4 * 1024);
      return consumeVoiceAnnouncement(announcement, participant.identity);
    }
    return false;
  }

  function consumeVoiceAudioTrack(track, publication, participant) {
    var kind = window.LivekitClient && window.LivekitClient.Track && window.LivekitClient.Track.Kind
      ? window.LivekitClient.Track.Kind.Audio : "audio";
    if (!track || track.kind !== kind || !participant || participant.identity !== voiceExpectedWorker) {
      stopVoiceAudioTrack(track);
      return false;
    }
    var sid = publication && publication.trackSid || track.sid;
    var manifest = typeof sid === "string" ? voiceAnnouncementByTrack[sid] : null;
    var published = typeof sid === "string" ? voicePublishedTracks[sid] : null;
    if (typeof sid !== "string" || !sid || sid !== voiceSubscribingTrackSid
        || !manifest || !published || published.publication !== publication
        || (publication.trackName || publication.name) !== manifest.track_name) {
      try { if (publication) publication.setSubscribed(false); } catch (e) {}
      stopVoiceAudioTrack(track);
      return false;
    }
    voiceSubscribingTrackSid = null;
    voicePendingTracks[sid] = { track: track, publication: publication, participant: participant };
    playVoiceTrack(sid);
    return true;
  }

  function consumeVoicePublishedTrack(publication, participant) {
    var kind = window.LivekitClient && window.LivekitClient.Track && window.LivekitClient.Track.Kind
      ? window.LivekitClient.Track.Kind.Audio : "audio";
    var sid = publication && publication.trackSid;
    if (!publication || publication.kind !== kind || !participant
        || participant.identity !== voiceExpectedWorker || typeof sid !== "string" || !sid) {
      try { if (publication) publication.setSubscribed(false); } catch (e) {}
      return false;
    }
    try { publication.setSubscribed(false); } catch (e) { return false; }
    voicePublishedTracks[sid] = { publication: publication, participant: participant };
    voiceMediaTimers.push(setTimeout(function () {
      if (!voiceAnnouncementByTrack[sid] && voicePublishedTracks[sid]) removeVoicePublishedTrack(sid);
    }, 1000));
    queueVoiceTrack(sid);
    return true;
  }

  function reconcileVoiceRemotePublications(room) {
    var participants = room && room.remoteParticipants;
    if (!participants || typeof participants.forEach !== "function") return;
    participants.forEach(function (participant) {
      if (!participant || participant.identity !== voiceExpectedWorker) return;
      var publications = participant.trackPublications || participant.audioTrackPublications;
      if (!publications || typeof publications.forEach !== "function") return;
      publications.forEach(function (publication) {
        consumeVoicePublishedTrack(publication, participant);
      });
    });
  }

  function removeVoicePublishedTrack(sid) {
    if (typeof sid !== "string" || !sid) return;
    var manifest = voiceAnnouncementByTrack[sid];
    var pending = voicePendingTracks[sid];
    if (pending) stopVoiceAudioTrack(pending.track);
    delete voicePendingTracks[sid];
    delete voicePublishedTracks[sid];
    voicePlayoutQueue = voicePlayoutQueue.filter(function (value) { return value !== sid; });
    if (voiceSubscribingTrackSid === sid) voiceSubscribingTrackSid = null;
    var activeFound = false;
    Object.keys(voiceActivePlayout).forEach(function (announcementId) {
      var active = voiceActivePlayout[announcementId];
      if (active && active.sid === sid) {
        activeFound = true;
        finishVoiceTrack(active, active.started ? "interrupted" : null);
      }
    });
    if (!activeFound) showVoiceResultSpeechFailure(manifest);
    delete voiceAnnouncementByTrack[sid];
    startNextVoiceTrack();
  }

  function expireVoiceAnnouncement(sid) {
    var manifest = voiceAnnouncementByTrack[sid];
    if (!manifest || voicePublishedTracks[sid] || voicePendingTracks[sid]
        || voicePlayoutQueue.indexOf(sid) !== -1 || voiceSubscribingTrackSid === sid) return;
    showVoiceResultSpeechFailure(manifest);
    delete voiceAnnouncementByTrack[sid];
  }

  function queueVoiceTrack(sid) {
    var manifest = voiceAnnouncementByTrack[sid];
    var published = voicePublishedTracks[sid];
    if (!manifest || !published) return;
    if ((published.publication.trackName || published.publication.name) !== manifest.track_name
        || published.participant.identity !== manifest.worker_identity) {
      try { published.publication.setSubscribed(false); } catch (e) {}
      showVoiceResultSpeechFailure(manifest);
      delete voiceAnnouncementByTrack[sid];
      delete voicePublishedTracks[sid];
      return;
    }
    if (voiceSubscribingTrackSid === sid || voicePlayoutQueue.indexOf(sid) !== -1
        || Object.keys(voiceActivePlayout).some(function (announcementId) {
          return voiceActivePlayout[announcementId].sid === sid;
        })) return;
    voicePlayoutQueue.push(sid);
    voicePlayoutQueue.sort(function (left, right) {
      return voiceAnnouncementByTrack[left].announcement_sequence
        - voiceAnnouncementByTrack[right].announcement_sequence;
    });
    startNextVoiceTrack();
  }

  function startNextVoiceTrack() {
    if (voiceSubscribingTrackSid || Object.keys(voiceActivePlayout).length) return;
    while (voicePlayoutQueue.length) {
      var sid = voicePlayoutQueue.shift();
      var manifest = voiceAnnouncementByTrack[sid];
      var published = voicePublishedTracks[sid];
      if (!manifest || !published) {
        showVoiceResultSpeechFailure(manifest);
        delete voiceAnnouncementByTrack[sid];
        delete voicePublishedTracks[sid];
        continue;
      }
      voiceSubscribingTrackSid = sid;
      try { published.publication.setSubscribed(true); }
      catch (e) {
        voiceSubscribingTrackSid = null;
        showVoiceResultSpeechFailure(manifest);
        delete voiceAnnouncementByTrack[sid];
        delete voicePublishedTracks[sid];
        continue;
      }
      voiceMediaTimers.push(setTimeout(function (expectedSid) {
        if (voiceSubscribingTrackSid !== expectedSid) return;
        var value = voicePublishedTracks[expectedSid];
        var expectedManifest = voiceAnnouncementByTrack[expectedSid];
        try { if (value) value.publication.setSubscribed(false); } catch (e) {}
        voiceSubscribingTrackSid = null;
        showVoiceResultSpeechFailure(expectedManifest);
        delete voiceAnnouncementByTrack[expectedSid];
        delete voicePublishedTracks[expectedSid];
        startNextVoiceTrack();
      }, 1000, sid));
      return;
    }
  }

  function stopVoiceAudioTrack(track) {
    if (!track) return;
    try { track.detach().forEach(function (element) { element.remove(); }); } catch (e) {}
  }

  function interruptVoiceAudioTrack(track, publication) {
    var sid = publication && publication.trackSid || track && track.sid;
    var activeFound = false;
    Object.keys(voiceActivePlayout).forEach(function (announcementId) {
      var active = voiceActivePlayout[announcementId];
      if (active && active.sid === sid) {
        activeFound = true;
        finishVoiceTrack(active, active.started ? "interrupted" : null);
      }
    });
    if (!activeFound) removeVoicePublishedTrack(sid);
    else stopVoiceAudioTrack(track);
  }

  function voicePlayout(frame, phase) {
    if (!ws || ws.readyState !== 1) return;
    var event = {
      type: "voice_playout_event",
      schema_version: "1",
      device_id: voiceDeviceId,
      connection_generation: connectionGeneration,
      session_id: frame.session_id,
      generation: frame.generation,
      media_grant_revision: frame.media_grant_revision,
      announcement_id: frame.announcement_id,
      announcement_sequence: frame.announcement_sequence,
      turn_id: frame.turn_id,
      kind: frame.kind,
      quantum_role: frame.quantum_role,
      quantum_index: frame.quantum_index,
      phase: phase,
      client_sequence: voicePlayoutSequence++,
      observed_at: new Date().toISOString(),
    };
    if (frame.result_reserved_samples_after != null) {
      event.result_reserved_samples_after = frame.result_reserved_samples_after;
    }
    send(event);
  }

  function playVoiceTrack(sid) {
    var pending = voicePendingTracks[sid];
    var manifest = voiceAnnouncementByTrack[sid];
    var published = voicePublishedTracks[sid];
    if (Object.keys(voiceActivePlayout).length) return;
    if (!pending || !manifest || !published) {
      showVoiceResultSpeechFailure(manifest);
      delete voicePendingTracks[sid];
      delete voiceAnnouncementByTrack[sid];
      delete voicePublishedTracks[sid];
      startNextVoiceTrack();
      return;
    }
    delete voicePendingTracks[sid];
    var context = ensureVoiceAudioContext();
    var mediaTrack = pending.track.mediaStreamTrack;
    if (!context || (context.sampleRate !== 24000 && context.sampleRate !== 48000)
        || typeof context.createMediaStreamSource !== "function"
        || typeof context.createScriptProcessor !== "function"
        || typeof window.MediaStream !== "function" || !mediaTrack) {
      try { published.publication.setSubscribed(false); } catch (e) {}
      stopVoiceAudioTrack(pending.track);
      showVoiceResultSpeechFailure(manifest);
      delete voiceAnnouncementByTrack[sid];
      delete voicePublishedTracks[sid];
      setVoiceFeedback("unavailable", "media_unavailable", null, true);
      startNextVoiceTrack();
      return;
    }
    var source;
    var processor;
    try {
      source = context.createMediaStreamSource(new window.MediaStream([mediaTrack]));
      processor = context.createScriptProcessor(1024, 1, 1);
    } catch (e) {
      try { published.publication.setSubscribed(false); } catch (_error) {}
      showVoiceResultSpeechFailure(manifest);
      delete voiceAnnouncementByTrack[sid];
      delete voicePublishedTracks[sid];
      startNextVoiceTrack();
      return;
    }
    var active = {
      sid: sid, manifest: manifest, pending: pending, published: published,
      source: source, processor: processor, finished: false, finishing: false,
      started: false, remainingFrames: manifest.duration_samples * (context.sampleRate / 24000),
      tailTimer: null, timeout: null,
    };
    voiceActivePlayout[manifest.announcement_id] = active;
    processor.onaudioprocess = function (event) {
      if (active.finished || active.finishing) return;
      var input = event.inputBuffer;
      var output = event.outputBuffer;
      var available = output.length;
      var accepted = Math.min(available, active.remainingFrames);
      for (var channel = 0; channel < output.numberOfChannels; channel++) {
        var outputData = output.getChannelData(channel);
        outputData.fill(0);
        if (accepted > 0 && input.numberOfChannels > 0) {
          var inputData = input.getChannelData(Math.min(channel, input.numberOfChannels - 1));
          outputData.set(inputData.subarray(0, accepted), 0);
        }
      }
      if (accepted > 0 && !active.started) {
        active.started = true;
        if (manifest.kind === "greeting") setVoiceFeedback("greeting", "ready", null, true);
        voicePlayout(manifest, "started");
      }
      active.remainingFrames -= accepted;
      if (active.remainingFrames === 0) {
        active.finishing = true;
        active.tailTimer = setTimeout(function () { finishVoiceTrack(active, "finished"); },
          Math.ceil(accepted * 1000 / context.sampleRate) + 20);
        voiceMediaTimers.push(active.tailTimer);
      }
    };
    try {
      source.connect(processor);
      processor.connect(context.destination);
    } catch (e) {
      finishVoiceTrack(active, null);
      return;
    }
    active.timeout = setTimeout(function () {
      finishVoiceTrack(active, active.started ? "interrupted" : null);
    }, Math.ceil(manifest.duration_samples / 24) + 2000);
    voiceMediaTimers.push(active.timeout);
    if (context.state === "suspended") {
      try { Promise.resolve(context.resume()).then(hideVoiceAudioResume).catch(showVoiceAudioResume); }
      catch (e) { showVoiceAudioResume(); }
    }
  }

  function finishVoiceTrack(active, phase, suppressNext) {
    if (!active || active.finished) return;
    active.finished = true;
    if (!active.started && suppressNext !== true) {
      showVoiceResultSpeechFailure(active.manifest);
    }
    if (active.timeout) clearTimeout(active.timeout);
    if (active.tailTimer) clearTimeout(active.tailTimer);
    try { active.processor.onaudioprocess = null; } catch (e) {}
    try { active.source.disconnect(); } catch (e) {}
    try { active.processor.disconnect(); } catch (e) {}
    try { active.published.publication.setSubscribed(false); } catch (e) {}
    stopVoiceAudioTrack(active.pending.track);
    if (phase && active.started) voicePlayout(active.manifest, phase);
    delete voiceActivePlayout[active.manifest.announcement_id];
    delete voiceAnnouncementByTrack[active.sid];
    delete voicePendingTracks[active.sid];
    delete voicePublishedTracks[active.sid];
    if (!suppressNext) startNextVoiceTrack();
  }

  function suspendVoiceForLifecycle(reason) {
    var fence = voiceRecoverableFence();
    if (!fence || voiceLifecycleSuspended) return;
    voiceLifecycleSuspended = true;
    clearVoiceRecovery();
    teardownVoiceMedia(false);
    setVoiceFeedback("suspended", reason, null, true);
    if (voiceBindingIsCurrent()) {
      voiceSuspensionPromise = patchVoiceSession({
        foreground_active: false,
        foreground_reason: reason === "audio_interrupted" ? "audio_interrupted" : "backgrounded",
        microphone_enabled: false,
      }).finally(function () { voiceSuspensionPromise = null; });
    }
  }

  function suspendVoiceForNetworkLoss() {
    var fence = voiceRecoverableFence();
    if (!fence) return;
    voiceLifecycleSuspended = false;
    clearVoiceRecovery();
    teardownVoiceMedia(false);
    setVoiceFeedback("reconnecting", "network_interrupted", null, true);
    if (voiceBindingIsCurrent()) {
      voiceSuspensionPromise = patchVoiceSession({
        foreground_active: false,
        foreground_reason: "connection_lost",
        microphone_enabled: false,
      }).finally(function () { voiceSuspensionPromise = null; });
    }
  }

  function resumeVoiceForLifecycle() {
    if (document.visibilityState === "hidden") return;
    if (navigator.onLine === false) return;
    voiceLifecycleSuspended = false;
    if (!voiceRecoverableFence()) return;
    if (voiceSuspensionPromise) {
      voiceSuspensionPromise.finally(function () {
        if (document.visibilityState !== "hidden" && !voiceLifecycleSuspended) {
          resumeVoiceForLifecycle();
        }
      });
      return;
    }
    if (!voiceBindingIsCurrent()) {
      requestFreshVoiceBinding(voiceBinding && voiceBinding.binding_id);
      return;
    }
    beginVoiceRecovery("network_interrupted");
  }

  function installVoiceCapabilityWatchers() {
    if (navigator.permissions && typeof navigator.permissions.query === "function") {
      navigator.permissions.query({ name: "microphone" }).then(function (permission) {
        var apply = function () {
          voicePermissionState = permission.state === "granted" ? "authorized"
            : permission.state === "denied" ? "denied" : "not_determined";
          if (permission.state === "denied" && currentVoiceFence()) handleVoiceMediaLoss("permission_denied");
        };
        apply();
        if (permission.addEventListener) permission.addEventListener("change", apply);
      }).catch(function () {});
    }
    if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
      navigator.mediaDevices.addEventListener("devicechange", function () {
        if (!currentVoiceFence() || !navigator.mediaDevices.enumerateDevices) return;
        navigator.mediaDevices.enumerateDevices().then(function (devices) {
          if (!devices.some(function (device) { return device.kind === "audioinput"; })) {
            handleVoiceMediaLoss("no_microphone");
          }
        }).catch(function () {});
      });
    }
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") suspendVoiceForLifecycle("backgrounded");
      else resumeVoiceForLifecycle();
    });
    window.addEventListener("pagehide", function () { suspendVoiceForLifecycle("backgrounded"); });
    window.addEventListener("pageshow", function () { resumeVoiceForLifecycle(); });
    window.addEventListener("offline", suspendVoiceForNetworkLoss);
    window.addEventListener("online", resumeVoiceForLifecycle);
  }

  if (voiceAudioResumeEl) voiceAudioResumeEl.addEventListener("click", function () {
    var resumptions = [];
    if (voiceRoom && typeof voiceRoom.startAudio === "function") {
      try { resumptions.push(Promise.resolve(voiceRoom.startAudio())); }
      catch (e) { resumptions.push(Promise.reject(e)); }
    }
    var context = ensureVoiceAudioContext();
    if (context && typeof context.resume === "function") {
      try { resumptions.push(Promise.resolve(context.resume())); }
      catch (e) { resumptions.push(Promise.reject(e)); }
    }
    if (!resumptions.length) return;
    Promise.all(resumptions).then(hideVoiceAudioResume).catch(showVoiceAudioResume);
  });
  installVoiceCapabilityWatchers();

  function send(obj) { try { ws.send(JSON.stringify(obj)); } catch (e) {} }

  /** Create the client-owned retry/generation identity before any socket I/O. */
  function beginOperationSubmission(name, payload, suppliedGeneration, exposeStatus) {
    var body = Object.assign({}, payload || {});
    var submissionId = isCanonicalUuid4(body.submission_id) ? body.submission_id : randomUuid4();
    var requestGeneration = isCanonicalUuid4(suppliedGeneration)
      ? suppliedGeneration
      : (isCanonicalUuid4(body.request_generation) ? body.request_generation : randomUuid4());
    body.submission_id = submissionId;
    body.request_generation = requestGeneration;
    var local = {
      submission_id: submissionId,
      request_generation: requestGeneration,
      action: name,
      chat_id: activeChatId || null,
      state: "submitting",
      label: "Submitting…",
      shows_status: exposeStatus !== false,
      status_order: ++operationSubmissionOrdinal,
      // 066: retained so a failed turn can offer an exact retry.
      message: name === "chat_message" && typeof body.message === "string" ? body.message : null,
    };
    operationSubmissionByGeneration[requestGeneration] = local;
    operationSubmissionById[submissionId] = local;
    if (local.shows_status) {
      setStatus(local.label, true, "operation-submission:" + requestGeneration);
    }
    return { payload: body, submissionId: submissionId, requestGeneration: requestGeneration };
  }

  function finishOperationSubmission(requestGeneration) {
    var local = operationSubmissionByGeneration[requestGeneration];
    if (!local) return false;
    delete operationSubmissionByGeneration[requestGeneration];
    delete operationSubmissionById[local.submission_id];
    return true;
  }

  // ---- 066: connection honesty. Actions attempted while the socket is not
  // healthily registered queue visibly (bounded) or refuse loudly — they are
  // never silently dropped. `socketReady` flips on the post-registration
  // rote_config verdict and off on close.
  var socketReady = false;
  var pendingActions = [];
  var PENDING_ACTION_LIMIT = 5;
  var PENDING_ACTION_TTL_MS = 45000;
  function isSocketReady() {
    return socketReady && ws && ws.readyState === 1;
  }
  function setConnState(state, text) {
    var pill = document.getElementById("astral-conn");
    var textEl = document.getElementById("astral-conn-text");
    if (!pill) return;
    if (state === "connected") { pill.hidden = true; return; }
    pill.hidden = false;
    pill.setAttribute("data-conn", state);
    if (textEl) textEl.textContent = text
      || (state === "connecting" ? "Connecting…" : "Reconnecting — messages will queue");
  }
  function queueOutboundAction(entry) {
    if (pendingActions.length >= PENDING_ACTION_LIMIT) {
      showToast("Not connected — too many queued actions. Try again shortly.", "error");
      return false;
    }
    entry.at = Date.now();
    entry.timer = setTimeout(function () {
      var idx = pendingActions.indexOf(entry);
      if (idx !== -1) pendingActions.splice(idx, 1);
      if (entry.onRefused) entry.onRefused();
      else showToast("Still not connected — the action was not sent.", "error");
    }, PENDING_ACTION_TTL_MS);
    pendingActions.push(entry);
    setConnState(ws && ws.readyState === 0 ? "connecting" : "offline");
    return true;
  }
  function flushPendingActions() {
    var entries = pendingActions;
    pendingActions = [];
    for (var i = 0; i < entries.length; i++) {
      clearTimeout(entries[i].timer);
      try { entries[i].dispatch(); } catch (e) {}
    }
  }

  function action(name, payload, exposeStatus) {
    if (name === "chat_message") openRequest("commit", activeChatId);
    var suppliedGeneration = requestState && (name === "chat_message" || name === "load_chat")
      ? requestState.generation : null;
    var submission = beginOperationSubmission(name, payload, suppliedGeneration, exposeStatus);
    var frame = {
      type: "ui_event",
      action: name,
      payload: submission.payload,
      session_id: activeChatId || undefined,
      submission_id: submission.submissionId,
      request_generation: submission.requestGeneration,
    };
    if (connectionGeneration) frame.connection_generation = connectionGeneration;
    if (!isSocketReady() && name !== "get_history" && name !== "watch_task") {
      // Queue chrome/settings actions too (FR-015): the same no-silent-drop
      // rule chat sends get. Frames are rebuilt at dispatch time so the
      // then-current connection_generation is used.
      finishOperationSubmission(submission.requestGeneration);
      queueOutboundAction({
        label: name,
        dispatch: function () { action(name, payload, exposeStatus); },
        onRefused: function () {
          showToast("“" + name.replace(/_/g, " ") + "” was not sent — still reconnecting.", "error");
        },
      });
      return submission;
    }
    send(frame);
    return submission;
  }

  /** Persist and bind resume scope before the registration frame is sent. */
  function sendRegistration(resumed) {
    var resume;
    if (activeChatId) {
      persistActiveChatLocator(activeChatId);
      openRequest("hydration", activeChatId);
      resume = {
        schema_version: 1,
        active_chat_id: activeChatId,
        request_generation: requestState.generation,
      };
    }
    var device = detectDeviceCapabilities();
    send({
      type: "register_ui",
      token: token,
      capabilities: ["render", "stream", "voice"],
      session_id: "ui-" + Date.now(),
      device_id: voiceDeviceId,
      device: device,
      resumed: resumed,
      connection_generation: connectionGeneration,
      resume: resume,
    });
  }

  function loadActiveChat(chatId) {
    if (!selectActiveChat(chatId, "hydration")) return;
    action("load_chat", {
      chat_id: chatId,
      connection_generation: connectionGeneration,
      request_generation: requestState.generation,
      snapshot_purpose: "hydration",
    });
  }

  // ---- Plotly lazy loader: the library left the shell <head> (feature 052);
  // it is injected once on first chart need and idle-prefetched after boot ----
  var plotlyLoading = false;
  var plotlyCallbacks = [];
  function ensurePlotly(cb) {
    if (typeof Plotly !== "undefined") { if (cb) { try { cb(); } catch (e) {} } return; }
    if (cb) plotlyCallbacks.push(cb);
    if (plotlyLoading) return;
    plotlyLoading = true;
    var s = document.createElement("script");
    s.src = window.__ASTRAL_PLOTLY_URL__ || "/static/vendor/plotly.min.js";
    s.onload = function () {
      var cbs = plotlyCallbacks;
      plotlyCallbacks = [];
      for (var i = 0; i < cbs.length; i++) { try { cbs[i](); } catch (e) {} }
    };
    // allow a later chart render to retry the injection after a load failure
    s.onerror = function () { plotlyLoading = false; };
    document.head.appendChild(s);
  }
  var pendingChartRoots = [];
  function flushPendingCharts() {
    var roots = pendingChartRoots;
    pendingChartRoots = [];
    for (var i = 0; i < roots.length; i++) initCharts(roots[i]);
  }

  // ---- Plotly chart init from server-rendered data-chart placeholders ----
  function initCharts(root) {
    if (typeof Plotly === "undefined") {
      if (root.querySelectorAll(".astral-chart").length) {
        pendingChartRoots.push(root);
        ensurePlotly(flushPendingCharts);
      }
      return;
    }
    var els = root.querySelectorAll(".astral-chart");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.dataset.rendered) continue;
      var kind = el.dataset.chartType, spec;
      try { spec = JSON.parse(el.dataset.chart || "{}"); } catch (e) { continue; }
      var layout = {
        autosize: true, height: window.innerWidth < 640 ? 240 : 320,
        margin: { l: 40, r: 20, t: 20, b: 40 },
        paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
        font: { color: "#9CA3AF" },
        xaxis: { gridcolor: "rgba(255,255,255,0.1)", tickfont: { size: 10 } },
        yaxis: { gridcolor: "rgba(255,255,255,0.1)", tickfont: { size: 10 } },
      };
      var traces, cfg = { displayModeBar: false, responsive: true };
      if (kind === "bar") traces = [{ x: spec.labels, y: spec.data, type: "bar", marker: { color: "#6366F1" } }];
      else if (kind === "line") traces = [{ x: spec.labels, y: spec.data, type: "scatter", mode: "lines+markers", marker: { color: "#6366F1" }, line: { color: "#6366F1", width: 2 } }];
      else if (kind === "pie") {
        var palette = (spec.colors && spec.colors.length) ? spec.colors : ["#6366F1", "#8B5CF6", "#06B6D4", "#10B981", "#F59E0B", "#EF4444", "#EC4899", "#3B82F6"];
        traces = [{ values: spec.data, labels: spec.labels, type: "pie", marker: { colors: palette }, textinfo: "label+percent", hole: 0.4 }];
        layout.margin = { l: 20, r: 20, t: 20, b: 20 }; layout.showlegend = true; layout.legend = { orientation: "h", y: -0.1 };
      } else if (kind === "plotly") {
        traces = spec.data || [];
        layout = Object.assign(layout, spec.layout || {});
        cfg = Object.assign(cfg, spec.config || {});
      } else continue;
      try { Plotly.newPlot(el, traces, layout, cfg); el.dataset.rendered = "1"; } catch (e) {}
    }
  }

  // ---- theme_apply: set --astral-* CSS vars from emitted banners ----
  function hexToChannels(hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec((hex || "").trim());
    if (!m) return null;
    var n = parseInt(m[1], 16);
    return (n >> 16 & 255) + " " + (n >> 8 & 255) + " " + (n & 255);
  }
  var PRESETS = {
    midnight: { bg: "#0F1221", surface: "#1A1E2E", primary: "#6366F1", secondary: "#8B5CF6", text: "#F3F4F6", muted: "#9CA3AF", accent: "#06B6D4" },
    daylight: { bg: "#F8FAFC", surface: "#FFFFFF", primary: "#4F46E5", secondary: "#7C3AED", text: "#1E293B", muted: "#64748B", accent: "#0891B2" },
    ocean: { bg: "#0C1222", surface: "#132038", primary: "#0EA5E9", secondary: "#06B6D4", text: "#E2E8F0", muted: "#94A3B8", accent: "#2DD4BF" },
    sunset: { bg: "#1C1017", surface: "#2D1B24", primary: "#F97316", secondary: "#EF4444", text: "#FEF2F2", muted: "#A8A29E", accent: "#FBBF24" },
    forest: { bg: "#0F1A14", surface: "#1A2E22", primary: "#22C55E", secondary: "#10B981", text: "#ECFDF5", muted: "#86EFAC", accent: "#A3E635" },
  };
  function setColor(key, hex) { var ch = hexToChannels(hex); if (ch) document.documentElement.style.setProperty("--astral-" + key, ch); }
  function applyTheme(spec) {
    if (spec.preset && PRESETS[spec.preset]) { var p = PRESETS[spec.preset]; for (var k in p) setColor(k, p[k]); }
    else if (spec.colors) { for (var k2 in spec.colors) setColor(k2, spec.colors[k2]); }
    else if (spec.color_key && spec.color_value) setColor(spec.color_key, spec.color_value);
  }
  function processSideEffects(root) {
    initCharts(root);
    var themes = root.querySelectorAll(".astral-theme-apply");
    for (var i = 0; i < themes.length; i++) { try { applyTheme(JSON.parse(themes[i].dataset.theme || "{}")); } catch (e) {} }
  }

  // ---- render server HTML into a region ----
  function setHTML(region, htmlStr) { region.innerHTML = htmlStr || ""; processSideEffects(region); }
  function appendHTML(region, htmlStr) {
    var d = document.createElement("div"); d.innerHTML = htmlStr || "";
    region.appendChild(d); processSideEffects(d);
    region.scrollTop = region.scrollHeight;
  }
  function appendChatBubble(role, htmlStr) {
    var wrap = document.createElement("div");
    wrap.className = role === "user" ? "flex justify-end" : "flex justify-start";
    var bubble = document.createElement("div");
    bubble.className = (role === "user"
      ? "bg-astral-primary/20 border border-astral-primary/30"
      : "bg-white/5 border border-white/5") + " rounded-lg p-3 max-w-[85%] text-sm text-astral-text";
    bubble.innerHTML = htmlStr || "";
    wrap.appendChild(bubble); chat.appendChild(wrap); processSideEffects(bubble);
    chat.scrollTop = chat.scrollHeight;
    if (role !== "user") noteAssistantActivity();
  }

  // 066: inline failed-turn notice — the user's message stays visible, the
  // failure is explained beside the conversation, and retry re-sends the
  // exact content. Never blanks the canvas.
  function appendFailedTurnNotice(message, retryText, generation) {
    var err = document.createElement("div");
    err.className = "astral-chat-error";
    err.setAttribute("role", "alert");
    if (generation) err.setAttribute("data-turn-generation", generation);
    var line = document.createElement("div");
    line.textContent = message || "The turn could not be completed.";
    err.appendChild(line);
    if (retryText) {
      var retry = document.createElement("button");
      retry.type = "button";
      retry.className = "astral-retry-btn";
      retry.textContent = "↻ Retry";
      retry.addEventListener("click", function () {
        if (err.parentNode) err.parentNode.removeChild(err);
        sendChat(retryText);
      });
      err.appendChild(retry);
    }
    chat.appendChild(err);
    chat.scrollTop = chat.scrollHeight;
    noteAssistantActivity();
  }

  // 066: a failed turn materializes its transient content into the canonical
  // rail (instead of evaporating with the overlay) and gains a retry.
  function surfaceFailedTurn(frame, localSubmission) {
    if (frame.action !== "chat_message") return;
    try {
      var retryText = localSubmission && localSubmission.message
        ? localSubmission.message : null;
      // Move whatever the turn had staged in the overlay into the canonical
      // rail (it is about to be cleared)...
      var overlayChat = transientOverlay && transientOverlay.chat;
      var moved = false;
      if (overlayChat) {
        while (overlayChat.firstChild) {
          chat.appendChild(overlayChat.firstChild);
          moved = true;
        }
      }
      // ...and if the overlay was already gone, rebuild the user's message
      // from the retained submission so their words NEVER disappear on a
      // failure (066 FR-017). Deterministic, not overlay-dependent.
      if (!moved && retryText) {
        appendChatBubble("user", "<div>" + escapeText(retryText) + "</div>");
      }
      appendFailedTurnNotice(
        (frame.error && frame.error.message) || "The turn could not be completed.",
        retryText, frame.request_generation);
    } catch (e) {}
  }

  // 066: a turn whose operation reported a non-final failure (e.g. an
  // execution lease expiring during a slow model call) can still go on to
  // succeed. When any later content or completion arrives for the same
  // request generation, retract the failure notice and its status so the
  // user is never told a turn failed while its answer is on screen.
  function retractFailedTurnNotice(generation) {
    if (!generation) return;
    var nodes = chat.querySelectorAll(
      '.astral-chat-error[data-turn-generation="' + generation + '"]');
    for (var i = 0; i < nodes.length; i++) nodes[i].remove();
    if (statusOwner && statusOwner.indexOf("operation-error:") === 0) setStatus("");
  }

  // ---- query-start loading skeleton ----
  // Client-local optimistic placeholder (the Android twin's SkeletonCanvas):
  // appended to the canvas when a chat turn is sent, removed by the FIRST
  // canvas content of the turn (render/upsert/stream) or when the turn ends
  // without any (text-only answers, errors, cancellation). Reuses the
  // .astral-skeleton-line shimmer the server-driven skeleton primitive ships.
  function showSkeleton() {
    if (timelineMode || document.getElementById("astral-canvas-skeleton")) return;
    hideCanvasEmpty(); // the welcome placeholder never coexists with the loading skeleton
    var d = document.createElement("div");
    d.id = "astral-canvas-skeleton";
    d.className = "astral-skeleton";
    d.setAttribute("role", "status");
    d.setAttribute("aria-busy", "true");
    d.setAttribute("aria-live", "polite");
    d.innerHTML = '<span class="sr-only">Loading…</span>'
      + '<div class="astral-skeleton-line h-3 w-1/3 mb-3"></div>'
      + '<div class="astral-skeleton-line h-20 w-full mb-3"></div>'
      + '<div class="astral-skeleton-line h-20 w-full mb-3"></div>'
      + '<div class="astral-skeleton-line h-3 w-1/2 mb-2"></div>';
    var host = requestState ? ensureTransientOverlay().canvas : canvas;
    host.appendChild(d);
    canvas.scrollTop = canvas.scrollHeight;
  }
  function hideSkeleton() {
    var d = document.getElementById("astral-canvas-skeleton");
    if (d && d.parentNode) d.parentNode.removeChild(d);
  }

  // Feature 055 (uniform rule, wire-contract §1): turn start drops the
  // ephemeral welcome components (identity prefix "wel_") from the canvas.
  // SELECTIVE removal only — mid-chat the canvas holds client-side workspace
  // nodes a blanket clear would lose. Unconditional on purpose: when the
  // server flag is off the welcome arrives id-less, nothing matches, and
  // this is a no-op.
  function purgeWelcome() {
    var nodes = canvas.querySelectorAll('[data-component-id^="wel_"]');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].parentNode) nodes[i].parentNode.removeChild(nodes[i]);
    }
    // Legacy safety: bare-id welcome nodes sitting directly under the canvas.
    for (var j = canvas.children.length - 1; j >= 0; j--) {
      var kid = canvas.children[j];
      if (kid.id && kid.id.indexOf("wel_") === 0) canvas.removeChild(kid);
    }
  }

  // ---- workspace upsert morph ----
  // Each op targets [data-component-id]: replace the node in place when it
  // exists (no flicker, neighbors untouched), append when new, remove on op
  // 'remove'. Side effects (Plotly/theme) re-run on inserted subtrees only.
  function componentSelector(id) {
    return '[data-component-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]';
  }
  function ensureRenderer() {
    var renderer = canvas.querySelector(".dynamic-renderer");
    if (!renderer) {
      renderer = document.createElement("div");
      renderer.className = "dynamic-renderer space-y-3";
      canvas.innerHTML = "";
      canvas.appendChild(renderer);
    }
    return renderer;
  }
  function applyUpsert(msg) {
    if (msg.chat_id && activeChatId && msg.chat_id !== activeChatId) return;
    if (timelineMode) {
      setStatus("Live workspace updated — use “Back to live” to see it.");
      return;
    }
    hideSkeleton(); // first canvas content of the turn
    var ops = msg.ops || [];
    if (ops.length) hideCanvasEmpty(); // content is arriving on the canvas
    var renderer = ensureRenderer();
    for (var i = 0; i < ops.length; i++) {
      var op = ops[i];
      if (!op || !op.component_id) continue;
      var node = canvas.querySelector(componentSelector(op.component_id));
      if (op.op === "remove") {
        if (node) node.parentNode.removeChild(node);
        continue;
      }
      if (!op.html) continue;
      var holder = document.createElement("div");
      holder.innerHTML = op.html;
      var fresh = holder.firstElementChild;
      if (!fresh) continue;
      if (node) node.replaceWith(fresh);
      else renderer.appendChild(fresh);
      processSideEffects(fresh);
    }
    syncCanvasToolbar(); // last-known flags (full renders refresh them)
  }

  // Plotly keeps per-node state and handlers; purge a node's charts before it
  // is replaced. The bundle is lazy-loaded (052) — nothing to purge before it
  // exists.
  function purgeCharts(node) {
    if (!node || typeof Plotly === "undefined") return;
    var els = node.querySelectorAll(".astral-chart");
    for (var i = 0; i < els.length; i++) {
      if (els[i].dataset.rendered) { try { Plotly.purge(els[i]); } catch (e) {} }
    }
  }

  // ---- streaming merge: replace-or-append a per-stream node keyed by stream_id ----
  // Frames carrying component_id (055 stream→artifact bridge, wire-contract
  // §2) are keyed by [data-component-id] from the FIRST frame instead — no
  // stream-<id> node ever exists for them — so the terminal persist ui_upsert
  // replaces the same node in place rather than double-rendering.
  var streamChartPlot = {}; // stream_id → last chart re-plot ms (interim ≤1/s)
  function mergeStream(msg) {
    var htmlStr = msg.html || "";
    if (msg.error) {
      htmlStr = '<div class="text-xs text-red-400 border border-red-500/20 rounded p-2">' +
        escapeText(msg.error.message || "stream error") + "</div>";
    }
    if (msg.component_id) { mergeKeyedStream(msg, htmlStr); return; }
    var id = "stream-" + msg.stream_id;
    var node = document.getElementById(id);
    if (!htmlStr && !msg.terminal) return;
    hideSkeleton(); // streamed canvas content counts as the first component
    if (node) { node.innerHTML = htmlStr; processSideEffects(node); }
    else if (htmlStr) {
      hideCanvasEmpty();
      node = document.createElement("div"); node.id = id; node.innerHTML = htmlStr;
      canvas.appendChild(node); processSideEffects(node);
    }
  }
  function mergeKeyedStream(msg, htmlStr) {
    if (msg.terminal) delete streamChartPlot[msg.stream_id];
    else if (htmlStr.indexOf("astral-chart") !== -1) {
      // Chart-bearing interim frames re-plot at most once per second per
      // stream (leak/flicker guard); the terminal frame always renders.
      var now = Date.now();
      if (now - (streamChartPlot[msg.stream_id] || 0) < 1000) return;
      streamChartPlot[msg.stream_id] = now;
    }
    if (!htmlStr) return; // empty terminal: keep the last content for the persist upsert
    hideSkeleton(); // streamed canvas content counts as the first component
    var node = canvas.querySelector(componentSelector(msg.component_id));
    var holder = document.createElement("div");
    holder.innerHTML = htmlStr;
    var fresh = holder.firstElementChild;
    if (!fresh) return;
    if (holder.children.length > 1 ||
        fresh.getAttribute("data-component-id") !== msg.component_id) {
      // Client-built error html (and any fragment the server did not wrap)
      // still needs the identity anchor or later frames would append copies.
      fresh = document.createElement("div");
      fresh.setAttribute("data-component-id", msg.component_id);
      while (holder.firstChild) fresh.appendChild(holder.firstChild);
    }
    purgeCharts(node);
    if (node) node.replaceWith(fresh);
    else { hideCanvasEmpty(); ensureRenderer().appendChild(fresh); }
    processSideEffects(fresh);
  }

  // ---- feature 060: atomic conversation snapshot + transient overlay ----
  function exactKeys(value, expected) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    return Object.keys(value).sort().join(",") === expected.slice().sort().join(",");
  }

  function stableStringify(value) {
    if (value === null || typeof value !== "object") return JSON.stringify(value);
    if (Array.isArray(value)) return "[" + value.map(stableStringify).join(",") + "]";
    return "{" + Object.keys(value).sort().map(function (key) {
      return JSON.stringify(key) + ":" + stableStringify(value[key]);
    }).join(",") + "}";
  }

  /** Clone semantic protocol data while excluding web-only presentation. */
  function semanticClone(value) {
    if (Array.isArray(value)) return value.map(semanticClone);
    if (!value || typeof value !== "object") return value;
    var clone = {};
    Object.keys(value).forEach(function (key) {
      if (key === "_presentation") return;
      clone[key] = semanticClone(value[key]);
    });
    return clone;
  }

  function validateSemanticJson(value) {
    if (value === null || typeof value === "string" || typeof value === "boolean") return;
    if (typeof value === "number") { if (!Number.isFinite(value)) throw new Error("semantic_number"); return; }
    if (Array.isArray(value)) { value.forEach(validateSemanticJson); return; }
    if (!value || typeof value !== "object") throw new Error("semantic_value");
    Object.keys(value).forEach(function (key) {
      if (key === "_presentation") throw new Error("nested_presentation");
      validateSemanticJson(value[key]);
    });
  }

  function validateSemanticComponent(component) {
    if (!component || typeof component !== "object" || Array.isArray(component)
        || typeof component.type !== "string" || !component.type) throw new Error("component_not_object");
    if (component.component_id != null
        && (typeof component.component_id !== "string" || !component.component_id)) throw new Error("component_identity");
    Object.keys(component).forEach(function (key) {
      if (key !== "_presentation") validateSemanticJson(component[key]);
    });
  }

  /**
   * Validate server-rendered web fragments before any live DOM mutation.
   * Every non-empty top-level component carries exactly the reserved envelope;
   * native semantic fields remain outside it and drive snapshot equality.
   */
  function prepareWebPresentation(components) {
    if (!Array.isArray(components)) throw new Error("components_not_array");
    var nodes = [];
    var envelopes = [];
    var sharedWorkspace = null;
    components.forEach(function (component) {
      validateSemanticComponent(component);
      var envelope = component._presentation;
      if (!exactKeys(envelope, ["html", "target", "workspace"])) throw new Error("presentation_shape");
      if (envelope.target !== "web" || typeof envelope.html !== "string" || !envelope.html) throw new Error("presentation_target");
      var workspace = envelope.workspace;
      if (!exactKeys(workspace, ["export", "share"])
          || typeof workspace.export !== "boolean" || typeof workspace.share !== "boolean") {
        throw new Error("presentation_workspace");
      }
      if (sharedWorkspace === null) sharedWorkspace = { export: workspace.export, share: workspace.share };
      else if (sharedWorkspace.export !== workspace.export || sharedWorkspace.share !== workspace.share) {
        throw new Error("presentation_workspace_conflict");
      }
      var template = document.createElement("template");
      template.innerHTML = envelope.html;
      var hasTextSibling = Array.prototype.some.call(template.content.childNodes, function (node) {
        return node.nodeType === 3 && node.textContent.trim();
      });
      if (template.content.childElementCount !== 1 || hasTextSibling) throw new Error("presentation_fragment");
      if (template.content.querySelector("script,iframe,object,embed")) throw new Error("presentation_unsafe_element");
      if (component.component_id
          && template.content.firstElementChild.getAttribute("data-component-id") !== component.component_id) {
        throw new Error("presentation_identity");
      }
      nodes.push(template.content.firstElementChild);
      envelopes.push({ target: envelope.target, html: envelope.html, workspace: sharedWorkspace });
    });
    return { nodes: nodes, envelopes: envelopes, workspace: sharedWorkspace };
  }

  function createChatBubbleNode(role) {
    var wrap = document.createElement("div");
    wrap.className = role === "user" ? "flex justify-end" : "flex justify-start";
    var bubble = document.createElement("div");
    bubble.className = (role === "user"
      ? "bg-astral-primary/20 border border-astral-primary/30"
      : "bg-white/5 border border-white/5") + " rounded-lg p-3 max-w-[85%] text-sm text-astral-text";
    wrap.appendChild(bubble);
    return { wrap: wrap, bubble: bubble };
  }

  /** Decode one validated semantic message into a detached visible bubble. */
  function decodeSemanticMessage(message) {
    var built = createChatBubbleNode(message.role);
    var presentations = [];
    var hasVisibleContent = false;
    message.attachments.forEach(function (attachment) {
      var chip = document.createElement("div");
      chip.className = "astral-attachment-chip";
      var label = [attachment.filename, attachment.name, attachment.attachment_id].find(function (value) {
        return typeof value === "string" && value;
      }) || "file";
      chip.textContent = "Attachment: " + label;
      built.bubble.appendChild(chip);
      hasVisibleContent = true;
    });
    message.parts.forEach(function (part) {
      if (part.type === "text") {
        var textPart = document.createElement("div");
        // 066: render the server's markdown rendition when present (same
        // escape-first pipeline as the live path); plain text otherwise.
        var textEnvelope = part._presentation;
        if (textEnvelope) {
          textPart.className = "astral-bubble-md";
          var textTemplate = document.createElement("template");
          textTemplate.innerHTML = textEnvelope.html;
          if (textTemplate.content.querySelector("script,iframe,object,embed")) {
            throw new Error("presentation_unsafe_element");
          }
          textPart.appendChild(textTemplate.content);
          presentations.push({ target: textEnvelope.target, html: textEnvelope.html });
        } else {
          textPart.textContent = part.text;
        }
        built.bubble.appendChild(textPart);
        if (part.text) hasVisibleContent = true;
      } else if (part.type === "components") {
        var prepared = prepareWebPresentation(part.components);
        if (!prepared.nodes.length) {
          var emptyRecovery = document.createElement("div");
          emptyRecovery.setAttribute("role", "alert");
          emptyRecovery.textContent = "A saved response could not be displayed.";
          built.bubble.appendChild(emptyRecovery);
          hasVisibleContent = true;
        }
        prepared.nodes.forEach(function (node) { built.bubble.appendChild(node); });
        if (prepared.nodes.length) hasVisibleContent = true;
        presentations = presentations.concat(prepared.envelopes);
      } else if (part.type === "structured") {
        var structured = document.createElement("div");
        structured.textContent = part.plain_text;
        structured.setAttribute("data-structured-value", stableStringify(part.value));
        built.bubble.appendChild(structured);
        if (part.plain_text) hasVisibleContent = true;
      } else if (part.type === "recovery") {
        var recovery = document.createElement("div");
        recovery.setAttribute("role", "alert");
        recovery.setAttribute("data-recovery-code", part.code);
        recovery.textContent = part.message;
        built.bubble.appendChild(recovery);
        hasVisibleContent = true;
      }
    });
    if (!hasVisibleContent) {
      var fallback = document.createElement("div");
      fallback.setAttribute("role", "alert");
      fallback.textContent = "A saved response could not be displayed.";
      built.bubble.appendChild(fallback);
    }
    return { node: built.wrap, presentations: presentations };
  }

  function validateSnapshotShape(frame) {
    var top = ["canvas", "chat_id", "committed_at", "connection_generation", "render_revision",
      "request_generation", "schema_version", "snapshot_id", "snapshot_purpose", "transcript", "type"];
    if (!exactKeys(frame, top) || frame.type !== "conversation_snapshot" || frame.schema_version !== 1) {
      throw new Error("snapshot_shape");
    }
    if (!isCanonicalUuid4(frame.snapshot_id) || !isCanonicalUuid4(frame.chat_id)
        || !isCanonicalUuid4(frame.connection_generation) || !isCanonicalUuid4(frame.request_generation)) {
      throw new Error("snapshot_identity");
    }
    if (frame.snapshot_purpose !== "hydration" && frame.snapshot_purpose !== "commit") throw new Error("snapshot_purpose");
    if (!Number.isSafeInteger(frame.render_revision) || frame.render_revision < 0) throw new Error("snapshot_revision");
    if (!isRfc3339Utc(frame.committed_at) || !Array.isArray(frame.transcript)) throw new Error("snapshot_time_or_transcript");
    frame.transcript.forEach(function (message) {
      if (!exactKeys(message, ["attachments", "created_at", "message_id", "parts", "role"])
          || typeof message.message_id !== "string" || !message.message_id
          || ["user", "assistant", "system", "tool"].indexOf(message.role) === -1
          || !isRfc3339Utc(message.created_at) || !Array.isArray(message.parts) || !message.parts.length
          || !Array.isArray(message.attachments)) throw new Error("snapshot_message");
      message.attachments.forEach(function (attachment) {
        if (!attachment || typeof attachment !== "object" || Array.isArray(attachment)) throw new Error("snapshot_attachment");
        validateSemanticJson(attachment);
      });
      message.parts.forEach(function (part) {
        if (!part || typeof part !== "object" || Array.isArray(part)) throw new Error("snapshot_part");
        if (part.type === "text") {
          // 066: an assistant text part may carry the transport-only web
          // rendition envelope (2 keys — never the components' workspace key).
          if ((!exactKeys(part, ["text", "type"]) && !exactKeys(part, ["_presentation", "text", "type"]))
              || typeof part.text !== "string") throw new Error("snapshot_text_part");
          if (part._presentation && (!exactKeys(part._presentation, ["html", "target"])
              || part._presentation.target !== "web"
              || typeof part._presentation.html !== "string" || !part._presentation.html)) {
            throw new Error("snapshot_text_presentation");
          }
        } else if (part.type === "components") {
          if (!exactKeys(part, ["components", "type"]) || !Array.isArray(part.components)) throw new Error("snapshot_components_part");
        } else if (part.type === "structured") {
          if (!exactKeys(part, ["plain_text", "type", "value"]) || typeof part.plain_text !== "string") throw new Error("snapshot_structured_part");
          validateSemanticJson(part.value);
        } else if (part.type === "recovery") {
          if (!exactKeys(part, ["code", "message", "type"]) || typeof part.code !== "string" || !part.code
              || typeof part.message !== "string" || !part.message) throw new Error("snapshot_recovery_part");
        } else throw new Error("snapshot_part_type");
      });
    });
    if (!exactKeys(frame.canvas, ["components", "target"])
        || frame.canvas.target !== "canvas" || !Array.isArray(frame.canvas.components)) throw new Error("snapshot_canvas");
  }

  function prepareSnapshotCandidate(frame) {
    var transcriptFragment = document.createDocumentFragment();
    var transcriptPresentations = [];
    frame.transcript.forEach(function (message) {
      var decoded = decodeSemanticMessage(message);
      transcriptFragment.appendChild(decoded.node);
      transcriptPresentations = transcriptPresentations.concat(decoded.presentations);
    });
    var canvasPresentation = prepareWebPresentation(frame.canvas.components);
    var canvasRoot = null;
    if (canvasPresentation.nodes.length) {
      canvasRoot = document.createElement("div");
      canvasRoot.className = "dynamic-renderer space-y-3";
      if (canvasPresentation.workspace.export) canvasRoot.setAttribute("data-astral-export", "true");
      if (canvasPresentation.workspace.share) canvasRoot.setAttribute("data-astral-share", "true");
      canvasPresentation.nodes.forEach(function (node) { canvasRoot.appendChild(node); });
    }
    return {
      chatFragment: transcriptFragment,
      canvasRoot: canvasRoot,
      emptyCanvas: !canvasPresentation.nodes.length,
      semanticCanonical: stableStringify(semanticClone(frame)),
      presentationCanonical: stableStringify({
        transcript: transcriptPresentations,
        canvas: canvasPresentation.envelopes,
      }),
    };
  }

  /** Commit a fully prepared transcript and ROTE canvas in one browser task. */
  function commitSnapshotCandidate(candidate, frame) {
    committedRevisionByChat[frame.chat_id] = frame.render_revision;
    lastSnapshotIdByChat[frame.chat_id] = frame.snapshot_id;
    transientOverlay = null;
    chat.replaceChildren(candidate.chatFragment);
    canvas.replaceChildren();
    if (candidate.emptyCanvas) showCanvasEmpty();
    else {
      canvas.appendChild(candidate.canvasRoot);
      processSideEffects(candidate.canvasRoot);
    }
    hideSkeleton();
    timelineMode = false;
    // The committed snapshot is content, not an operation terminal. Keep the
    // correlated progress owner until its canonical terminal frame arrives;
    // otherwise a snapshot from one turn can also erase another active task.
    readCanvasFlags();
    syncCanvasToolbar();
    // Keep the named revision read in this atomic commit seam for audit/source
    // guards and to make clear that overlays never own it.
    lastCommittedRenderRevision();
  }

  function continuityDisposition(code) {
    if (window.console && console.info) console.info("conversation_continuity", code);
    return code;
  }

  /** Open the exact commit fence advertised for detached server work. */
  function acceptConversationCommitReady(frame) {
    var expected = ["chat_id", "connection_generation", "render_revision", "request_generation",
      "schema_version", "type"];
    if (!exactKeys(frame, expected) || frame.type !== "conversation_commit_ready"
        || frame.schema_version !== 1 || !isCanonicalUuid4(frame.chat_id)
        || !isCanonicalUuid4(frame.connection_generation)
        || !isCanonicalUuid4(frame.request_generation)
        || !Number.isSafeInteger(frame.render_revision) || frame.render_revision <= 0) {
      return continuityDisposition("invalid_commit_ready");
    }
    if (!activeChatId || frame.chat_id !== activeChatId
        || frame.connection_generation !== connectionGeneration) {
      return continuityDisposition("wrong_scope");
    }
    if (frame.render_revision <= lastCommittedRenderRevision()) {
      return continuityDisposition("stale_commit_ready");
    }
    // Never steal the fence from a user turn that has been submitted but has
    // not received its own snapshot yet. That later full snapshot will include
    // this already-committed detached update.
    if (requestState && requestState.purpose === "commit" && !requestState.snapshotApplied) {
      return continuityDisposition("commit_request_busy");
    }
    openRequest("commit", frame.chat_id, frame.request_generation);
    return continuityDisposition("commit_ready_applied");
  }

  /** Purpose-aware reducer for the sole committed-state publication. */
  function reduceConversationSnapshot(frame) {
    try { validateSnapshotShape(frame); }
    catch (e) { return continuityDisposition("invalid_snapshot"); }
    if (!activeChatId || !requestState || frame.chat_id !== activeChatId
        || frame.connection_generation !== connectionGeneration
        || frame.request_generation !== requestState.generation) return continuityDisposition("wrong_scope");
    if (frame.snapshot_purpose !== requestState.purpose) return continuityDisposition("wrong_purpose");
    var committed = lastCommittedRenderRevision();
    if (frame.render_revision < committed) return continuityDisposition("stale_frame_ignored");
    if (frame.render_revision === committed && requestState.purpose !== "hydration") {
      return continuityDisposition("unexpected_equal_commit");
    }
    var candidate;
    try { candidate = prepareSnapshotCandidate(frame); }
    catch (e) { return continuityDisposition("invalid_snapshot"); }
    if (frame.render_revision === committed && requestState.hydrationApplied) {
      if (frame.snapshot_id === requestState.acceptedSnapshotId
          && candidate.semanticCanonical === requestState.acceptedSemantic
          && candidate.presentationCanonical === requestState.acceptedPresentation) {
        return continuityDisposition("snapshot_replay");
      }
      return continuityDisposition("revision_conflict");
    }
    var seenIds = seenSnapshotIdsByChat[frame.chat_id];
    if (lastSnapshotIdByChat[frame.chat_id] === frame.snapshot_id
        || (seenIds && seenIds[frame.snapshot_id])) return continuityDisposition("revision_conflict");
    commitSnapshotCandidate(candidate, frame);
    if (!seenIds) { seenIds = Object.create(null); seenSnapshotIdsByChat[frame.chat_id] = seenIds; }
    seenIds[frame.snapshot_id] = true;
    requestState.acceptedSnapshotId = frame.snapshot_id;
    requestState.acceptedSemantic = candidate.semanticCanonical;
    requestState.acceptedPresentation = candidate.presentationCanonical;
    if (requestState.purpose === "hydration") requestState.hydrationApplied = true;
    requestState.snapshotApplied = true;
    if (requestState.purpose === "hydration") {
      // The atomic hydration snapshot is the authoritative completion of a
      // load_chat request. Retire only that local submission/status owner;
      // committed result snapshots still wait for their operation terminal.
      settleHydrationStatus(frame.request_generation);
    }
    continueVoiceAfterHydration(frame);
    return continuityDisposition("snapshot_applied");
  }

  function clearTransientOverlay() {
    if (transientOverlay) {
      if (transientOverlay.chat && transientOverlay.chat.parentNode) transientOverlay.chat.parentNode.removeChild(transientOverlay.chat);
      if (transientOverlay.canvas && transientOverlay.canvas.parentNode) transientOverlay.canvas.parentNode.removeChild(transientOverlay.canvas);
    }
    transientOverlay = null;
  }

  function ensureTransientOverlay() {
    var generation = requestState && requestState.generation;
    if (transientOverlay && transientOverlay.requestGeneration === generation) return transientOverlay;
    clearTransientOverlay();
    var chatRoot = document.createElement("div");
    chatRoot.setAttribute("data-astral-transient-overlay", "chat");
    var canvasRoot = document.createElement("div");
    canvasRoot.setAttribute("data-astral-transient-overlay", "canvas");
    canvasRoot.className = "astral-transient-overlay";
    chat.appendChild(chatRoot);
    canvas.appendChild(canvasRoot);
    transientOverlay = { requestGeneration: generation, chat: chatRoot, canvas: canvasRoot };
    return transientOverlay;
  }

  function acceptTransientFrame(frame) {
    if (!activeChatId || !requestState || requestState.snapshotApplied || !isCanonicalUuid4(frame.chat_id)
        || !isCanonicalUuid4(frame.connection_generation) || !isCanonicalUuid4(frame.request_generation)
        || !Number.isSafeInteger(frame.base_render_revision) || frame.base_render_revision < 0
        || !Number.isSafeInteger(frame.frame_sequence) || frame.frame_sequence < 0) return false;
    if (frame.chat_id !== activeChatId || frame.connection_generation !== connectionGeneration
        || frame.request_generation !== requestState.generation) return false;
    if (frame.base_render_revision !== lastCommittedRenderRevision()) return false;
    if (frame.frame_sequence <= requestState.lastFrameSequence) return false;
    requestState.lastFrameSequence = frame.frame_sequence;
    return true;
  }

  function appendChatBubbleTo(region, role, htmlStr) {
    var built = createChatBubbleNode(role);
    built.bubble.innerHTML = htmlStr || "";
    region.appendChild(built.wrap);
    processSideEffects(built.bubble);
  }

  function applyOverlayUpsert(region, frame) {
    var renderer = region.querySelector(".dynamic-renderer");
    if (!renderer) {
      renderer = document.createElement("div");
      renderer.className = "dynamic-renderer space-y-3";
      region.replaceChildren(renderer);
    }
    (frame.ops || []).forEach(function (op) {
      if (!op || !op.component_id) return;
      var current = region.querySelector(componentSelector(op.component_id));
      if (op.op === "remove") { if (current) current.remove(); return; }
      if (typeof op.html !== "string") return;
      var holder = document.createElement("div");
      holder.innerHTML = op.html;
      var fresh = holder.firstElementChild;
      if (!fresh) return;
      if (current) current.replaceWith(fresh); else renderer.appendChild(fresh);
      processSideEffects(fresh);
    });
  }

  /** Reduce one accepted live frame into disposable request-scoped overlay. */
  function reduceTransientFrame(frame) {
    if (!acceptTransientFrame(frame)) return continuityDisposition("transient_frame_ignored");
    var overlay = ensureTransientOverlay();
    if (frame.type === "ui_render") {
      if (frame.target === "chat") appendChatBubbleTo(overlay.chat, "assistant", frame.html);
      else setHTML(overlay.canvas, frame.html);
    } else if (frame.type === "ui_update") setHTML(overlay.canvas, frame.html);
    else if (frame.type === "ui_append") appendHTML(overlay.canvas, frame.html);
    else if (frame.type === "ui_upsert") applyOverlayUpsert(overlay.canvas, frame);
    else if (frame.type === "ui_stream_data") {
      var streamId = "transient-stream-" + (frame.component_id || frame.stream_id || "current");
      var streamNode = overlay.canvas.querySelector("#" + streamId);
      if (!streamNode) { streamNode = document.createElement("div"); streamNode.id = streamId; overlay.canvas.appendChild(streamNode); }
      streamNode.innerHTML = frame.html || "";
      processSideEffects(streamNode);
    }
    return continuityDisposition("transient_overlay_applied");
  }

  function scopedStatusMatches(frame) {
    var carriesScope = frame.chat_id != null || frame.connection_generation != null || frame.request_generation != null;
    if (!carriesScope) return true;
    if (frame.connection_generation !== connectionGeneration) return false;
    var pending = operationSubmissionByGeneration[frame.request_generation];
    if (pending) return frame.chat_id == null || frame.chat_id === activeChatId;
    if (!requestState || frame.request_generation !== requestState.generation) return false;
    // Surface-only operations deliberately carry chat_id:null. Chat operations
    // additionally bind to the selected chat; neither may cross generations.
    return frame.chat_id == null || !!(activeChatId && frame.chat_id === activeChatId);
  }

  function operationStatusShowsActivity(frame) {
    var local = operationSubmissionByGeneration[frame.request_generation];
    if (local && local.shows_status === false) return false;
    // load_chat can still emit compatibility work after its atomic snapshot.
    // That late operation projection is reconciliation state, not visible
    // activity, because the requested conversation is already restored.
    return !(frame.action === "load_chat" && requestState
      && requestState.purpose === "hydration" && requestState.snapshotApplied
      && frame.request_generation === requestState.generation);
  }

  function newestActiveOperationStatus() {
    var active = null;
    Object.keys(operationStatusById).forEach(function (operationId) {
      var candidate = operationStatusById[operationId];
      if (candidate.terminal || !scopedStatusMatches(candidate)
          || !operationStatusShowsActivity(candidate)) return;
      if (!active || candidate.updated_at > active.updated_at
          || (candidate.updated_at === active.updated_at && candidate.sequence > active.sequence)
          || (candidate.updated_at === active.updated_at && candidate.sequence === active.sequence
            && candidate.operation_id > active.operation_id)) active = candidate;
    });
    return active;
  }

  function newestVisibleLocalSubmission() {
    var newest = null;
    Object.keys(operationSubmissionByGeneration).forEach(function (requestGeneration) {
      var candidate = operationSubmissionByGeneration[requestGeneration];
      if (candidate.shows_status === false
          || (candidate.chat_id != null && candidate.chat_id !== activeChatId)) return;
      if (!newest || (candidate.status_order || 0) > (newest.status_order || 0)) newest = candidate;
    });
    return newest;
  }

  function restoreActiveStatusOrClear(owners) {
    if (owners.indexOf(statusOwner) === -1) return;
    var active = newestActiveOperationStatus();
    if (active) {
      setStatus(
        (active.error && active.error.message) || active.label,
        true,
        "operation:" + active.operation_id
      );
    } else {
      var local = newestVisibleLocalSubmission();
      if (local) {
        setStatus(
          local.label,
          true,
          "operation-submission:" + local.request_generation
        );
      } else setStatus("");
    }
  }

  function settleHydrationStatus(requestGeneration) {
    var owners = ["operation-submission:" + requestGeneration];
    Object.keys(operationStatusById).forEach(function (operationId) {
      if (operationStatusById[operationId].request_generation === requestGeneration) {
        owners.push("operation:" + operationId);
      }
    });
    finishOperationSubmission(requestGeneration);
    restoreActiveStatusOrClear(owners);
  }

  /** Retain/render one canonical operation projection. */
  function reduceOperationStatus(frame) {
    var flags = {
      accepted: [false, false], validating: [false, false],
      persisting: [false, false], running: [false, false],
      completed: [true, false], failed: [true, false],
      cancelled: [true, false], retryable: [true, true],
    };
    var expected = flags[frame.state];
    var errorCodes = {
      invalid_input: true, validation_failed: true, provider_unavailable: true,
      network_unavailable: true, deadline_exceeded: true, capacity_exceeded: true,
      queue_wait_expired: true, registration_timeout: true, disconnected: true,
      cancelled_by_user: true, operation_failed: true, conflict: true,
      incompatible_runtime: true, agent_offline: true, stale_generation: true,
    };
    var keys = ["action", "chat_id", "connection_generation", "error", "label", "operation_id",
      "phase", "request_generation", "retry_after_ms", "retryable", "sequence", "state",
      "surface", "terminal", "type", "updated_at"];
    var actualKeys = Object.keys(frame).sort();
    var terminalError = ["failed", "cancelled", "retryable"].indexOf(frame.state) !== -1;
    var validError = terminalError
      ? !!(frame.error && Object.keys(frame.error).sort().join(",") === "code,message"
        && errorCodes[frame.error.code] && typeof frame.error.message === "string" && frame.error.message)
      : frame.error === null;
    var validRetryAfter = frame.retry_after_ms === null
      || (frame.state === "retryable" && Number.isSafeInteger(frame.retry_after_ms) && frame.retry_after_ms >= 0);
    if (actualKeys.join(",") !== keys.sort().join(",")
        || !scopedStatusMatches(frame) || !isCanonicalUuid4(frame.operation_id)
        || !isCanonicalUuid4(frame.connection_generation) || !isCanonicalUuid4(frame.request_generation)
        || (frame.chat_id !== null && !isCanonicalUuid4(frame.chat_id))
        || !Number.isSafeInteger(frame.sequence) || frame.sequence < 0
        || !expected || frame.terminal !== expected[0]
        || frame.retryable !== expected[1]
        || typeof frame.action !== "string" || !/^[a-z][a-z0-9_]*$/.test(frame.action)
        || typeof frame.surface !== "string" || !/^[a-z][a-z0-9_]*$/.test(frame.surface)
        || typeof frame.phase !== "string" || !/^[a-z][a-z0-9_]*$/.test(frame.phase)
        || typeof frame.label !== "string" || !frame.label
        || !validError || !validRetryAfter || !isRfc3339Utc(frame.updated_at)) return false;
    var current = operationStatusById[frame.operation_id];
    if (current && (current.terminal || frame.sequence <= current.sequence)) return false;
    operationStatusById[frame.operation_id] = frame;
    var visible = (frame.error && frame.error.message) || frame.label;
    var operationOwner = "operation:" + frame.operation_id;
    var submissionOwner = "operation-submission:" + frame.request_generation;
    var localSubmission = operationSubmissionByGeneration[frame.request_generation];
    if (frame.terminal) finishOperationSubmission(frame.request_generation);
    if (frame.state === "completed") {
      // Completion is reconciliation state, not user-facing progress. Clear it
      // only if this operation (or its local submission) still owns the line.
      // A concurrent operation or unrelated notice must remain visible.
      retractFailedTurnNotice(frame.request_generation);
      restoreActiveStatusOrClear([operationOwner, submissionOwner]);
    } else if (frame.terminal) {
      // Failure/cancellation/retry guidance persists, but is settled and must
      // never look like work is still in progress.
      setStatus(visible, false, "operation-error:" + frame.operation_id);
    } else if (operationStatusShowsActivity(frame)
        && (!localSubmission || localSubmission.shows_status !== false)
        && !(statusOwner && statusOwner.indexOf("operation-error:") === 0)) {
      setStatus(visible, true, operationOwner);
    }
    if (frame.terminal && ["failed", "cancelled", "retryable"].indexOf(frame.state) !== -1) {
      if (frame.state !== "cancelled") surfaceFailedTurn(frame, localSubmission);
      clearTransientOverlay();
    }
    return true;
  }

  /** Correlate an admission refusal without inventing a server operation. */
  function reduceAdmissionRefusal(frame) {
    var keys = ["accepted", "code", "message", "retry_after_ms", "retryable", "submission_id", "type"];
    var codes = {
      capacity_exceeded: true, registration_required: true,
      registration_timeout: true, idempotency_conflict: true,
      connection_closing: true, service_draining: true,
      invalid_input: true, registration_queue_full: true,
      operation_failed: true,
    };
    var validRetryAfter = frame.retry_after_ms === null
      || (frame.retryable === true
        && Number.isSafeInteger(frame.retry_after_ms)
        && frame.retry_after_ms >= 0);
    if (Object.keys(frame).sort().join(",") !== keys.join(",")
        || frame.type !== "error"
        || frame.accepted !== false
        || !isCanonicalUuid4(frame.submission_id)
        || !Object.prototype.hasOwnProperty.call(codes, frame.code)
        || typeof frame.message !== "string"
        || !frame.message.trim()
        || typeof frame.retryable !== "boolean"
        || !validRetryAfter) return false;
    var local = operationSubmissionById[frame.submission_id];
    if (!local) return false;
    finishOperationSubmission(local.request_generation);
    setStatus(errorMessage(frame), false, "operation-error:" + frame.submission_id);
    return true;
  }

  /** Update any open agent surface, with the shared label as a fallback. */
  function renderAgentLifecycle(frame) {
    var matched = false;
    var nodes = document.querySelectorAll("[data-agent-id]");
    for (var index = 0; index < nodes.length; index++) {
      var node = nodes[index];
      if (node.getAttribute("data-agent-id") !== frame.agent_id) continue;
      matched = true;
      node.setAttribute("data-lifecycle-state", frame.state);
      var badge = node.querySelector("[data-agent-lifecycle]");
      if (!badge) {
        badge = document.createElement("span");
        badge.setAttribute("data-agent-lifecycle", "");
        badge.className = "text-xs text-astral-muted";
        node.insertBefore(badge, node.firstChild);
      }
      badge.setAttribute("role", "status");
      badge.setAttribute("aria-label", frame.agent_id + " lifecycle status");
      badge.setAttribute("aria-live", "polite");
      badge.setAttribute("aria-atomic", "true");
      badge.setAttribute(
        "aria-busy", frame.state === "starting" || frame.state === "updating" ? "true" : "false");
      badge.textContent = frame.label;
    }
    if (!matched) {
      setStatus(frame.label);
      showToast(frame.label, frame.state === "failed" ? "error" : "info");
    }
  }

  /** Retain/render one lexicographically newer canonical lifecycle pair. */
  function reduceAgentLifecycle(frame) {
    var states = { starting: true, online: true, updating: true, failed: true, offline: true };
    var reasonCodes = {
      invalid_host_registration: true, runtime_contract_unsupported: true,
      runtime_lock_mismatch: true, bundle_digest_mismatch: true, bundle_install_failed: true,
      child_start_failed: true, child_registration_timeout: true, child_exited: true,
      child_hung: true, host_lost: true, agent_offline: true, agent_deleted: true,
      stale_runtime_generation: true, revision_promotion_failed: true,
      inventory_required: true, process_cleanup_timeout: true,
    };
    var keys = ["agent_id", "label", "lifecycle_generation", "reason_code", "revision_id",
      "runtime_instance_id", "state", "state_revision", "type", "updated_at"];
    var active = frame.state === "starting" || frame.state === "online" || frame.state === "updating";
    if (Object.keys(frame).sort().join(",") !== keys.sort().join(",")
        || typeof frame.agent_id !== "string" || !frame.agent_id
        || !states[frame.state] || typeof frame.label !== "string" || !frame.label
        || (frame.revision_id !== null && !isCanonicalUuid4(frame.revision_id))
        || (frame.runtime_instance_id !== null && !isCanonicalUuid4(frame.runtime_instance_id))
        || (active && (!isCanonicalUuid4(frame.revision_id) || !isCanonicalUuid4(frame.runtime_instance_id)))
        || (frame.reason_code !== null && !reasonCodes[frame.reason_code])
        || !isRfc3339Utc(frame.updated_at)
        || !Number.isSafeInteger(frame.lifecycle_generation) || frame.lifecycle_generation < 0
        || !Number.isSafeInteger(frame.state_revision) || frame.state_revision < 0) return false;
    var current = agentLifecycleById[frame.agent_id];
    if (current && (frame.lifecycle_generation < current.lifecycle_generation
        || (frame.lifecycle_generation === current.lifecycle_generation
          && frame.state_revision <= current.state_revision))) return false;
    agentLifecycleById[frame.agent_id] = frame;
    renderAgentLifecycle(frame);
    return true;
  }

  function appendTransientChatBubble(role, htmlStr) {
    appendChatBubbleTo(ensureTransientOverlay().chat, role, htmlStr);
  }

  // ---- incoming messages ----
  function onMessage(ev) {
    var data; try { data = JSON.parse(ev.data); } catch (e) { return; }
    if (data.type === "voice_transcript"
        && new TextEncoder().encode(ev.data).length > 12 * 1024) return;
    switch (data.type) {
      case "voice_control_binding":
        consumeVoiceControlBinding(data);
        break;
      case "composer_state":
        consumeComposerState(data);
        break;
      case "voice_session_state":
        consumeVoiceSessionState(data);
        break;
      case "voice_turn_state":
        consumeVoiceTurnState(data);
        break;
      case "voice_transcript":
        consumeVoiceTranscript(data, null);
        break;
      case "user_message_acked":
        if (!consumeVoiceMessageAcknowledged(data)
            && isCanonicalUuid4(data.request_generation)) {
          finishOperationSubmission(data.request_generation);
        }
        break;
      case "voice_submission_rejected":
        consumeVoiceSubmissionRejected(data);
        break;
      case "conversation_commit_ready":
        acceptConversationCommitReady(data);
        break;
      case "conversation_snapshot":
        reduceConversationSnapshot(data);
        break;
      case "ui_render":
        if (data.target === "history") { var hr = document.getElementById("astral-history"); if (hr) setHTML(hr, data.html); }
        else if (activeChatId) reduceTransientFrame(data);
        else if (data.target === "chat") appendChatBubble("assistant", data.html);
        else {
          hideSkeleton(); setHTML(canvas, data.html);
          // Emptiness comes from the STRUCTURED payload: render_workspace
          // emits a truthy wrapper div even for zero components (055), so
          // html truthiness only decides frames without a components array.
          if (Array.isArray(data.components) ? !data.components.length : !data.html) showCanvasEmpty();
          readCanvasFlags(); syncCanvasToolbar();
        }
        break;
      case "ui_upsert":
        if (activeChatId) reduceTransientFrame(data);
        else applyUpsert(data);
        break; // in-place workspace updates
      case "ui_update":
        if (activeChatId) reduceTransientFrame(data);
        else {
          hideSkeleton(); setHTML(canvas, data.html); if (!data.html) showCanvasEmpty();
          readCanvasFlags(); syncCanvasToolbar();
        }
        break;
      case "ui_append":
        if (activeChatId) reduceTransientFrame(data);
        else { hideSkeleton(); hideCanvasEmpty(); appendHTML(canvas, data.html); }
        break;
      case "workspace_timeline_mode": // read-only history view
        timelineMode = !!data.active;
        if (timelineMode) hideSkeleton();
        setStatus(timelineMode ? "Viewing workspace history (read-only)" : "");
        syncCanvasToolbar(); // export/share chrome hides in the read-only view
        break;
      case "chat_deleted": // chat removed (possibly from another tab)
        if (data.chat_id && data.chat_id === activeChatId) {
          var deletedVoiceFence = currentVoiceFence();
          voiceRecoverySuppressed = true;
          teardownVoiceMedia(true);
          bestEffortEndVoice(deletedVoiceFence);
          setVoiceFeedback("ended", "chat_context_unavailable", "Voice ended because this chat is no longer available.", true);
          clearActiveChatLocator("confirmed_deletion", data.chat_id);
          activeChatId = null; timelineMode = false;
          setHTML(canvas, "");
          showCanvasEmpty();
          setStatus("This chat was deleted.");
        }
        break;
      case "auth_required": // recoverable WS auth failure
        if (currentVoiceFence() || voiceActivation) {
          voiceRecoverySuppressed = true;
          teardownVoiceMedia(true);
          clearPendingVoiceSubmissions();
          clearVoiceBindingRenewal();
          voiceBinding = null;
          setVoiceFeedback("ended", "auth_expired", null, true);
        }
        if (!authRetried) {
          authRetried = true;
          refreshToken(true, function (ok) {
            if (ok && ws && ws.readyState === 1) {
              // sendRegistration emits {type: "register_ui", token: token}
              // after re-binding the locator and a fresh hydration request.
              sendRegistration(true);
            } else if (ok) { try { ws.close(); } catch (e) {} }
          });
        } else { gotoLogin(); }
        break;
      case "ui_stream_data": {
        if (activeChatId) { reduceTransientFrame(data); break; }
        if (data.session_id && activeChatId && data.session_id !== activeChatId) return;
        var last = streamSeq[data.stream_id]; if (last == null) last = -1;
        if (data.seq <= last) return; streamSeq[data.stream_id] = data.seq;
        mergeStream(data);
        if (data.terminal) delete streamSeq[data.stream_id];
        break;
      }
      case "stream_subscribed": {
        // component_id-bridged streams get a keyed placeholder (wire-contract
        // §2) so the first frame and the terminal persist upsert replace it
        // in place; legacy subscriptions need no node until data arrives.
        if (!data.component_id) break;
        if (data.session_id && activeChatId && data.session_id !== activeChatId) break;
        if (!scopedStatusMatches(data)) break;
        var subscriptionCanvas = activeChatId ? ensureTransientOverlay().canvas : canvas;
        if (subscriptionCanvas.querySelector(componentSelector(data.component_id))) break;
        hideSkeleton(); if (!activeChatId) hideCanvasEmpty();
        var ph = document.createElement("div");
        ph.setAttribute("data-component-id", data.component_id);
        ph.innerHTML = '<div class="astral-skeleton" role="status" aria-busy="true">'
          + '<span class="sr-only">Loading…</span>'
          + '<div class="astral-skeleton-line h-20 w-full"></div></div>';
        if (activeChatId) subscriptionCanvas.appendChild(ph);
        else ensureRenderer().appendChild(ph);
        break;
      }
      case "chrome_render": // server-rendered chrome regions
        if (data.region === "modal") setModal(data.html || "");
        else if (data.region === "topbar") {
          var tb = document.getElementById("astral-topbar");
          if (tb) {
            tb.innerHTML = data.html || "";
            statusEl = configureStatusElement(document.getElementById("astral-status"));
          }
        }
        break;
      case "chat_status":
        if (!scopedStatusMatches(data)) break;
        var chatStatusOwner = data.request_generation
          ? "operation-submission:" + data.request_generation
          : "chat-status";
        // A turn that ends with no canvas output (text-only answer, error,
        // cancellation) must still clear the query-start skeleton.
        if (data.status === "done" || data.status === "idle") {
          hideSkeleton();
          clearTransientOverlay();
          // The welcome was dropped when the skeleton started; if the turn produced
          // no canvas component at all, restore it so the canvas isn't left blank.
          if (canvas && !canvas.querySelector('[data-component-id], .astral-component')) showCanvasEmpty();
        }
        if (data.status === "processing_async") {
          // Background dispatch ack (055): status text only — never the turn
          // lock (no skeleton), so the user can keep chatting or switch chats.
          hideSkeleton();
          setStatus(
            "Running in background…",
            true,
            chatStatusOwner
          );
          break;
        }
        var chatStatusLabel = { idle: "", thinking: "Thinking…", executing: "Working…", done: "" }[data.status] || "";
        if (chatStatusLabel) {
          setStatus(
            chatStatusLabel,
            true,
            chatStatusOwner
          );
        } else restoreActiveStatusOrClear([chatStatusOwner]);
        break;
      case "chat_step":
        if (scopedStatusMatches(data)) renderStep(data.step);
        break;
      case "chat_created":
        if (correlatedVoiceChatCreated(data)) break;
        if (data.payload && isCanonicalUuid4(data.payload.chat_id)) {
          persistActiveChatLocator(data.payload.chat_id);
          activeChatId = data.payload.chat_id;
          syncVoiceVisibleChat(activeChatId);
          if (requestState && !requestState.chatId) requestState.chatId = activeChatId;
        }
        break;
      case "chat_loaded":
        // Bounded compatibility acknowledgement only. Feature-060 clients do
        // not clear/replace either committed surface from the legacy two-frame
        // chat_loaded + ui_render pair; the atomic snapshot must follow.
        if (data.chat && isCanonicalUuid4(data.chat.id)) {
          if (!activeChatId) selectActiveChat(data.chat.id, "hydration");
          // A compatibility ack may race behind the authoritative snapshot.
          // Never resurrect hydration progress once that snapshot committed.
          if (data.chat.id === activeChatId && requestState && !requestState.snapshotApplied) {
            setStatus(
              "Restoring conversation…",
              true,
              "operation-submission:" + requestState.generation
            );
          }
        }
        break;
      case "user_preferences":
        if (data.preferences && data.preferences.theme) applyTheme(data.preferences.theme);
        break;
      case "error": { // feature 044 FR-002 — server error replies are never silent
        if (!scopedStatusMatches(data)) break;
        var admissionRefusal = reduceAdmissionRefusal(data);
        var em = errorMessage(data);
        showToast(em, "error");
        hideSkeleton(); // the turn is over; no components are coming
        clearTransientOverlay();
        if (["chat_not_found", "chat_deleted", "not_found"].indexOf(data.code) !== -1
            && data.chat_id === activeChatId) clearActiveChatLocator("confirmed_deletion", data.chat_id);
        if (!admissionRefusal) setStatus(""); // resolve any stuck "Thinking…" state (SC-006)
        break;
      }
      case "notification": // scheduler push (feature 044 parity matrix)
        showToast((data.title ? data.title + ": " : "") + (data.body || ""), data.level === "error" ? "error" : "info");
        break;
      case "task_started": { // 055: background dispatch accepted (any device)
        var tsp = data.payload || {};
        addTaskChip(tsp.task_id, tsp.chat_id, tsp.title);
        showToast("Running in background — you will be notified when it finishes.", "info");
        break;
      }
      case "task_completed": { // 055: background task finished (any device)
        var tcp = data.payload || {};
        if (tcp.task_id) {
          if (bgTaskDone[tcp.task_id]) break; // watcher + fan-out duplicate
          bgTaskDone[tcp.task_id] = true;
          removeTaskChip(tcp.task_id);
        }
        var tcFail = tcp.status === "failed";
        var tcMsg = tcp.summary || ("Background task " + (tcp.status || "completed"));
        if (tcp.chat_id && tcp.chat_id === activeChatId) {
          showToast(tcMsg, tcFail ? "error" : "info");
          // Pull the narrative/canvas the task persisted while detached.
          loadActiveChat(tcp.chat_id);
        } else if (tcp.chat_id) {
          showToast(tcMsg + " — tap to open", tcFail ? "error" : "info", function () {
            loadActiveChat(tcp.chat_id); // recents-click path
            closeHistoryOverlay();
          });
        } else {
          showToast(tcMsg, tcFail ? "error" : "info");
        }
        break;
      }
      case "tool_progress": { // long-running job update (fan-out is chat-scoped)
        if (!scopedStatusMatches(data)) break;
        var tpChat = data.session_id || data.chat_id;
        if (tpChat && activeChatId && tpChat !== activeChatId) break;
        if (data.terminal) { setStatus(""); break; } // outcome lands as a persisted upsert
        var tpText = data.message || ((data.tool_name || "job") + " running…");
        if (typeof data.percentage === "number") tpText += " (" + Math.round(data.percentage) + "%)";
        setStatus(tpText);
        break;
      }
      case "operation_status":
        reduceOperationStatus(data);
        break;
      case "agent_lifecycle":
        reduceAgentLifecycle(data);
        break;
      case "rote_config": // ROTE's device verdict drives the shell layout
        applyDeviceProfile(data.device_profile && data.device_profile.device_type);
        // 066: the post-registration verdict marks the socket healthy — flush
        // queued sends and retire the connection pill.
        if (!socketReady) {
          socketReady = true;
          setConnState("connected");
          flushPendingActions();
        }
        break;
      case "agent_host_inventory_reconciled": case "agent_host_registration_refused":
      case "agent_host_registered": // host-only; the browser is author-only
      case "system_config": case "agent_list": case "agent_registered":
      case "history_list": case "heartbeat": case "llm_config_ack": case "saved_components_list":
        break; // not needed for the core flow
      default: break;
    }
  }

  // Normalize the three historical error-frame shapes (see
  // backend/shared/ui_protocol.json): {code,message} | {payload:{message}} | {message}.
  function errorMessage(data) {
    var m = data.message || (data.payload && data.payload.message) || "Something went wrong.";
    return data.code && data.code !== "internal" ? m + " (" + data.code + ")" : m;
  }

  var toastHost = null;
  /** onTap (optional) makes the toast a tap-to-open affordance (055
   *  background completions); tappable toasts linger longer. */
  function showToast(message, kind, onTap) {
    if (!message) return;
    if (!toastHost) {
      toastHost = document.createElement("div");
      toastHost.id = "astral-toasts";
      toastHost.setAttribute("role", "status");
      toastHost.style.cssText = "position:fixed;bottom:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;max-width:360px;";
      document.body.appendChild(toastHost);
    }
    var t = document.createElement("div");
    t.className = "astral-toast astral-toast-" + (kind || "info");
    t.style.cssText = "padding:10px 14px;border-radius:8px;font-size:13px;color:#fff;box-shadow:0 4px 14px rgba(0,0,0,.4);"
      + (kind === "error" ? "background:#7f1d1d;border:1px solid #b91c1c;" : "background:#1e293b;border:1px solid #334155;");
    t.textContent = message;
    if (onTap) {
      t.style.cursor = "pointer";
      t.setAttribute("role", "button");
      t.tabIndex = 0;
      var fire = function () { if (t.parentNode) t.parentNode.removeChild(t); onTap(); };
      t.addEventListener("click", fire);
      t.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fire(); }
      });
    }
    toastHost.appendChild(t);
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, onTap ? 12000 : 6000);
  }

  function escapeText(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }

  // Render attachment(s) as a pill on its own line above the request text (a
  // plain "📎 name" prefix collapses onto the query line because chat bubbles
  // don't preserve newlines).
  function attachChipHtml(names) {
    return "<div class=\"mb-1\"><span class=\"inline-flex items-center gap-1 rounded "
      + "bg-white/10 border border-white/10 px-2 py-0.5 text-xs\">📎 "
      + escapeText(names) + "</span></div>";
  }

  var stepEls = {};
  function renderStep(step) {
    if (!step) return;
    var el = stepEls[step.id];
    if (!el) {
      el = document.createElement("div");
      el.className = "text-xs text-astral-muted/70 px-2 py-1";
      var stepHost = requestState ? ensureTransientOverlay().chat : chat;
      stepHost.appendChild(el); stepEls[step.id] = el;
    }
    var icon = step.status === "completed" ? "✓" : step.status === "errored" ? "✗" : "•";
    // Chat shows only the tool/step name; result summaries stay in the
    // persisted step record (chat-steps API / audit), not the transcript.
    el.textContent = icon + " " + (step.name || step.kind || "step");
    chat.scrollTop = chat.scrollHeight;
  }

  // ---- outgoing: chat + delegated component actions ----
  // A message may carry staged attachments (see the attachment block lower
  // down). readyAttachments()/clearStagedAttachments() are declared there;
  // function/var hoisting makes them available here at call time.
  function sendChat(message) {
    var ready = (typeof readyAttachments === "function") ? readyAttachments() : [];
    if (!message && !ready.length) return;
    if (!isSocketReady()) {
      // 066: never silently drop a send — queue it visibly and dispatch on
      // registration, or refuse loudly with the text preserved.
      queueChatSend(message, ready);
      if (typeof clearStagedAttachments === "function") clearStagedAttachments();
      return;
    }
    doSendChat(message, ready);
    if (typeof clearStagedAttachments === "function") clearStagedAttachments();
  }

  function queueChatSend(message, ready) {
    var html = "";
    if (ready.length) {
      var names = ready.map(function (a) { return a.filename; }).join(", ");
      html += attachChipHtml(names);
    }
    if (message) html += "<div>" + escapeText(message) + "</div>";
    var wrap = document.createElement("div");
    wrap.className = "flex justify-end astral-bubble-queued";
    var bubble = document.createElement("div");
    bubble.className = "bg-astral-primary/20 border border-astral-primary/30 rounded-lg p-3 max-w-[85%] text-sm text-astral-text";
    bubble.innerHTML = html;
    wrap.appendChild(bubble);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
    var accepted = queueOutboundAction({
      label: "chat_message",
      dispatch: function () {
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
        doSendChat(message, ready);
      },
      onRefused: function () {
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
        appendFailedTurnNotice("Still offline — your message was not sent.", message);
        if (input && !input.value) input.value = message || "";
      },
    });
    if (!accepted && wrap.parentNode) {
      wrap.parentNode.removeChild(wrap);
      if (input && !input.value) input.value = message || "";
    }
  }

  function doSendChat(message, ready) {
    openRequest("commit", activeChatId);
    var html = "";
    if (ready.length) {
      var names = ready.map(function (a) { return a.filename; }).join(", ");
      html += attachChipHtml(names);  // pill on its own line above the request
    }
    if (message) html += "<div>" + escapeText(message) + "</div>";
    appendTransientChatBubble("user", html);
    var payload = {
      message: message || "",
      chat_id: activeChatId,
      connection_generation: connectionGeneration,
      request_generation: requestState.generation,
      snapshot_purpose: "commit",
    };
    if (ready.length) {
      payload.attachments = ready.map(function (a) {
        return { attachment_id: a.attachment_id, filename: a.filename, category: a.category };
      });
    }
    if (bgArmed) payload.async_mode = true; // one-shot background-run arming (055)
    var submission = beginOperationSubmission("chat_message", payload, requestState.generation);
    send({
      type: "ui_event",
      action: "chat_message",
      session_id: activeChatId || undefined,
      connection_generation: connectionGeneration,
      submission_id: submission.submissionId,
      request_generation: submission.requestGeneration,
      payload: submission.payload,
    });
    purgeWelcome(); // 055 uniform rule: welcome never survives the first send
    // Async turns never lock the composer: no skeleton — the processing_async
    // ack drives the status line instead.
    if (bgArmed) setBgArmed(false);
    else showSkeleton(); // optimistic loading state until the first canvas content
  }

  if (form) form.addEventListener("submit", function (e) {
    e.preventDefault();
    var v = input.value.trim();
    var hasReady = (typeof readyAttachments === "function") && readyAttachments().length;
    if (!v && !hasReady) return;
    input.value = "";
    sendChat(v);
  });

  // ---- new chat (topbar button) — the web twin of the native clients' ＋ New:
  // clear the local conversation state, then ask the server for a fresh chat
  // (it replies chat_created, which sets activeChatId).
  var newChatBtn = document.getElementById("astral-newchat-btn");
  if (newChatBtn) newChatBtn.addEventListener("click", function () {
    if (voiceActivation) {
      teardownVoiceMedia(false);
      setVoiceFeedback("off", "ready", null, false);
    } else if (currentVoiceFence()) {
      pauseVoiceCaptureForChatTransition();
      setVoiceFeedback("connecting", "chat_context_unavailable", "Creating the new voice chat context…", true);
    }
    clearActiveChatLocator("explicit_new_chat", activeChatId);
    activeChatId = null;
    timelineMode = false;
    streamSeq = {};
    streamChartPlot = {};
    stepEls = {};
    hideSkeleton();
    chat.innerHTML = "";
    canvas.innerHTML = "";
    showCanvasEmpty();
    setStatus("");
    action("new_chat", {});
    closeHistoryOverlay();
    if (input) { try { input.focus(); } catch (e) {} }
  });

  // The local endpoint invalidates the server session before redirecting to
  // Keycloak, so a deliberate click is the web client's definitive sign-out
  // event. Token refresh/auth_required never traverses this path.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest('a[href^="/auth/logout"]');
    if (link) {
      var voiceFence = currentVoiceFence();
      voiceRecoverySuppressed = true;
      clearVoiceBindingRenewal();
      teardownVoiceMedia(true);
      clearPendingVoiceSubmissions();
      bestEffortEndVoice(voiceFence);
      // Clear credentials before navigation. A Keycloak end-session redirect
      // leaves this tab's sessionStorage alive, so retaining TOKEN_KEY could
      // register the next account's WebSocket as the previous principal.
      token = "";
      try {
        sessionStorage.removeItem(TOKEN_KEY);
        sessionStorage.removeItem(ACCOUNT_SESSION_KEY);
      } catch (e) {}
      clearActiveChatLocator("definitive_sign_out", activeChatId);
    }
  }, true);

  // ---- stacked-shell chrome: the web twin of Android's StackedShell.
  // Recent chats live behind the topbar speech-bubble button (full-screen
  // overlay of the same server-rendered #astral-history region), and the
  // transcript collapses behind a "Messages (N)" bar above the input.
  // Split layouts never see these controls — astral.css gates them on
  // body[data-astral-layout="stacked"].
  function closeHistoryOverlay() {
    document.body.classList.remove("astral-history-open");
    if (chatsBtn) chatsBtn.setAttribute("aria-expanded", "false");
  }
  var chatsBtn = document.getElementById("astral-chats-btn");
  if (chatsBtn) chatsBtn.addEventListener("click", function () {
    var topbar = document.getElementById("astral-topbar");
    if (topbar) document.documentElement.style.setProperty("--astral-topbar-h", topbar.offsetHeight + "px");
    var open = document.body.classList.toggle("astral-history-open");
    chatsBtn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  var msgsToggle = document.getElementById("astral-msgs-toggle");
  var msgsLabel = document.getElementById("astral-msgs-label");
  if (msgsToggle) msgsToggle.addEventListener("click", function () {
    var open = document.body.classList.toggle("astral-msgs-open");
    msgsToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open && chat) chat.scrollTop = chat.scrollHeight;
  });
  function syncMsgsToggle() {
    if (!msgsToggle || !chat) return;
    var n = chat.children.length;
    msgsToggle.hidden = n === 0;
    if (n === 0) document.body.classList.remove("astral-msgs-open");
    if (msgsLabel) msgsLabel.textContent = n ? "Messages (" + n + ")" : "Messages";
  }
  if (window.MutationObserver && chat) new MutationObserver(syncMsgsToggle).observe(chat, { childList: true });
  syncMsgsToggle();

  // Delegated handlers for server-rendered interactive primitives
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".astral-action");
    if (btn) {
      var act = btn.getAttribute("data-action"); var payload = {};
      try { payload = JSON.parse(btn.getAttribute("data-payload") || "{}"); } catch (_) {}
      // Actions emitted inside a workspace component carry its identity;
      // historical views are inert except chrome actions.
      var compHost = btn.closest && btn.closest("[data-component-id]");
      if (compHost && !payload.component_id) payload.component_id = compHost.getAttribute("data-component-id");
      if (!payload.chat_id && activeChatId) payload.chat_id = activeChatId;
      if (timelineMode && compHost && act && act.indexOf("chrome_") !== 0) {
        setStatus("Read-only history view — go back to live to interact.");
        return;
      }
      // A chat_message action (e.g. the welcome examples' buttons) is exactly
      // a typed message — present it the same way: user bubble + the standard
      // chat payload shape.
      if (act === "chat_message" && payload.message) { sendChat(payload.message); return; }
      if (act === "chrome_open") showModalSkeleton(act, payload);
      if (act === "load_chat" && payload.chat_id) {
        loadActiveChat(payload.chat_id);
        closeHistoryOverlay();
        return;
      }
      if (act) action(act, payload);
      if (act === "load_chat") closeHistoryOverlay(); // mobile: leave the full-screen list
      return;
    }
    // param_picker toggle buttons (checklist)
    var chip = e.target.closest && e.target.closest(".astral-pp-field[data-kind='checklist']");
    if (chip) { var on = chip.getAttribute("aria-pressed") === "true"; chip.setAttribute("aria-pressed", on ? "false" : "true");
      chip.classList.toggle("bg-astral-primary/30"); chip.classList.toggle("border-astral-primary"); chip.classList.toggle("text-white"); return; }
    // param_picker submit
    var sub = e.target.closest && e.target.closest(".astral-pp-submit");
    if (sub) { submitParamPicker(sub.closest(".astral-param-picker")); return; }
    // table pagination
    var pgPrev = e.target.closest && e.target.closest(".astral-page-prev");
    var pgNext = e.target.closest && e.target.closest(".astral-page-next");
    if (pgPrev || pgNext) { paginate(e.target.closest(".astral-pagination"), pgNext ? 1 : -1); return; }
  });
  document.addEventListener("change", function (e) {
    if (e.target.classList && e.target.classList.contains("astral-page-size")) {
      paginateSize(e.target.closest(".astral-pagination"), parseInt(e.target.value, 10));
    }
    if (e.target.classList && e.target.classList.contains("astral-color-picker")) {
      var key = e.target.getAttribute("data-color-key"); setColor(key, e.target.value);
      action("save_theme", { theme: { color_key: key, color_value: e.target.value } });
    }
  });

  function collectFields(form) {
    var state = {};
    form.querySelectorAll(".astral-pp-field").forEach(function (f) {
      var name = f.getAttribute("data-field"), kind = f.getAttribute("data-kind");
      if (!name) return;
      if (kind === "boolean") state[name] = f.checked;
      else if (kind === "number") state[name] = f.value === "" ? null : Number(f.value);
      else if (kind === "checklist") { state[name] = state[name] || []; if (f.getAttribute("aria-pressed") === "true") state[name].push(f.getAttribute("data-value")); }
      else state[name] = f.value;
    });
    return state;
  }
  function submitParamPicker(form) {
    if (!form) return;
    var template = form.getAttribute("data-template") || "";
    var state = collectFields(form);
    var msg = template.replace("{__values_json__}", JSON.stringify(state, null, 2));
    msg = msg.replace(/\{(\w+)\}/g, function (m, k) {
      if (!(k in state)) return m; var v = state[k];
      return typeof v === "string" ? v : JSON.stringify(v);
    });
    sendChat(msg);
  }
  // Pagination carries the table's component identity so the server updates
  // ONLY that table in place via the standardized component_action pipeline.
  function paginateComponentId(el) {
    var host = el && el.closest && el.closest("[data-component-id]");
    return host ? host.getAttribute("data-component-id") : null;
  }
  function paginate(el, dir) {
    if (!el) return; var ctx; try { ctx = JSON.parse(el.getAttribute("data-ctx") || "{}"); } catch (e) { return; }
    if (timelineMode) { setStatus("Read-only history view — go back to live to interact."); return; }
    var size = ctx.page_size, off = Math.max(0, (ctx.page_offset || 0) + dir * size);
    action("table_paginate", { tool_name: ctx.source_tool, agent_id: ctx.source_agent,
      component_id: paginateComponentId(el), chat_id: activeChatId,
      params: Object.assign({}, ctx.source_params, { limit: size, offset: off }) });
  }
  function paginateSize(el, size) {
    if (!el) return; var ctx; try { ctx = JSON.parse(el.getAttribute("data-ctx") || "{}"); } catch (e) { return; }
    if (timelineMode) { setStatus("Read-only history view — go back to live to interact."); return; }
    action("table_paginate", { tool_name: ctx.source_tool, agent_id: ctx.source_agent,
      component_id: paginateComponentId(el), chat_id: activeChatId,
      params: Object.assign({}, ctx.source_params, { limit: size, offset: 0 }) });
  }

  // ---- 055 US4/US5: component chrome (refine / history / export / share) ----
  // The server renders the affordances (flag-gated, renderer.py
  // _component_chrome); this block owns their click behavior. The instruction
  // capture is an inline popover (same idiom as the paperclip menu — the
  // codebase never uses window.prompt/alert).
  var chromePop = null;
  function closeChromePop() {
    if (chromePop && chromePop.parentNode) chromePop.parentNode.removeChild(chromePop);
    chromePop = null;
  }
  function openChromePop(anchor) {
    closeChromePop();
    var row = anchor.parentNode; // the .astral-component-chrome affordance row
    if (row && !row.style.position) row.style.position = "relative";
    chromePop = document.createElement("div");
    chromePop.className = "astral-chrome-pop";
    chromePop.style.cssText = "position:absolute;right:0;bottom:100%;margin-bottom:6px;z-index:40;"
      + "min-width:260px;max-width:340px;padding:10px;border-radius:10px;"
      + "background:rgb(var(--astral-surface,26 30 46));border:1px solid rgba(255,255,255,.12);"
      + "box-shadow:0 8px 24px rgba(0,0,0,.45);font-size:13px;";
    (row || document.body).appendChild(chromePop);
    return chromePop;
  }
  function chromePopButton(text, primary) {
    var b = document.createElement("button");
    b.type = "button";
    b.textContent = text;
    b.style.cssText = "font-size:12px;border-radius:8px;padding:4px 10px;cursor:pointer;"
      + (primary ? "background:rgb(var(--astral-primary,99 102 241));border:0;color:#fff;"
                 : "background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);color:inherit;");
    return b;
  }
  function chromeComponentId(el) {
    var host = el.closest && el.closest("[data-component-id]");
    return host ? host.getAttribute("data-component-id") : null;
  }

  function openRefinePrompt(btn) {
    if (timelineMode) { setStatus("Read-only history view — go back to live to interact."); return; }
    var cid = chromeComponentId(btn);
    if (!cid) return;
    var pop = openChromePop(btn);
    var label = document.createElement("div");
    label.textContent = "Describe the change to this component";
    label.style.cssText = "font-size:12px;margin-bottom:6px;opacity:.8;";
    var inp = document.createElement("input");
    inp.type = "text";
    inp.placeholder = "e.g. add a totals row";
    inp.style.cssText = "width:100%;box-sizing:border-box;font-size:13px;padding:6px 8px;border-radius:8px;"
      + "background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);color:inherit;";
    var rowEl = document.createElement("div");
    rowEl.style.cssText = "display:flex;justify-content:flex-end;gap:8px;margin-top:8px;";
    var cancel = chromePopButton("Cancel", false);
    var go = chromePopButton("Refine", true);
    function submit() {
      var text = (inp.value || "").trim();
      if (!text) { inp.focus(); return; }
      action("component_refine", { component_id: cid, instruction: text, chat_id: activeChatId });
      closeChromePop();
      showToast("Refining component…", "info");
    }
    go.addEventListener("click", submit);
    cancel.addEventListener("click", closeChromePop);
    inp.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); submit(); }
      else if (e.key === "Escape") { e.stopPropagation(); closeChromePop(); }
    });
    pop.appendChild(label); pop.appendChild(inp); pop.appendChild(rowEl);
    rowEl.appendChild(cancel); rowEl.appendChild(go);
    inp.focus();
  }

  function openHistoryList(btn) {
    if (timelineMode) { setStatus("Read-only history view — go back to live to interact."); return; }
    var cid = chromeComponentId(btn);
    if (!cid) return;
    var versions = [];
    try { versions = JSON.parse(btn.getAttribute("data-versions") || "[]"); } catch (e) {}
    var pop = openChromePop(btn);
    var label = document.createElement("div");
    label.textContent = "Version history";
    label.style.cssText = "font-size:12px;margin-bottom:6px;opacity:.8;";
    pop.appendChild(label);
    if (!versions.length) {
      var none = document.createElement("div");
      none.textContent = "No earlier versions yet — refine the component to create one.";
      none.style.cssText = "font-size:12px;opacity:.6;";
      pop.appendChild(none);
      return;
    }
    versions.forEach(function (v) {
      if (!v || v.version_no == null) return;
      var b = document.createElement("button");
      b.type = "button";
      var when = String(v.created_at || "").replace("T", " ").slice(0, 16);
      b.textContent = "v" + v.version_no
        + (v.title ? " · " + v.title : "")
        + (when ? " · " + when : "");
      b.title = "Restore this version" + (v.reason ? " (archived on " + v.reason + ")" : "");
      b.style.cssText = "display:block;width:100%;text-align:left;font-size:12px;padding:6px 8px;"
        + "border-radius:8px;background:transparent;border:0;color:inherit;cursor:pointer;";
      b.addEventListener("click", function () {
        action("component_restore", { component_id: cid, version_no: v.version_no, chat_id: activeChatId });
        closeChromePop();
        showToast("Restoring version " + v.version_no + "…", "info");
      });
      pop.appendChild(b);
    });
  }

  // Exports are authenticated downloads: fetch with the bearer token, then
  // hand the blob to a temporary <a download> (a plain href can't carry auth).
  function exportDownload(path, filename, appendChat) {
    var url = path;
    if (appendChat) {
      if (!activeChatId) { showToast("Open a chat first — nothing to export yet.", "error"); return; }
      url += (url.indexOf("?") === -1 ? "?" : "&") + "chat_id=" + encodeURIComponent(activeChatId);
    }
    fetch(API_URL + url, { headers: { Authorization: "Bearer " + token }, credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("Export failed (" + r.status + ")");
        return r.blob();
      })
      .then(function (blob) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = filename || "export";
        document.body.appendChild(a);
        a.click();
        setTimeout(function () {
          URL.revokeObjectURL(a.href);
          if (a.parentNode) a.parentNode.removeChild(a);
        }, 1000);
      })
      .catch(function (err) { showToast(String((err && err.message) || err), "error"); });
  }

  function mintShare(scope, componentId) {
    if (!activeChatId) { showToast("Open a chat first — nothing to share yet.", "error"); return; }
    var body = { chat_id: activeChatId, scope: scope };
    if (componentId) body.component_id = componentId;
    fetch(API_URL + "/api/share", {
      method: "POST",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body),
    })
      .then(function (r) {
        return r.json().then(function (j) { return { ok: r.ok, status: r.status, body: j }; });
      })
      .then(function (res) {
        if (!res.ok) {
          var msg = res.body && res.body.error === "phi_blocked"
            ? "Sharing refused: the content matched the PHI gate."
            : (res.body && (res.body.detail || res.body.error)) || ("Share failed (" + res.status + ")");
          showToast(msg, "error");
          return;
        }
        var shareUrl = res.body && res.body.share_url;
        if (!shareUrl) { showToast("Share failed: no link returned.", "error"); return; }
        var abs = shareUrl.indexOf("http") === 0 ? shareUrl : API_URL + shareUrl;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(abs).then(
            function () { showToast("Share link copied to clipboard.", "info"); },
            function () { showToast("Share link: " + abs, "info"); });
        } else { showToast("Share link: " + abs, "info"); }
      })
      .catch(function () { showToast("Couldn't create the share link.", "error"); });
  }

  // Canvas toolbar (export page / share page). The server stamps the flag
  // state as data-astral-export / data-astral-share on the .dynamic-renderer
  // root of every full canvas render (renderer.py _workspace_flag_attrs);
  // the toolbar exists only while a flagged renderer is on the canvas.
  var canvasFlags = { exp: false, share: false };
  function readCanvasFlags() {
    var r = canvas.querySelector(".dynamic-renderer");
    canvasFlags.exp = !!(r && r.getAttribute("data-astral-export"));
    canvasFlags.share = !!(r && r.getAttribute("data-astral-share"));
  }
  function syncCanvasToolbar() {
    var bar = document.getElementById("astral-canvas-toolbar");
    var want = (canvasFlags.exp || canvasFlags.share) && !timelineMode
      && !!canvas.querySelector(".dynamic-renderer");
    if (!want) {
      if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
      return;
    }
    if (bar) return; // already up
    bar = document.createElement("div");
    bar.id = "astral-canvas-toolbar"; // 066: astral.css owns the surface (solid
    // backdrop) so the sticky row never see-throughs over component content.
    if (canvasFlags.exp) bar.appendChild(chromeToolbarButton("⬇ Export page", "astral-export-canvas", null));
    if (canvasFlags.share) bar.appendChild(chromeToolbarButton("↗ Share page", "astral-share-btn", "canvas"));
    canvas.insertBefore(bar, canvas.firstChild);
  }
  function chromeToolbarButton(text, cls, shareScope) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = text;
    if (shareScope) b.setAttribute("data-share-scope", shareScope);
    b.style.cssText = "font-size:11px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);"
      + "border-radius:8px;padding:3px 10px;color:inherit;cursor:pointer;";
    return b;
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    var refine = t.closest && t.closest(".astral-refine-btn");
    if (refine) { openRefinePrompt(refine); return; }
    var hist = t.closest && t.closest(".astral-vhistory-btn");
    if (hist) { openHistoryList(hist); return; }
    var csv = t.closest && t.closest(".astral-export-csv");
    if (csv) {
      e.preventDefault();
      var cid = chromeComponentId(csv);
      exportDownload(csv.getAttribute("href"), (cid || "table") + ".csv", true);
      return;
    }
    var expCanvas = t.closest && t.closest(".astral-export-canvas");
    if (expCanvas) {
      if (!activeChatId) { showToast("Open a chat first — nothing to export yet.", "error"); return; }
      exportDownload("/api/export/canvas/" + encodeURIComponent(activeChatId) + ".html",
        "canvas-" + activeChatId + ".html", false);
      return;
    }
    var share = t.closest && t.closest(".astral-share-btn");
    if (share) {
      mintShare(share.getAttribute("data-share-scope") || "component", chromeComponentId(share));
      return;
    }
    if (chromePop && !chromePop.contains(t)) closeChromePop();
  });

  // Attachment staging: paperclip → pick → upload → chip → send as structured
  // attachments[] on the next chat_message.
  var stagedAttachments = [];   // {uid, attachment_id|null, filename, category, state, note}
  var attachSeq = 0;
  var MAX_ATTACHMENTS = 10;
  var attachEl = document.getElementById("astral-attachments");
  var attachBtn = document.getElementById("astral-attach-btn");
  var attachInput = document.getElementById("astral-attach-input");

  function readyAttachments() {
    return stagedAttachments.filter(function (a) { return a.state === "ready" && a.attachment_id; });
  }
  function clearStagedAttachments() {
    stagedAttachments = [];
    renderAttachments();
  }
  function removeStaged(uid) {
    stagedAttachments = stagedAttachments.filter(function (a) { return a.uid !== uid; });
    renderAttachments();
  }
  function renderAttachments() {
    if (!attachEl) return;
    attachEl.innerHTML = "";
    if (!stagedAttachments.length) { attachEl.classList.add("hidden"); return; }
    attachEl.classList.remove("hidden");
    stagedAttachments.forEach(function (a) {
      var chip = document.createElement("span");
      chip.className = "astral-chip is-" + a.state;
      chip.setAttribute("data-uid", String(a.uid));
      var name = document.createElement("span");
      name.className = "astral-chip-name";
      name.textContent = a.filename;
      name.title = a.note || a.filename;
      chip.appendChild(name);
      var state = document.createElement("span");
      state.className = "astral-chip-state";
      state.textContent = a.state === "uploading" ? "…" :
                          a.state === "failed" ? "failed" :
                          (a.note ? a.note : "");
      chip.appendChild(state);
      var x = document.createElement("button");
      x.type = "button";
      x.className = "astral-chip-remove";
      x.setAttribute("aria-label", "Remove " + a.filename);
      x.setAttribute("data-remove-uid", String(a.uid));
      x.textContent = "×";
      chip.appendChild(x);
      attachEl.appendChild(chip);
    });
  }

  function uploadStagedFile(file) {
    var entry = { uid: ++attachSeq, attachment_id: null, filename: file.name,
                  category: "file", state: "uploading", note: "" };
    stagedAttachments.push(entry);
    renderAttachments();
    var fd = new FormData(); fd.append("file", file);
    fetch(API_URL + "/api/upload", { method: "POST", headers: { Authorization: "Bearer " + token }, body: fd })
      .then(function (r) {
        return r.json().then(function (j) { return { ok: r.ok, status: r.status, body: j }; });
      })
      .then(function (res) {
        if (!res.ok) {
          entry.state = "failed";
          entry.note = (res.body && (res.body.detail || res.body.message)) || ("error " + res.status);
          setStatus("Couldn't attach " + file.name + ": " + entry.note);
          renderAttachments();
          return;
        }
        var j = res.body || {};
        entry.attachment_id = j.attachment_id || null;
        entry.category = j.category || "file";
        entry.state = entry.attachment_id ? "ready" : "failed";
        // Surface the eager auto-parser status (US2) on the chip.
        var ps = j.parser_status;
        if (ps === "preparing") entry.note = "preparing reader…";
        else if (ps === "pending_admin_approval") entry.note = "reader pending admin";
        else if (ps === "unavailable") entry.note = "no reader yet";
        else entry.note = "";
        if (!entry.attachment_id) entry.note = "upload failed";
        renderAttachments();
      })
      .catch(function () {
        entry.state = "failed"; entry.note = "network error";
        setStatus("Couldn't attach " + file.name);
        renderAttachments();
      });
  }

  // Paperclip → small menu: upload a new file, or choose an existing one (US3).
  var attachMenu = null;
  function closeAttachMenu() { if (attachMenu) { attachMenu.remove(); attachMenu = null; } }
  function openAttachMenu() {
    closeAttachMenu();
    attachMenu = document.createElement("div");
    attachMenu.className = "astral-attach-menu";
    [["Upload a file", function () { attachInput.click(); }],
     ["Choose from your files", function () {
       showModalSkeleton("chrome_open", { surface: "attachments" });
       action("chrome_open", { surface: "attachments" });
     }]
    ].forEach(function (pair) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "astral-attach-menu-item"; b.textContent = pair[0];
      b.addEventListener("click", function () { closeAttachMenu(); pair[1](); });
      attachMenu.appendChild(b);
    });
    (attachBtn.parentNode || document.body).appendChild(attachMenu);
  }
  if (attachBtn && attachInput) {
    attachBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (attachMenu) closeAttachMenu(); else openAttachMenu();
    });
    document.addEventListener("click", function (e) {
      if (attachMenu && !attachMenu.contains(e.target) && e.target !== attachBtn) closeAttachMenu();
    });
  }
  // Attach an EXISTING file from the library modal — stage a ready chip with no
  // re-upload, then close the modal (US3).
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".astral-attach-existing");
    if (!btn) return;
    var aid = btn.getAttribute("data-attachment-id");
    if (!aid) return;
    if (stagedAttachments.length >= MAX_ATTACHMENTS) {
      setStatus("You can attach up to " + MAX_ATTACHMENTS + " files per message."); return;
    }
    var dup = stagedAttachments.some(function (a) { return a.attachment_id === aid; });
    if (!dup) {
      stagedAttachments.push({ uid: ++attachSeq, attachment_id: aid,
        filename: btn.getAttribute("data-filename") || "file",
        category: btn.getAttribute("data-category") || "file",
        state: "ready", note: "" });
      renderAttachments();
    }
    if (typeof setModal === "function") setModal("");
  });
  // Remove-chip delegation.
  if (attachEl) {
    attachEl.addEventListener("click", function (e) {
      var rm = e.target.closest && e.target.closest("[data-remove-uid]");
      if (rm) { removeStaged(parseInt(rm.getAttribute("data-remove-uid"), 10)); }
    });
  }
  // File selection (the hidden input carries class astral-file-upload).
  document.addEventListener("change", function (e) {
    if (!(e.target.classList && e.target.classList.contains("astral-file-upload"))) return;
    var files = e.target.files ? Array.prototype.slice.call(e.target.files) : [];
    if (!files.length) return;
    var room = MAX_ATTACHMENTS - stagedAttachments.length;
    if (room <= 0) { setStatus("You can attach up to " + MAX_ATTACHMENTS + " files per message."); e.target.value = ""; return; }
    if (files.length > room) { setStatus("Only " + room + " more file(s) can be attached to this message."); files = files.slice(0, room); }
    files.forEach(uploadStagedFile);
    e.target.value = "";  // allow re-selecting the same file later
  });

  // ---- 055 cross-device continuity: background-run arming + task chips ----
  // The composer toggle next to the paperclip arms async_mode for the NEXT
  // send only; sendChat reads bgArmed via hoisting (same contract as the
  // attachment helpers above it) and disarms after the message goes out.
  var bgBtn = document.getElementById("astral-bg-btn");
  var bgArmed = false;
  function setBgArmed(on) {
    bgArmed = !!on;
    if (!bgBtn) return;
    bgBtn.setAttribute("aria-pressed", bgArmed ? "true" : "false");
    // Armed look via the runtime theme tokens — this file styles its own
    // dynamic chrome inline (see showToast/openChromePop).
    bgBtn.style.cssText = bgArmed
      ? "color:rgb(var(--astral-primary));border-color:rgb(var(--astral-primary) / .7);background:rgb(var(--astral-primary) / .15);"
      : "";
  }
  if (bgBtn) bgBtn.addEventListener("click", function () { setBgArmed(!bgArmed); });

  // One slim chip per running background task (keyed by task_id, cleared by
  // its task_completed). Lives at the top of the composer so it survives chat
  // switches; tapping a chip opens the task's chat.
  var bgTaskChips = {};  // task_id → chip element
  var bgTaskDone = {};   // task_id → true (dedupes watcher + fan-out copies)
  var bgTaskHost = null;
  function bgTaskHostEl() {
    if (!bgTaskHost && form) {
      bgTaskHost = document.createElement("div");
      bgTaskHost.id = "astral-bgtasks";
      bgTaskHost.style.cssText = "display:none;flex-wrap:wrap;gap:6px;";
      form.insertBefore(bgTaskHost, form.firstChild);
    }
    return bgTaskHost;
  }
  function syncBgTaskHost() {
    if (bgTaskHost) bgTaskHost.style.display = bgTaskHost.children.length ? "flex" : "none";
  }
  function addTaskChip(taskId, chatId, title) {
    if (!taskId || bgTaskChips[taskId] || bgTaskDone[taskId]) return;
    var host = bgTaskHostEl();
    if (!host) return;
    var chip = document.createElement("button");
    chip.type = "button";
    chip.className = "astral-chip";
    chip.style.cursor = "pointer";
    chip.title = "Open the chat running this task";
    var dot = document.createElement("span");
    dot.style.cssText = "width:7px;height:7px;border-radius:9999px;background:rgb(var(--astral-primary));flex:none;";
    chip.appendChild(dot);
    var label = document.createElement("span");
    label.className = "astral-chip-name";
    label.textContent = "Background task running" + (title ? " — " + title : "…");
    chip.appendChild(label);
    if (chatId) chip.addEventListener("click", function () {
      if (chatId !== activeChatId) loadActiveChat(chatId);
      closeHistoryOverlay();
    });
    host.appendChild(chip);
    bgTaskChips[taskId] = chip;
    syncBgTaskHost();
  }
  function removeTaskChip(taskId) {
    var chip = bgTaskChips[taskId];
    if (chip && chip.parentNode) chip.parentNode.removeChild(chip);
    delete bgTaskChips[taskId];
    syncBgTaskHost();
    // Don't leave the dispatch-time status text stranded once nothing runs.
    var any = false;
    for (var k in bgTaskChips) { any = true; break; }
    if (!any && statusEl && statusEl.textContent === "Running in background…") setStatus("");
  }

  // Chrome runtime: settings menu, modal surfaces, generic [data-ui-action]
  // delegation, and the tour step-runner. Server renders all chrome HTML
  // (webrender/chrome/); this block is plumbing only.
  var modalRoot = document.getElementById("astral-modal");
  var modalReturnFocus = null;

  // ---- chrome_open perceived latency (feature 052): a local skeleton fills
  // the modal instantly; chrome_render replaces it via setModal. If nothing
  // arrives within the timeout, a retry card re-sends the same chrome_open
  // instead of leaving an infinite shimmer. Focus is NOT moved here so
  // setModal still captures the real return-focus element when it lands.
  var MODAL_SKELETON_TIMEOUT_MS = 6000;
  var modalSkeletonTimer = null;
  var modalSkeletonRequest = null;
  function clearModalSkeletonTimer() {
    if (modalSkeletonTimer) { clearTimeout(modalSkeletonTimer); modalSkeletonTimer = null; }
  }
  function modalShellHtml(bodyHtml) {
    return '<div class="astral-modal-backdrop fixed inset-0 z-50 bg-black/60 backdrop-blur-sm '
      + 'flex items-start justify-center overflow-y-auto py-10">'
      + '<div class="astral-modal-card relative bg-astral-surface border border-white/10 rounded-xl '
      + 'shadow-2xl w-full max-w-3xl mx-4 my-auto" role="dialog" aria-modal="true" tabindex="-1">'
      + '<div class="px-5 py-4 space-y-4">' + bodyHtml + "</div></div></div>";
  }
  function showModalSkeleton(act, payload) {
    if (!modalRoot) return;
    clearModalSkeletonTimer();
    modalSkeletonRequest = { action: act, payload: payload || {} };
    modalRoot.innerHTML = modalShellHtml(
      '<div class="astral-skeleton" role="status" aria-busy="true" aria-live="polite">'
      + '<span class="sr-only">Loading…</span>'
      + '<div class="astral-skeleton-line h-3 w-1/3 mb-3"></div>'
      + '<div class="astral-skeleton-line h-20 w-full mb-3"></div>'
      + '<div class="astral-skeleton-line h-20 w-full mb-3"></div>'
      + '<div class="astral-skeleton-line h-3 w-1/2 mb-2"></div></div>');
    modalSkeletonTimer = setTimeout(showModalRetry, MODAL_SKELETON_TIMEOUT_MS);
  }
  function showModalRetry() {
    modalSkeletonTimer = null;
    if (!modalRoot || !modalSkeletonRequest) return;
    modalRoot.innerHTML = modalShellHtml(
      '<div class="text-sm text-astral-text" role="status">This is taking longer than expected.</div>'
      + '<div class="flex gap-2">'
      + '<button type="button" class="astral-modal-retry px-3 py-1.5 rounded-lg text-xs font-medium '
      + 'bg-astral-primary text-white">Retry</button>'
      + '<button type="button" class="astral-modal-close px-3 py-1.5 rounded-lg text-xs '
      + 'bg-white/5 border border-white/10 text-astral-text">Close</button></div>');
    var retry = modalRoot.querySelector(".astral-modal-retry");
    if (retry) retry.addEventListener("click", function () {
      var req = modalSkeletonRequest;
      showModalSkeleton(req.action, req.payload);
      action(req.action, req.payload);
    });
  }

  /** Replace the chrome modal content; empty html closes it (restores focus). */
  function setModal(htmlStr) {
    if (!modalRoot) return;
    clearAuthoringControlPending();
    clearModalSkeletonTimer();
    if (htmlStr) {
      modalReturnFocus = document.activeElement;
      modalRoot.innerHTML = htmlStr;
      processSideEffects(modalRoot);
      var card = modalRoot.querySelector(".astral-modal-card");
      if (card) card.focus();
      maybeStartTour();
    } else {
      modalRoot.innerHTML = "";
      if (modalReturnFocus && modalReturnFocus.focus) { try { modalReturnFocus.focus(); } catch (e) {} }
      modalReturnFocus = null;
    }
  }
  /** Feature 054: a modal whose card carries data-mandatory (the first-run
   *  provider-setup gate) refuses every dismissal affordance — ✕/backdrop/
   *  Escape all funnel here. The server closes it after a successful save;
   *  the dialog's "Sign out" link is the one escape hatch. */
  function modalIsMandatory() {
    return !!(modalRoot && modalRoot.querySelector && modalRoot.querySelector(".astral-modal-card[data-mandatory]"));
  }
  function closeModal() {
    if (!modalRoot || !modalRoot.innerHTML) return;
    if (modalIsMandatory()) return;
    setModal(""); action("chrome_close", {});
  }

  // ---- settings menu (static, server-rendered; WAI-ARIA menu pattern) ----
  function menuEl() { return document.getElementById("astral-settings-menu"); }
  function menuBtn() { return document.getElementById("astral-settings-btn"); }
  function menuItems() {
    var m = menuEl(); if (!m) return [];
    return Array.prototype.slice.call(m.querySelectorAll('[role="menuitem"]'));
  }
  function menuOpen() { var m = menuEl(); return !!(m && !m.hidden); }
  function setMenu(open, focusFirst) {
    var m = menuEl(), b = menuBtn(); if (!m || !b) return;
    m.hidden = !open;
    b.setAttribute("aria-expanded", open ? "true" : "false");
    if (open && focusFirst) { var items = menuItems(); if (items.length) items[0].focus(); }
    // Restoring focus to the gear is right for normal open/close, but mid-tour
    // it would arm the button's Enter/Space/ArrowDown handler — the next key
    // press would reopen the menu instead of advancing the tour.
    if (!open && !tourState) { try { b.focus(); } catch (e) {} }
  }
  function menuMove(delta, edge) {
    var items = menuItems(); if (!items.length) return;
    var idx = items.indexOf(document.activeElement);
    var next = edge != null ? edge : (idx < 0 ? 0 : (idx + delta + items.length) % items.length);
    items[next].focus();
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("#astral-settings-btn");
    if (btn) { setMenu(!menuOpen(), false); return; }
    // Tour-card clicks must not count as "outside" — the tour opens the menu
    // to spotlight in-menu targets, and Next would otherwise close it again.
    var inTour = e.target.closest && e.target.closest("#astral-tour-card");
    if (menuOpen() && !inTour && !(e.target.closest && e.target.closest("#astral-settings-menu"))) setMenu(false, false);
    // modal close affordances: X button or backdrop click
    if (e.target.closest && e.target.closest(".astral-modal-close")) { closeModal(); return; }
    var backdrop = e.target.classList && e.target.classList.contains("astral-modal-backdrop");
    if (backdrop) closeModal();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (tourState) { endTour("dismissed"); return; }
      if (menuOpen()) { setMenu(false, false); return; }
      if (modalRoot && modalRoot.innerHTML) { closeModal(); return; }
    }
    var b = menuBtn();
    if (document.activeElement === b && (e.key === "Enter" || e.key === " " || e.key === "ArrowDown")) {
      e.preventDefault(); setMenu(true, true); return;
    }
    if (!menuOpen()) return;
    var inMenu = e.target.closest && e.target.closest("#astral-settings-menu");
    if (!inMenu) return;
    if (e.key === "ArrowDown") { e.preventDefault(); menuMove(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); menuMove(-1); }
    else if (e.key === "Home") { e.preventDefault(); menuMove(0, 0); }
    else if (e.key === "End") { e.preventDefault(); menuMove(0, menuItems().length - 1); }
    else if (e.key === "Tab") { e.preventDefault(); menuMove(e.shiftKey ? -1 : 1); }
  });

  // ---- generic [data-ui-action] delegation (chrome surfaces + creation cards) ----
  function collectChromeFields(container) {
    var fields = {};
    if (!container) return fields;
    var els = container.querySelectorAll("input[name], select[name], textarea[name]");
    for (var i = 0; i < els.length; i++) {
      var el = els[i], name = el.getAttribute("name");
      if (el.type === "checkbox") fields[name] = el.checked;
      else if (el.type === "radio") { if (el.checked) fields[name] = el.value; }
      else if (el.type === "number") fields[name] = el.value === "" ? null : Number(el.value);
      else fields[name] = el.value;
    }
    return fields;
  }

  var AUTHORING_MUTATION_ACTIONS = Object.freeze({
    chrome_author_create: true,
    chrome_author_edit: true,
    chrome_author_clarify: true,
    chrome_author_advance: true,
    chrome_author_analyze: true,
    chrome_author_generate: true,
    chrome_author_draft: true,
  });
  var pendingAuthoringControl = null;
  var pendingAuthoringTimer = null;

  function clearAuthoringControlPending() {
    if (pendingAuthoringTimer) clearTimeout(pendingAuthoringTimer);
    pendingAuthoringTimer = null;
    if (pendingAuthoringControl) {
      pendingAuthoringControl.setAttribute("aria-busy", "false");
      pendingAuthoringControl.setAttribute("aria-disabled", "false");
      pendingAuthoringControl.setAttribute("data-control-state", "ready");
    }
    pendingAuthoringControl = null;
  }

  function beginAuthoringControlPending(el, actionName) {
    if (!AUTHORING_MUTATION_ACTIONS[actionName]) return true;
    if (pendingAuthoringControl || el.getAttribute("aria-busy") === "true") return false;
    pendingAuthoringControl = el;
    el.setAttribute("aria-busy", "true");
    el.setAttribute("aria-disabled", "true");
    el.setAttribute("data-control-state", "submitting");
    // Preserve the native button in the focus order while exposing a guarded
    // single-flight state. The server-rendered replacement clears the state;
    // this bound restores it if a response never arrives.
    pendingAuthoringTimer = setTimeout(clearAuthoringControlPending, 10000);
    return true;
  }

  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-ui-action]");
    if (!el) return;
    var act = el.getAttribute("data-ui-action");
    if (!beginAuthoringControlPending(el, act)) {
      e.preventDefault();
      return;
    }
    var payload = {};
    try { payload = JSON.parse(el.getAttribute("data-ui-payload") || "{}"); } catch (err) {}
    if (el.getAttribute("data-ui-collect") === "true") {
      payload.fields = collectChromeFields(el.closest("[data-ui-form]") || modalRoot);
    }
    // The timeline surface needs the active chat, which only the client knows
    // at click time (the static menu is rendered per shell).
    if (act === "chrome_open" && payload.surface === "workspace_timeline") {
      payload.params = payload.params || {};
      if (!payload.params.chat_id && activeChatId) payload.params.chat_id = activeChatId;
    }
    if (act === "chrome_open") { setMenu(false, false); showModalSkeleton(act, payload); }
    action(act, payload);
  });

  // Permission sections (Agents & permissions): the section master gates its
  // tool switches — on enables them all, off clears and disables them. The
  // server enforces the same rule on save; this just keeps the form honest.
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!(t.classList && t.classList.contains("astral-perm-master"))) return;
    var section = t.closest && t.closest("[data-perm-section]");
    if (!section) return;
    var on = t.checked;
    var tools = section.querySelectorAll(".astral-perm-tool");
    for (var i = 0; i < tools.length; i++) { tools[i].checked = on; tools[i].disabled = !on; }
    var body = section.querySelector(".astral-perm-tools");
    if (body) body.classList.toggle("opacity-50", !on);
  });

  // LLM provider picker (feature 054): the chrome modal is static HTML with no
  // reactive re-render, so toggle the endpoint field client-side when the
  // provider dropdown changes — show the free-form base_url input only for
  // "custom", otherwise show the (auto-set) preset endpoint caption. The
  // server still derives the URL for presets, so the hidden input is inert.
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!(t.classList && t.classList.contains("astral-llm-provider"))) return;
    var form = t.closest && t.closest("[data-ui-form]");
    var wrap = form && form.querySelector(".astral-llm-endpoint");
    if (!wrap) return;
    var map = {};
    try { map = JSON.parse(form.getAttribute("data-llm-endpoints") || "{}"); } catch (err) {}
    var preset = wrap.querySelector(".astral-llm-endpoint-preset");
    var custom = wrap.querySelector(".astral-llm-endpoint-custom");
    var urlEl = wrap.querySelector(".astral-llm-endpoint-url");
    var input = wrap.querySelector('input[name="base_url"]');
    if (t.value === "custom") {
      if (preset) preset.style.display = "none";
      if (custom) custom.style.display = "";
      if (input) { input.value = ""; input.focus(); }
    } else {
      if (custom) custom.style.display = "none";
      if (preset) preset.style.display = "";
      if (urlEl) urlEl.textContent = map[t.value] || "";
      if (input) input.value = "";  // preset URL is derived server-side
    }
  });

  // Feature 063: the remote-machines "Credential type" dropdown toggles which
  // credential fields are shown — SSH key + passphrase for "ssh_key", the
  // password field for "password". Both groups are always in the DOM (the chrome
  // modal has no reactive re-render); this flips display to match the selection.
  // Same static-modal pattern as the LLM provider/endpoint toggle above.
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!(t.classList && t.classList.contains("astral-cred-type"))) return;
    var form = t.closest && t.closest("[data-ui-form]");
    if (!form) return;
    var groups = form.querySelectorAll(".astral-cred-group");
    for (var i = 0; i < groups.length; i++) {
      var g = groups[i];
      g.style.display = g.classList.contains("astral-cred-" + t.value) ? "" : "none";
    }
  });

  // ---- tour runner (steps server-rendered into [data-tour-steps]; A10 skips) ----
  var tourState = null;
  function maybeStartTour() {
    var holder = modalRoot && modalRoot.querySelector("[data-tour-steps]");
    if (!holder) return;
    var steps = [];
    try { steps = JSON.parse(holder.getAttribute("data-tour-steps") || "[]"); } catch (e) { return; }
    if (!steps.length) return;
    setModal(""); // tour replaces the modal with its floating card
    action("chrome_close", {});
    tourState = { steps: steps, idx: 0 };
    action("chrome_tour_event", { event: "started" });
    showTourStep();
  }
  function tourTargetEl(step) {
    if (!step.target_key) return null;
    try { return document.querySelector('[data-tour-target="' + step.target_key + '"]'); } catch (e) { return null; }
  }
  function clearTourHighlight() {
    var hl = document.querySelectorAll(".astral-tour-highlight");
    for (var i = 0; i < hl.length; i++) hl[i].classList.remove("astral-tour-highlight");
    var card = document.getElementById("astral-tour-card");
    if (card) card.parentNode.removeChild(card);
  }
  function showTourStep() {
    if (!tourState) return;
    clearTourHighlight();
    var step = tourState.steps[tourState.idx];
    var target = tourTargetEl(step);
    var skippedNote = "";
    if (step.target_kind === "static" && step.target_key && !target) {
      // A10: target belongs to chrome that isn't built yet — note + no highlight.
      skippedNote = '<div class="text-xs text-astral-muted italic mt-1">(this step’s target isn’t available yet)</div>';
    }
    // In-menu targets need the popover open (and laid out — scrollIntoView is
    // a no-op while it is hidden) BEFORE the highlight; any other step closes
    // it again so it doesn't cover the topbar/canvas highlights (Back
    // navigation, the no-target intro/outro cards).
    if (target && (target.id === "astral-settings-menu" || (target.closest && target.closest("#astral-settings-menu")))) setMenu(true, false);
    else if (menuOpen()) setMenu(false, false);
    if (target) {
      target.classList.add("astral-tour-highlight");
      if (target.scrollIntoView) target.scrollIntoView({ block: "nearest" });
    }
    var card = document.createElement("div");
    card.id = "astral-tour-card";
    card.className = "fixed bottom-6 left-1/2 -translate-x-1/2 z-[70] w-[360px] max-w-[90vw] " +
      "bg-astral-surface border border-white/10 rounded-xl shadow-2xl p-4";
    var last = tourState.idx === tourState.steps.length - 1;
    card.innerHTML =
      '<div class="text-xs text-astral-muted mb-1">Step ' + (tourState.idx + 1) + " of " + tourState.steps.length + "</div>" +
      '<div class="text-sm font-semibold text-astral-text mb-1" id="astral-tour-title"></div>' +
      '<div class="text-sm text-astral-text/80" id="astral-tour-body"></div>' + skippedNote +
      '<div class="flex justify-between items-center mt-3">' +
      '<button type="button" class="astral-tour-skip text-xs text-astral-muted hover:text-astral-text">Skip tour</button>' +
      '<div class="flex gap-2">' +
      (tourState.idx > 0 ? '<button type="button" class="astral-tour-back px-3 py-1.5 rounded-lg text-xs bg-white/5 border border-white/10 text-astral-text">Back</button>' : "") +
      '<button type="button" class="astral-tour-next px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary text-white">' + (last ? "Finish" : "Next") + "</button>" +
      "</div></div>";
    document.body.appendChild(card);
    // server step content is text — set via textContent to stay inert
    card.querySelector("#astral-tour-title").textContent = step.title || "";
    card.querySelector("#astral-tour-body").textContent = step.body || "";
    var next = card.querySelector(".astral-tour-next");
    next.addEventListener("click", function () {
      if (last) { endTour("completed"); }
      else { tourState.idx++; showTourStep(); }
    });
    var back = card.querySelector(".astral-tour-back");
    if (back) back.addEventListener("click", function () { tourState.idx--; showTourStep(); });
    card.querySelector(".astral-tour-skip").addEventListener("click", function () { endTour("skipped"); });
    // Each step rebuilds the card, dropping focus to <body>; put it on Next so
    // Enter keeps advancing for keyboard users.
    try { next.focus(); } catch (e) {}
  }
  function endTour(outcome) {
    var wasRunning = !!tourState;
    tourState = null; // before setMenu so the gear regains focus at tour end
    clearTourHighlight();
    setMenu(false, false);
    if (wasRunning) action("chrome_tour_event", { event: outcome });
  }

  // ---- connection lifecycle ----
  function connect() {
    var preserveVoiceControls = !!voiceRecoverableFence() || !!voicePendingEndFence;
    voiceBinding = null;
    voiceComposer = null;
    voiceComposerRevision = -1;
    voiceTakeover = null;
    clearVoiceBindingRenewal();
    if (voiceControlsEl && !preserveVoiceControls) {
      // 066: never leave the composer without a voice affordance.
      renderDefaultVoiceControl("Voice unavailable while reconnecting…");
    }
    connectionGeneration = randomUuid4();
    ws = new WebSocket(WS_URL);
    ws.onopen = function () {
      attempts = 0; authRetried = false; setStatus("");
      setConnState("connecting", "Registering…");
      // resumed: firstConnect ? serverResumed : true
      sendRegistration(firstConnect ? serverResumed : true);
      firstConnect = false;
      // Startup/reconnect metadata is retained and reconciled like every other
      // operation, but it is not user work and must not flash a global spinner.
      action("get_history", {}, false);
      // Re-attach to still-running background tasks: watch_task re-registers
      // this socket as a watcher and answers task_completed immediately when
      // the task finished while the socket was down.
      for (var tid in bgTaskChips) action("watch_task", { task_id: tid }, false);
    };
    ws.onmessage = onMessage;
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
    ws.onclose = function () {
      socketReady = false;
      setConnState("offline", "Reconnecting — messages will queue");
      operationSubmissionByGeneration = Object.create(null);
      operationSubmissionById = Object.create(null);
      clearVoiceBindingRenewal();
      voiceBinding = null;
      if (voiceRecoverableFence() && document.visibilityState !== "hidden") {
        if (!beginVoiceRecovery("network_interrupted")) suspendVoiceForNetworkLoss();
      } else if (voiceActivation) {
        teardownVoiceMedia(false);
        setVoiceFeedback("reconnecting", "network_interrupted", null, true);
      }
      setStatus("Disconnected"); attempts++;
      hideSkeleton(); // the in-flight turn died with the socket
      clearTransientOverlay(); // old connection/request previews are disposable
      // Refresh the session token BEFORE reconnecting so a register_ui after
      // the access-token TTL recovers silently instead of dead-ending. First
      // connect uses the shell-injected token directly.
      if (attempts <= 10) setTimeout(function () {
        refreshToken(false, function () { connect(); });
      }, 3000);
    };
  }
  // Account digest and locator selection complete before the first socket can
  // register. If the shell token cannot be decoded, /auth/session gets one
  // bounded chance to provide a fresh token; connection still fails closed at
  // the server when that session is unavailable.
  prepareAccountIdentity(token, null).then(function (ready) {
    if (ready) connect();
    else refreshToken(false, function () { connect(); });
  }).catch(function () { connect(); });

  // Warm the lazy Plotly bundle once the boot work has settled so the first
  // chart-bearing turn is usually already loaded.
  function idlePrefetchPlotly() { ensurePlotly(null); }
  if (window.requestIdleCallback) window.requestIdleCallback(idlePrefetchPlotly, { timeout: 5000 });
  else setTimeout(idlePrefetchPlotly, 2500);
})();

/* Feature 040 (US5): slash-command typeahead. Discovery only — the server
   rewrites a "/command" into a normal prompt; nothing here invokes a tool. The
   curated list mirrors orchestrator/slash_commands.COMMANDS. */
(function () {
  var COMMANDS = [
    { name: "/help", desc: "show available commands" },
    { name: "/agents", desc: "list your enabled agents" },
    { name: "/summarize", desc: "summarize a link or text" },
    { name: "/research", desc: "research + cited brief" },
    { name: "/weather", desc: "weather + forecast" },
    { name: "/download", desc: "get the Windows desktop app" }
  ];
  var input = document.getElementById("astral-input");
  var menu = document.getElementById("astral-slash-menu");
  if (!input || !menu) return;

  function hide() { menu.classList.add("hidden"); menu.innerHTML = ""; }

  function render(matches) {
    if (!matches.length) { hide(); return; }
    menu.innerHTML = "";
    matches.forEach(function (c) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "astral-slash-item";
      item.setAttribute("role", "option");
      var n = document.createElement("span");
      n.className = "astral-slash-name";
      n.textContent = c.name;
      var d = document.createElement("span");
      d.className = "astral-slash-desc";
      d.textContent = c.desc;
      item.appendChild(n);
      item.appendChild(d);
      // mousedown (not click) fires before the input blur that would hide us.
      item.addEventListener("mousedown", function (e) {
        e.preventDefault();
        input.value = c.name + " ";
        hide();
        input.focus();
      });
      menu.appendChild(item);
    });
    menu.classList.remove("hidden");
  }

  function update() {
    var trimmed = (input.value || "").replace(/^\s+/, "");
    // Only while typing the command NAME: a leading "/" and no space yet.
    if (trimmed.charAt(0) !== "/" || trimmed.indexOf(" ") !== -1) { hide(); return; }
    var prefix = trimmed.toLowerCase();
    render(COMMANDS.filter(function (c) { return c.name.indexOf(prefix) === 0; }));
  }

  input.addEventListener("input", update);
  input.addEventListener("blur", function () { setTimeout(hide, 120); });
  input.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
})();
