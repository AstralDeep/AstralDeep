// Feature 065 browser-media contract suite.  It drives the shipped classic
// client against a synthetic DOM, WebSocket, getUserMedia, and LiveKit room;
// no speech API or product-only test hook is used.
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

import { expect, test } from "@playwright/test";

import { convertPlaywrightV8Coverage } from "../coverage-conversion.mjs";


const ROOT = resolve(import.meta.dirname, "../../..");
const CLIENT_PATH = resolve(ROOT, "backend/webrender/static/client.js");
const FIXTURE_PATH = resolve(ROOT, "backend/tests/fixtures/voice_065/client_conformance.json");
const CHAT_ID = "11111111-1111-4111-8111-111111111111";
const OLDER_CHAT_ID = "88888888-8888-4888-8888-888888888888";
const SESSION_ID = "22222222-2222-4222-8222-222222222222";
const TURN_ID = "33333333-3333-4333-8333-333333333333";
const CLIENT_TURN_ID = "44444444-4444-4444-8444-444444444444";
const SUBMISSION_ID = "55555555-5555-4555-8555-555555555555";
const REQUEST_ID = "66666666-6666-4666-8666-666666666666";
const WORKER_IDENTITY = "voice-worker-065";
const BINDING = "synthetic-binding-value-000000000000";
const VOICE_RETRY_SETTLE_MS = 2750;
const VOICE_COVERAGE_OUTPUT = process.env.ASTRAL_VOICE_COVERAGE_ISTANBUL_OUTPUT;
const voiceCoverageEntries = [];

const fixture = JSON.parse(await readFile(FIXTURE_PATH, "utf8"));
const fixtureComposer = fixture.cases
  .find((item) => item.id === "C0").positive[0].payload;
const fixtureVectors = new Map(fixture.cases.flatMap((fixtureCase) => (
  [...fixtureCase.positive, ...fixtureCase.negative].map((vector) => [vector.id, vector])
)));


function mergedVoiceCoverageEntries(entries) {
  const merged = new Map();
  for (const entry of entries) {
    let parsed;
    try {
      parsed = new URL(entry.url);
    } catch {
      continue;
    }
    if (
      parsed.origin !== "https://candidate.example"
      || parsed.pathname !== "/static/client.js"
    ) {
      continue;
    }
    const sourcePath = "backend/webrender/static/client.js";
    if (typeof entry.source !== "string") {
      throw new Error("voice coverage entry lacks exact client source text");
    }
    const record = merged.get(sourcePath) ?? { source: entry.source, ranges: new Map() };
    if (record.source !== entry.source) {
      throw new Error("client source changed during the voice browser run");
    }
    for (const functionCoverage of entry.functions ?? []) {
      for (const { startOffset, endOffset, count } of functionCoverage.ranges ?? []) {
        const key = `${startOffset}:${endOffset}`;
        const mergedCount = (record.ranges.get(key)?.count ?? 0) + count;
        if (!Number.isSafeInteger(mergedCount)) {
          throw new Error("voice coverage count overflow");
        }
        record.ranges.set(key, { startOffset, endOffset, count: mergedCount });
      }
    }
    merged.set(sourcePath, record);
  }
  return [...merged.entries()].map(([sourcePath, record]) => ({
    sourcePath,
    source: record.source,
    functions: [{ ranges: [...record.ranges.values()] }],
  }));
}


async function writeVoiceCoverage(output, document) {
  const outputPath = resolve(output);
  const temporaryPath = `${outputPath}.${process.pid}.tmp`;
  await mkdir(dirname(outputPath), { recursive: true });
  try {
    await writeFile(temporaryPath, `${JSON.stringify(document, null, 2)}\n`, {
      encoding: "utf8",
      flag: "wx",
      mode: 0o600,
    });
    await rename(temporaryPath, outputPath);
  } catch (error) {
    await rm(temporaryPath, { force: true });
    throw error;
  }
}


test.beforeEach(async ({ browserName, page }) => {
  if (!VOICE_COVERAGE_OUTPUT) return;
  if (browserName !== "chromium") {
    throw new Error("voice Istanbul production requires the pinned Chromium lane");
  }
  await page.coverage.startJSCoverage({
    reportAnonymousScripts: false,
    resetOnNavigation: false,
  });
});


test.afterEach(async ({ page }) => {
  if (!VOICE_COVERAGE_OUTPUT) return;
  voiceCoverageEntries.push(...await page.coverage.stopJSCoverage());
});


test.afterAll(async () => {
  if (!VOICE_COVERAGE_OUTPUT) return;
  const entries = mergedVoiceCoverageEntries(voiceCoverageEntries);
  if (entries.length !== 1) {
    throw new Error("voice browser run did not produce exactly one maintained client source");
  }
  const report = await convertPlaywrightV8Coverage(
    entries,
    (entry) => entry.sourcePath,
  );
  if (!Object.hasOwn(report.coverage, "backend/webrender/static/client.js")) {
    throw new Error("voice Istanbul report does not contain the shipped client");
  }
  await writeVoiceCoverage(VOICE_COVERAGE_OUTPUT, report);
});


function materializeFixtureVector(vectorId, stack = []) {
  if (stack.includes(vectorId)) throw new Error(`fixture cycle at ${vectorId}`);
  const source = fixtureVectors.get(vectorId);
  if (!source) throw new Error(`unknown fixture vector ${vectorId}`);
  const value = source.base_vector
    ? materializeFixtureVector(source.base_vector, [...stack, vectorId])
    : {};
  for (const [key, child] of Object.entries(structuredClone(source))) {
    if (key !== "base_vector" && key !== "mutations") value[key] = child;
  }
  for (const mutation of source.mutations || []) {
    const pieces = mutation.path.slice(1).split("/").map((piece) => (
      piece.replaceAll("~1", "/").replaceAll("~0", "~")
    ));
    let parent = value.payload;
    for (const piece of pieces.slice(0, -1)) parent = parent[piece];
    const key = pieces.at(-1);
    if (mutation.op === "remove") delete parent[key];
    else if (mutation.op === "repeat") parent[key] = mutation.value.repeat(mutation.count);
    else parent[key] = structuredClone(mutation.value);
  }
  return value;
}


function bindFixturePayload(vectorId, scope) {
  const replacements = new Map([
    ["00000000-0000-4000-8000-000000000001", scope.device_id],
    ["00000000-0000-4000-8000-000000000002", scope.connection_generation],
    ["00000000-0000-4000-8000-000000000003", SESSION_ID],
    ["00000000-0000-4000-8000-000000000004", CHAT_ID],
    ["00000000-0000-4000-8000-000000000005", TURN_ID],
    ["00000000-0000-4000-8000-000000000006", CLIENT_TURN_ID],
    ["00000000-0000-4000-8000-000000000007", SUBMISSION_ID],
    ["00000000-0000-4000-8000-000000000008", REQUEST_ID],
    ["voice-worker-01", WORKER_IDENTITY],
  ]);
  function bind(value, key = "") {
    if (Array.isArray(value)) return value.map((child) => bind(child));
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([childKey, child]) => (
        [childKey, bind(child, childKey)]
      )));
    }
    if (key === "media_grant_revision" && value === 2) return 1;
    return replacements.get(value) || value;
  }
  return bind(materializeFixtureVector(vectorId).payload);
}


function htmlShell() {
  return `<!doctype html><html><body>
    <header id="astral-topbar"><a id="logout" href="/auth/logout">Sign out</a></header>
    <button id="astral-newchat-btn" type="button">New chat</button>
    <button id="astral-chats-btn" type="button"></button>
    <button id="astral-msgs-toggle" type="button"></button>
    <span id="astral-msgs-label"></span>
    <div id="astral-history"></div>
    <main>
      <section id="astral-canvas"><div id="astral-canvas-empty">Empty</div></section>
      <div id="astral-chat"></div>
      <div id="astral-status"></div>
      <form id="astral-form">
        <div id="astral-voice-controls" role="group" aria-label="Voice conversation controls"></div>
        <input id="astral-input"><button type="submit">Send</button>
        <div id="astral-voice-feedback" class="astral-voice-feedback" hidden>
          <div id="astral-voice-status" role="status" aria-live="polite" aria-atomic="true"></div>
          <div id="astral-voice-transcript" aria-live="polite"></div>
          <button id="astral-voice-audio-resume" type="button" hidden>Enable voice audio</button>
        </div>
        <div id="astral-voice-turn-notice" class="astral-voice-turn-notice" role="alert"
             aria-live="assertive" aria-atomic="true" hidden>
          <span class="astral-voice-turn-notice-icon" aria-hidden="true">!</span>
          <div>
            <strong id="astral-voice-turn-notice-title" class="astral-voice-turn-notice-title"></strong>
            <span id="astral-voice-turn-notice-message"></span>
            <span id="astral-voice-turn-notice-guidance" class="astral-voice-turn-notice-fallback">Typed chat remains available.</span>
          </div>
        </div>
        <div id="astral-voice-audio" hidden aria-hidden="true"></div>
      </form>
      <div id="astral-modal"></div>
    </main>
  </body></html>`;
}


async function installHarness(page, {
  selectedChat = true,
  mediaMode = "ok",
  audioBlocked = false,
  deferredConnect = false,
} = {}) {
  await page.addInitScript(({ mode, blocked, deferConnect }) => {
    window.__ASTRAL_TOKEN__ = "synthetic-user-token";
    window.__ASTRAL_RESUMED__ = true;
    window.__socketEvents = [];
    window.__sockets = [];
    window.__voiceFetches = [];
    window.__voiceResponses = [];
    window.__gumCalls = 0;
    window.__rooms = [];
    window.__voiceProcessors = [];
    window.__audioBlocked = blocked;
    window.requestIdleCallback = () => 0;

    class FakeVoiceProcessor {
      constructor() {
        this.onaudioprocess = null;
        this.connected = false;
        this.lastOutput = null;
        window.__voiceProcessors.push(this);
      }

      connect() { this.connected = true; }
      disconnect() { this.connected = false; }

      pump() {
        const inputData = new Float32Array(1024).fill(0.25);
        const outputData = new Float32Array(1024);
        this.onaudioprocess?.({
          inputBuffer: {
            length: 1024,
            numberOfChannels: 1,
            getChannelData: () => inputData,
          },
          outputBuffer: {
            length: 1024,
            numberOfChannels: 1,
            getChannelData: () => outputData,
          },
        });
        this.lastOutput = Array.from(outputData);
      }
    }

    class FakeAudioContext {
      constructor() {
        this.sampleRate = 24000;
        this.state = blocked ? "suspended" : "running";
        this.destination = {};
      }
      createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
      createScriptProcessor() { return new FakeVoiceProcessor(); }
      async resume() {
        if (window.__audioBlocked) throw new DOMException("Autoplay blocked", "NotAllowedError");
        this.state = "running";
      }
      async close() { this.state = "closed"; }
    }
    window.AudioContext = FakeAudioContext;
    window.MediaStream = class { constructor(tracks) { this.tracks = tracks; } };

    window.fetch = async (url, options = {}) => {
      const path = new URL(url, location.origin).pathname;
      if (path === "/auth/session") {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            authenticated: true,
            access_token: "synthetic-user-token",
            resumed: true,
            user_id: "voice-user",
          }),
        };
      }
      const record = {
        url: String(url),
        method: options.method || "GET",
        headers: { ...(options.headers || {}) },
        body: options.body ? JSON.parse(options.body) : null,
      };
      window.__voiceFetches.push(record);
      const response = window.__voiceResponses.shift() || {
        status: 503,
        body: { code: "voice_unavailable", message: "Voice unavailable", retryable: true },
      };
      if (response.delayMs) {
        await new Promise((resolveDelay) => setTimeout(resolveDelay, response.delayMs));
      }
      if (response.reject) throw new TypeError("Synthetic voice network failure");
      const responseBody = structuredClone(response.body);
      if (response.echoRefreshId && record.body?.refresh_id) {
        responseBody.refresh_id = record.body.refresh_id;
      }
      return {
        ok: response.status >= 200 && response.status < 300,
        status: response.status,
        headers: new Headers({ "content-type": "application/json" }),
        json: async () => responseBody,
        text: async () => JSON.stringify(responseBody),
      };
    };

    class FakeWebSocket {
      static OPEN = 1;

      constructor(socketUrl) {
        this.url = socketUrl;
        this.readyState = 0;
        this.sent = [];
        window.__sockets.push(this);
        queueMicrotask(() => {
          this.readyState = FakeWebSocket.OPEN;
          this.onopen?.();
        });
      }

      send(raw) {
        const frame = JSON.parse(raw);
        this.sent.push(frame);
        window.__socketEvents.push(frame);
      }

      close() {
        this.readyState = 3;
        this.onclose?.();
      }

      receive(frame) {
        this.onmessage?.({ data: JSON.stringify(frame) });
      }
    }
    window.WebSocket = FakeWebSocket;

    const permissionListeners = [];
    const permission = {
      state: mode === "deny" ? "prompt" : "prompt",
      addEventListener(name, callback) {
        if (name === "change") permissionListeners.push(callback);
      },
    };
    window.__permission = permission;

    const trackListeners = {};
    const microphoneTrack = {
      kind: "audio",
      enabled: true,
      readyState: "live",
      addEventListener(name, callback) { trackListeners[name] = callback; },
      stop() {
        this.readyState = "ended";
        this.enabled = false;
      },
      end() {
        this.readyState = "ended";
        this.enabled = false;
        trackListeners.ended?.();
      },
    };
    window.__voiceTrack = microphoneTrack;
    const mediaDeviceListeners = {};
    const mediaDevices = {
      async getUserMedia(constraints) {
        window.__gumCalls += 1;
        window.__lastConstraints = constraints;
        if (mode === "deny") throw new DOMException("Permission denied", "NotAllowedError");
        if (mode === "missing") throw new DOMException("No microphone", "NotFoundError");
        permission.state = "granted";
        microphoneTrack.readyState = "live";
        microphoneTrack.enabled = true;
        return {
          getAudioTracks: () => [microphoneTrack],
          getTracks: () => [microphoneTrack],
        };
      },
      async enumerateDevices() {
        return mode === "missing" ? [] : [{ kind: "audioinput", deviceId: "synthetic-mic" }];
      },
      addEventListener(name, callback) { mediaDeviceListeners[name] = callback; },
    };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: mode === "no-api" ? undefined : mediaDevices,
    });
    Object.defineProperty(navigator, "permissions", {
      configurable: true,
      value: { query: async () => permission },
    });

    class FakeRoom {
      constructor() {
        this.handlers = new Map();
        this.connected = false;
        this.disconnected = false;
        this.startAudioCalls = 0;
        this.localParticipant = {
          published: [],
          publishTrack: async (track, options) => {
            this.localParticipant.published.push({ track, options });
            return { trackSid: "TR_client_mic" };
          },
        };
        window.__rooms.push(this);
      }

      on(name, callback) {
        const callbacks = this.handlers.get(name) || [];
        callbacks.push(callback);
        this.handlers.set(name, callbacks);
        return this;
      }

      emit(name, ...args) {
        for (const callback of this.handlers.get(name) || []) callback(...args);
      }

      async startAudio() {
        this.startAudioCalls += 1;
        if (window.__audioBlocked) throw new DOMException("Autoplay blocked", "NotAllowedError");
      }

      async connect(url, joinToken, options) {
        this.connectStarted = true;
        if (deferConnect) {
          await new Promise((resolveConnect) => { this.resolveConnect = resolveConnect; });
        }
        this.connected = true;
        this.url = url;
        this.joinToken = joinToken;
        this.connectOptions = options;
      }

      disconnect() {
        this.disconnected = true;
        this.connected = false;
      }
    }

    window.LivekitClient = {
      setLogLevel(level) {
        window.__livekitLogLevels = window.__livekitLogLevels || [];
        window.__livekitLogLevels.push(level);
      },
      Room: FakeRoom,
      RoomEvent: {
        DataReceived: "dataReceived",
        Disconnected: "disconnected",
        ParticipantDisconnected: "participantDisconnected",
        TrackPublished: "trackPublished",
        TrackSubscribed: "trackSubscribed",
        TrackUnpublished: "trackUnpublished",
        TrackUnsubscribed: "trackUnsubscribed",
      },
      Track: {
        Kind: { Audio: "audio" },
        Source: { Microphone: "microphone" },
      },
    };
  }, { mode: mediaMode, blocked: audioBlocked, deferConnect: deferredConnect });

  await page.route("https://candidate.example/**", (route) => route.fulfill({
    contentType: "text/html",
    body: htmlShell(),
  }));
  const url = selectedChat ? `https://candidate.example/?chat=${CHAT_ID}` : "https://candidate.example/";
  await page.goto(url);
  const source = await readFile(CLIENT_PATH, "utf8");
  await page.addScriptTag({ content: `${source}\n//# sourceURL=https://candidate.example/static/client.js` });
  await page.waitForFunction(() => window.__socketEvents.some((frame) => frame.type === "register_ui"));
}


async function registration(page) {
  return page.evaluate(() => window.__socketEvents.find((frame) => frame.type === "register_ui"));
}


async function receive(page, frame) {
  await page.evaluate((value) => window.__sockets.at(-1).receive(value), frame);
}


async function queueResponse(page, status, body, delayMs = 0) {
  await page.evaluate(({ responseStatus, responseBody, responseDelay }) => {
    window.__voiceResponses.push({
      status: responseStatus,
      body: responseBody,
      delayMs: responseDelay,
    });
  }, { responseStatus: status, responseBody: body, responseDelay: delayMs });
}


async function queueRejectedResponse(page, delayMs = 0) {
  await page.evaluate((responseDelay) => {
    window.__voiceResponses.push({ reject: true, delayMs: responseDelay });
  }, delayMs);
}


async function queueRefreshResponse(page, status, body) {
  await page.evaluate(({ responseStatus, responseBody }) => {
    window.__voiceResponses.push({
      status: responseStatus,
      body: responseBody,
      echoRefreshId: true,
    });
  }, { responseStatus: status, responseBody: body });
}


function bindingFrame(scope) {
  return {
    type: "voice_control_binding",
    schema_version: "1",
    device_id: scope.device_id,
    connection_generation: scope.connection_generation,
    binding_id: "77777777-7777-4777-8777-777777777777",
    binding: BINDING,
    expires_at: new Date(Date.now() + 9 * 60 * 1000).toISOString(),
  };
}


function composerFrame(scope, overrides = {}, revision = 7) {
  const frame = structuredClone(fixtureComposer);
  frame.connection_generation = scope.connection_generation;
  frame.revision = revision;
  frame.voice.visible_chat_id = CHAT_ID;
  Object.assign(frame.voice, overrides);
  return frame;
}


function activeControls() {
  return structuredClone(fixtureComposer.voice.controls).map((control) => ({
    ...control,
    visible: control.action === "voice_session_end"
      || control.action === "voice_microphone_set"
      || control.action === "voice_speech_mute_set",
    enabled: control.action === "voice_session_end"
      || control.action === "voice_microphone_set"
      || control.action === "voice_speech_mute_set",
    pressed: control.action === "voice_microphone_set",
  }));
}


function sessionResponse(scope, {
  generation = 1,
  revision = 1,
  state = "active",
  foregroundActive = true,
  microphoneEnabled = true,
  visibleChatId = CHAT_ID,
  contextSynced = true,
} = {}) {
  return {
    session: {
      session_id: SESSION_ID,
      device_id: scope.device_id,
      device_kind: "web",
      transport: "livekit",
      state,
      generation,
      media_grant_revision: revision,
      owner_connection_generation: scope.connection_generation,
      visible_chat_id: visibleChatId,
      applied_visible_chat_id: contextSynced ? visibleChatId : null,
      chat_context_revision: 1,
      applied_chat_context_revision: contextSynced ? 1 : null,
      chat_context_synced: contextSynced,
      foreground_active: foregroundActive,
      foreground_reason: foregroundActive ? "foreground" : "backgrounded",
      foreground_changed_at: "2026-07-31T12:00:00Z",
      speech_muted: false,
      microphone_enabled: microphoneEnabled,
      lease_expires_at: "2099-07-31T12:10:00Z",
      started_at: "2026-07-31T12:00:00Z",
      idle_expires_at: "2099-07-31T12:05:00Z",
    },
    grant: {
      grant_id: "grant-065",
      transport: "livekit",
      session_id: SESSION_ID,
      generation,
      media_grant_revision: revision,
      expires_at: "2099-07-31T12:01:00Z",
      url: "wss://voice.example.test",
      join_token: "synthetic-livekit-token-value-000000000000",
      room_name: "room-065",
      participant_identity: "web-client-065",
      worker_identity: WORKER_IDENTITY,
    },
  };
}


function credentialFreeGrantState(scope, options = {}) {
  const response = sessionResponse(scope, options);
  return {
    session: response.session,
    grant_state: {
      transport: "livekit",
      media_grant_revision: response.session.media_grant_revision,
      status: options.grantStatus || "active",
      expires_at: options.grantStatus === "unavailable" ? null : "2099-07-31T12:01:00Z",
    },
  };
}


function refreshedGrant(scope, options = {}) {
  const response = sessionResponse(scope, options);
  return {
    refresh_id: options.refreshId,
    replayed: options.replayed || false,
    replay_expires_at: "2099-07-31T12:00:30Z",
    ...response,
  };
}


async function setVisibility(page, state) {
  await page.evaluate((nextState) => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: nextState });
    document.dispatchEvent(new Event("visibilitychange"));
  }, state);
}


function sessionState(scope, state, reason = "ready", overrides = {}) {
  return {
    type: "voice_session_state",
    schema_version: "1",
    session_id: SESSION_ID,
    connection_generation: scope.connection_generation,
    generation: overrides.generation || 1,
    media_grant_revision: 1,
    visible_chat_id: CHAT_ID,
    chat_context_revision: 1,
    applied_chat_context_revision: 1,
    chat_context_synced: true,
    state,
    speech_muted: false,
    microphone_enabled: state === "listening",
    foreground_active: state !== "ended" && state !== "suspended",
    reason,
    occurred_at: "2026-07-31T12:00:01Z",
    ...overrides,
  };
}


function turnState(scope, state, {
  message,
  sequence = 1,
  turnId = TURN_ID,
  occurredAt = "2026-07-31T12:00:01Z",
} = {}) {
  const frame = {
    type: "voice_turn_state",
    schema_version: "1",
    session_id: SESSION_ID,
    connection_generation: scope.connection_generation,
    generation: 1,
    media_grant_revision: 1,
    turn_id: turnId,
    client_turn_id: CLIENT_TURN_ID,
    submission_id: SUBMISSION_ID,
    request_generation: REQUEST_ID,
    chat_id: CHAT_ID,
    chat_context_revision: 1,
    detected_language: "en-US",
    spoken_output_policy: "full_recap",
    output_reason: "ready",
    state,
    foreground: true,
    sensitive_result_pending: false,
    sequence,
    occurred_at: occurredAt,
  };
  if (message !== undefined) frame.message = message;
  return frame;
}


function submissionRejectedFrame(scope, overrides = {}) {
  return {
    type: "voice_submission_rejected",
    schema_version: "1",
    session_id: SESSION_ID,
    connection_generation: scope.connection_generation,
    generation: 1,
    media_grant_revision: 1,
    turn_id: TURN_ID,
    client_turn_id: CLIENT_TURN_ID,
    submission_id: SUBMISSION_ID,
    request_generation: REQUEST_ID,
    chat_id: CHAT_ID,
    reason: "invalid_proof",
    retry_policy: "explicit_user_retry",
    message: "The spoken request could not be verified.",
    occurred_at: "2026-07-31T12:00:01Z",
    ...overrides,
  };
}


function transcriptFrame(overrides = {}) {
  return {
    type: "voice_transcript",
    schema_version: "1",
    session_id: SESSION_ID,
    generation: 1,
    turn_id: TURN_ID,
    client_turn_id: CLIENT_TURN_ID,
    submission_id: SUBMISSION_ID,
    request_generation: REQUEST_ID,
    chat_id: CHAT_ID,
    chat_context_revision: 1,
    media_grant_revision: 1,
    sequence: 2,
    final: true,
    text: "Please review the latest result",
    detected_language: "en-US",
    text_digest_sha256: "a".repeat(64),
    transcript_proof: "b".repeat(64),
    proof_expires_at: "2099-07-31T12:02:00Z",
    source_participant_identity: WORKER_IDENTITY,
    ...overrides,
  };
}


function announcementFrame({
  announcementId,
  sequence,
  trackSid,
  trackName,
  durationSamples = 100,
} = {}) {
  return {
    type: "voice_announcement_media",
    schema_version: "1",
    session_id: SESSION_ID,
    generation: 1,
    media_grant_revision: 1,
    announcement_id: announcementId,
    announcement_sequence: sequence,
    turn_id: TURN_ID,
    kind: "acknowledgement",
    quantum_role: "single",
    quantum_index: 0,
    transport: "livekit",
    worker_identity: WORKER_IDENTITY,
    track_sid: trackSid,
    track_name: trackName,
    sample_rate_hz: 24000,
    duration_samples: durationSamples,
  };
}


async function startReadyVoice(page) {
  const scope = await registration(page);
  await receive(page, bindingFrame(scope));
  await receive(page, composerFrame(scope));
  await queueResponse(page, 201, sessionResponse(scope));
  await page.getByRole("button", { name: "Start voice conversation" }).click();
  await page.waitForFunction(() => window.__rooms.some((room) => room.connected));
  return scope;
}


async function startLongVoicePlayout(page) {
  const manifest = announcementFrame({
    announcementId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    sequence: 1,
    trackSid: "TR_worker_interrupt",
    trackName: "astraldeep-announcement-interrupt",
    durationSamples: 48_000,
  });
  await page.evaluate(({ announcement, workerIdentity }) => {
    const room = window.__rooms[0];
    const participant = { identity: workerIdentity };
    window.__stopTimeline = [];
    const track = {
      kind: "audio",
      sid: announcement.track_sid,
      mediaStreamTrack: {},
      detach() {
        window.__stopTimeline.push("playout:detach");
        return [];
      },
    };
    const publication = {
      kind: "audio",
      trackSid: announcement.track_sid,
      trackName: announcement.track_name,
      subscriptions: [],
      setSubscribed(value) {
        this.subscriptions.push(value);
        if (value) queueMicrotask(() => room.emit("trackSubscribed", track, publication, participant));
      },
    };
    window.__stopPublication = publication;
    room.emit("trackPublished", publication, participant);
    room.emit(
      "dataReceived",
      new TextEncoder().encode(JSON.stringify(announcement)),
      participant,
      "reliable",
      "astraldeep.voice.announcement.v1",
    );
  }, { announcement: manifest, workerIdentity: WORKER_IDENTITY });
  await page.waitForFunction(() => window.__voiceProcessors.length === 1);
  await page.evaluate(() => {
    window.__voiceProcessors[0].pump();
    window.__stopTimeline = [];
    const originalFetch = window.fetch;
    window.fetch = async (...args) => {
      const path = new URL(args[0], location.origin).pathname;
      const isStop = path.endsWith("/speech/stop");
      if (isStop) window.__stopTimeline.push("fetch:start");
      try {
        const response = await originalFetch(...args);
        if (isStop) window.__stopTimeline.push("fetch:resolved");
        return response;
      } catch (error) {
        if (isStop) window.__stopTimeline.push("fetch:failed");
        throw error;
      }
    };
  });
  return manifest;
}


test("voice runtime loads no external asset or media dependency", async ({ page }) => {
  const requests = [];
  page.on("request", (request) => {
    requests.push({ type: request.resourceType(), url: request.url() });
  });
  await installHarness(page);

  expect(requests.length).toBeGreaterThan(0);
  expect(requests.every(({ url }) => new URL(url).origin === "https://candidate.example")).toBe(true);
  expect(requests.filter(({ type }) => (
    ["font", "image", "media", "script", "stylesheet"].includes(type)
  ))).toEqual([]);
});


test("voice disables vendor RTC diagnostics before constructing a room", async ({ page }) => {
  await installHarness(page);
  const scope = await registration(page);
  await receive(page, bindingFrame(scope));
  await receive(page, composerFrame(scope));
  await page.getByRole("button", { name: "Start voice conversation" }).click();

  await expect.poll(() => page.evaluate(() => window.__livekitLogLevels || [])).toEqual(["silent"]);
});


test("C0 canonical composer rejects an unknown field before advancing revision", async ({ page }) => {
  await installHarness(page);
  const scope = await registration(page);
  const positive = bindFixturePayload("C0-P1-composer", scope);
  positive.voice.visible_chat_id = CHAT_ID;
  await receive(page, positive);
  await expect(page.getByRole("button", { name: "Start voice conversation" })).toBeVisible();

  const invalid = bindFixturePayload("C0-N1-extra-field", scope);
  invalid.revision = 8;
  invalid.voice.visible_chat_id = CHAT_ID;
  await receive(page, invalid);

  const sameRevisionRecovery = structuredClone(positive);
  sameRevisionRecovery.revision = 8;
  sameRevisionRecovery.voice.controls[0].label = "Recovered start voice conversation";
  await receive(page, sameRevisionRecovery);
  await expect(page.getByRole("button", { name: "Recovered start voice conversation" })).toBeVisible();
});


test("composer revisions restart with each connection generation", async ({ page }) => {
  await installHarness(page);
  const firstScope = await registration(page);
  const generationA = composerFrame(firstScope, {}, 9000);
  generationA.voice.controls[0].label = "Generation A stale voice control";
  await receive(page, generationA);
  await expect(page.locator('[aria-label="Generation A stale voice control"]')).toHaveCount(1);

  await page.evaluate(() => window.__sockets.at(-1).close());
  await page.waitForFunction(() => window.__socketEvents.filter(
    (frame) => frame.type === "register_ui",
  ).length >= 2, null, { timeout: 8000 });
  const secondScope = await page.evaluate(() => window.__socketEvents.filter(
    (frame) => frame.type === "register_ui",
  ).at(-1));
  expect(secondScope.connection_generation).not.toBe(firstScope.connection_generation);
  await expect(page.locator('[aria-label="Generation A stale voice control"]')).toHaveCount(0);

  const generationBInitial = composerFrame(secondScope, {}, 0);
  generationBInitial.voice.controls[0].label = "Generation B revision zero voice control";
  await receive(page, generationBInitial);
  await expect(page.locator('[aria-label="Generation B revision zero voice control"]')).toHaveCount(1);

  const generationBCurrent = composerFrame(secondScope, {}, 2);
  generationBCurrent.voice.controls[0].label = "Generation B current voice control";
  await receive(page, generationBCurrent);
  await expect(page.locator('[aria-label="Generation B current voice control"]')).toHaveCount(1);

  const duplicate = composerFrame(secondScope, {}, 2);
  duplicate.voice.controls[0].label = "Duplicate revision stale voice control";
  const decreasing = composerFrame(secondScope, {}, 1);
  decreasing.voice.controls[0].label = "Decreasing revision stale voice control";
  await receive(page, duplicate);
  await receive(page, decreasing);

  await expect(page.locator('[aria-label="Generation B current voice control"]')).toHaveCount(1);
  await expect(page.locator('[aria-label="Duplicate revision stale voice control"]')).toHaveCount(0);
  await expect(page.locator('[aria-label="Decreasing revision stale voice control"]')).toHaveCount(0);
});


test("C4 canonical language policy rejects an inconsistent English disposition", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  const accepted = bindFixturePayload("C4-P1-en", scope);
  accepted.result_id = "canonical-result";
  await receive(page, accepted);

  const invalid = bindFixturePayload("C4-N1-en-wrong-policy", scope);
  invalid.result_id = "poison-result";
  await receive(page, invalid);

  const controls = activeControls();
  const recap = controls.find((control) => control.action === "voice_sensitive_recap_request");
  recap.visible = true;
  recap.enabled = true;
  await receive(page, composerFrame(scope, {
    state: "listening",
    foreground_active: true,
    microphone_enabled: true,
    session_id: SESSION_ID,
    generation: 1,
    media_grant_revision: 1,
    foreground_turn_id: TURN_ID,
    owner_device: { device_id: scope.device_id, device_kind: "web", generation: 1 },
    controls,
  }, 8));
  await queueResponse(page, 200, { status: "queued" });
  await page.getByRole("button", { name: "Read sensitive result" }).click();
  await page.waitForFunction(() => window.__voiceFetches.some((request) => (
    new URL(request.url).pathname.includes("/results/")
  )));
  const request = await page.evaluate(() => window.__voiceFetches.find((item) => (
    new URL(item.url).pathname.includes("/results/")
  )));
  expect(new URL(request.url).pathname).toContain("/results/canonical-result/read-consent");
});


test("C2 canonical oversized transcript is rejected at the WebSocket parser boundary", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  const oversized = bindFixturePayload("C2-N2-packet-too-large", scope);
  oversized.proof_expires_at = "2099-07-31T12:02:00Z";
  await receive(page, oversized);
  await page.waitForTimeout(50);

  expect(await page.evaluate((submissionId) => window.__socketEvents.some((frame) => (
    frame.action === "chat_message" && frame.submission_id === submissionId
  )), SUBMISSION_ID)).toBe(false);
  await expect(page.locator("#astral-voice-transcript")).toBeEmpty();
});


test("C5 canonical lifecycle rejects background microphone enablement", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  await receive(page, bindFixturePayload("C5-P1-active", scope));
  await expect(page.locator("#astral-voice-feedback")).toHaveAttribute("data-state", "listening");

  await receive(page, bindFixturePayload("C5-N1-background-mic-enabled", scope));
  await expect(page.locator("#astral-voice-feedback")).toHaveAttribute("data-state", "listening");
  expect(await page.evaluate(() => window.__voiceTrack.enabled)).toBe(true);
});


test("C6 canonical greeting requires the exact worker identity", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  const invalid = bindFixturePayload("C6-N2-unexpected-worker", scope);
  const valid = bindFixturePayload("C6-P1-greeting-null-turn", scope);
  await page.evaluate(({ invalidManifest, validManifest, workerIdentity }) => {
    const room = window.__rooms[0];
    const participant = { identity: workerIdentity };
    window.__fixturePublications = [];
    function publish(manifest) {
      const publication = {
        kind: "audio",
        trackSid: manifest.track_sid,
        trackName: manifest.track_name,
        subscriptions: [],
        setSubscribed(value) { this.subscriptions.push(value); },
      };
      window.__fixturePublications.push(publication);
      room.emit("trackPublished", publication, participant);
      room.emit(
        "dataReceived",
        new TextEncoder().encode(JSON.stringify(manifest)),
        participant,
        "reliable",
        "astraldeep.voice.announcement.v1",
      );
    }
    publish(invalidManifest);
    publish(validManifest);
  }, { invalidManifest: invalid, validManifest: valid, workerIdentity: WORKER_IDENTITY });

  await page.waitForFunction(() => window.__fixturePublications[1].subscriptions.includes(true));
  expect(await page.evaluate(() => window.__fixturePublications.map(
    (publication) => publication.subscriptions,
  ))).toEqual([[false], [false, true]]);
});


test("explicit activation uses bound REST, getUserMedia, LiveKit, and visible transcript states", async ({ page }) => {
  await installHarness(page);
  const scope = await registration(page);
  expect(scope.device_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u);
  expect(scope.device).toEqual(expect.objectContaining({
    has_microphone: true,
    has_audio_output: true,
    microphone_permission: "not_determined",
    full_duplex: true,
    transport: "livekit",
  }));
  expect(await page.evaluate(() => window.__gumCalls)).toBe(0);

  await receive(page, bindingFrame(scope));
  await receive(page, composerFrame(scope));
  await queueResponse(page, 201, sessionResponse(scope));
  const start = page.getByRole("button", { name: "Start voice conversation" });
  await expect(start).toHaveAttribute("aria-pressed", "false");
  await start.click();
  await page.waitForFunction(() => window.__gumCalls === 1);
  await page.waitForFunction(() => window.__rooms.some((room) => room.connected));

  const request = await page.evaluate(() => window.__voiceFetches[0]);
  expect(new URL(request.url).pathname).toBe("/api/voice/sessions");
  expect(request.method).toBe("POST");
  expect(request.headers.Authorization).toBe("Bearer synthetic-user-token");
  expect(request.headers["X-Astral-Device-Id"]).toBe(scope.device_id);
  expect(request.headers["X-Astral-Connection-Generation"]).toBe(scope.connection_generation);
  expect(request.headers["X-Astral-Voice-Control-Binding"]).toBe(BINDING);
  expect(request.body).toEqual(expect.objectContaining({
    device_id: scope.device_id,
    device_kind: "web",
    visible_chat_id: CHAT_ID,
    foreground_active: true,
    capability: expect.objectContaining({ microphone_permission: "authorized", transport: "livekit" }),
  }));
  expect(await page.evaluate(() => window.__rooms[0].localParticipant.published.length)).toBe(1);
  expect(await page.evaluate(() => window.__rooms[0].connectOptions)).toEqual({ autoSubscribe: false });
  expect(await page.evaluate(() => window.__voiceTrack.enabled)).toBe(true);

  await receive(page, sessionState(scope, "greeting"));
  await expect(page.locator("#astral-voice-feedback")).toHaveAttribute("data-state", "greeting");
  await expect(page.locator("#astral-voice-status")).toContainText("greeting");
  await receive(page, sessionState(scope, "listening"));
  await expect(page.locator("#astral-voice-status")).toContainText("Listening");

  await receive(page, transcriptFrame());
  await expect(page.locator("#astral-voice-transcript")).toContainText("Please review the latest result");
  await expect(page.locator("#astral-input")).toBeEnabled();
  const submission = await page.evaluate((id) => window.__socketEvents.find(
    (frame) => frame.action === "chat_message" && frame.submission_id === id,
  ), SUBMISSION_ID);
  expect(submission).toEqual(expect.objectContaining({
    type: "ui_event",
    action: "chat_message",
    session_id: CHAT_ID,
    connection_generation: scope.connection_generation,
    submission_id: SUBMISSION_ID,
    request_generation: REQUEST_ID,
    payload: expect.objectContaining({
      message: "Please review the latest result",
      chat_id: CHAT_ID,
      snapshot_purpose: "commit",
      voice_origin: expect.objectContaining({
        session_id: SESSION_ID,
        turn_id: TURN_ID,
        client_turn_id: CLIENT_TURN_ID,
        transcript_proof: "b".repeat(64),
      }),
    }),
  }));
});


test("final transcript stays bound across navigation and retries only until correlated acknowledgement", async ({ page }) => {
  await installHarness(page);
  const firstScope = await startReadyVoice(page);
  await receive(page, transcriptFrame({ chat_id: OLDER_CHAT_ID }));
  await page.waitForFunction((id) => window.__socketEvents.some(
    (frame) => frame.action === "chat_message" && frame.submission_id === id,
  ), SUBMISSION_ID);
  const firstSubmission = await page.evaluate((id) => window.__socketEvents.find(
    (frame) => frame.action === "chat_message" && frame.submission_id === id,
  ), SUBMISSION_ID);
  expect(firstSubmission.session_id).toBe(OLDER_CHAT_ID);
  expect(firstSubmission.payload.chat_id).toBe(OLDER_CHAT_ID);
  expect(firstSubmission.connection_generation).toBe(firstScope.connection_generation);
  expect(await page.evaluate((chatId) => window.__socketEvents.some(
    (frame) => frame.action === "load_chat" && frame.payload?.chat_id === chatId,
  ), OLDER_CHAT_ID)).toBe(false);

  await page.evaluate(() => window.__sockets.at(-1).close());
  await page.waitForFunction(() => window.__socketEvents.filter(
    (frame) => frame.type === "register_ui",
  ).length >= 2, null, { timeout: 8000 });
  const secondScope = await page.evaluate(() => window.__socketEvents.filter(
    (frame) => frame.type === "register_ui",
  ).at(-1));
  expect(secondScope.connection_generation).not.toBe(firstScope.connection_generation);
  await receive(page, bindingFrame(secondScope));
  await page.waitForFunction(({ id, connection }) => window.__socketEvents.some(
    (frame) => frame.action === "chat_message" && frame.submission_id === id
      && frame.connection_generation === connection,
  ), { id: SUBMISSION_ID, connection: secondScope.connection_generation });

  await receive(page, {
    type: "user_message_acked",
    schema_version: "1",
    chat_id: OLDER_CHAT_ID,
    message_id: 41,
    submission_id: SUBMISSION_ID,
    request_generation: REQUEST_ID,
    connection_generation: secondScope.connection_generation,
    voice_turn_id: TURN_ID,
  });
  const acceptedCount = await page.evaluate((id) => window.__socketEvents.filter(
    (frame) => frame.action === "chat_message" && frame.submission_id === id,
  ).length, SUBMISSION_ID);
  await page.waitForTimeout(VOICE_RETRY_SETTLE_MS);
  expect(await page.evaluate((id) => window.__socketEvents.filter(
    (frame) => frame.action === "chat_message" && frame.submission_id === id,
  ).length, SUBMISSION_ID)).toBe(acceptedCount);
  await expect(page.locator("#astral-voice-transcript")).toHaveAttribute("data-accepted", "true");
});


test("only a fully correlated submission rejection shows a persistent retry notice and stops replay", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  await receive(page, transcriptFrame());
  await page.waitForFunction((id) => window.__socketEvents.some(
    (frame) => frame.action === "chat_message" && frame.submission_id === id,
  ), SUBMISSION_ID);
  const notice = page.locator("#astral-voice-turn-notice");
  const wrong = submissionRejectedFrame(scope, {
    client_turn_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  });
  await receive(page, wrong);
  await expect(notice).toBeHidden();

  const serverMessage = "The spoken request could not be verified <safely>.";
  await receive(page, submissionRejectedFrame(scope, { message: serverMessage }));
  await expect(notice).toBeVisible();
  await expect(notice).toHaveAttribute("role", "alert");
  await expect(notice).toHaveAttribute("data-state", "refused");
  await expect(page.locator("#astral-voice-turn-notice-title"))
    .toHaveText("Voice request did not start.");
  await expect(page.locator("#astral-voice-turn-notice-message"))
    .toHaveText(serverMessage);
  await expect(page.locator("#astral-voice-turn-notice-message *")).toHaveCount(0);
  await expect(page.locator("#astral-voice-turn-notice-guidance"))
    .toHaveText("Please say it again, or use typed chat.");
  await expect(page.locator("#astral-input")).toBeEnabled();

  const terminalCount = await page.evaluate((id) => window.__socketEvents.filter(
    (frame) => frame.action === "chat_message" && frame.submission_id === id,
  ).length, SUBMISSION_ID);
  await page.waitForTimeout(VOICE_RETRY_SETTLE_MS);
  expect(await page.evaluate((id) => window.__socketEvents.filter(
    (frame) => frame.action === "chat_message" && frame.submission_id === id,
  ).length, SUBMISSION_ID)).toBe(terminalCount);
});


for (const scenario of [
  { mode: "deny", reason: "permission_denied", text: "permission" },
  { mode: "missing", reason: "no_microphone", text: "microphone" },
]) {
  test(`${scenario.mode} keeps typed chat available without opening a voice session`, async ({ page }) => {
    await installHarness(page, { mediaMode: scenario.mode });
    const scope = await registration(page);
    await receive(page, bindingFrame(scope));
    await receive(page, composerFrame(scope));
    await page.getByRole("button", { name: "Start voice conversation" }).click();

    await expect(page.locator("#astral-voice-feedback")).toHaveAttribute("data-reason", scenario.reason);
    await expect(page.locator("#astral-voice-status")).toContainText(scenario.text, { ignoreCase: true });
    await expect(page.locator("#astral-input")).toBeEnabled();
    expect(await page.evaluate(() => window.__voiceFetches.length)).toBe(0);
    expect(await page.evaluate(() => window.__rooms.some((room) => room.connected))).toBe(false);
  });
}


test("fresh-chat activation hydrates, adopts its first commit, and refreshes history", async ({ page }) => {
  await installHarness(page, { selectedChat: false });
  await page.locator("#astral-history").evaluate((node) => {
    node.textContent = "No conversations yet.";
  });
  const scope = await registration(page);
  await receive(page, bindingFrame(scope));
  await receive(page, composerFrame(scope, { visible_chat_id: null }));
  await page.getByRole("button", { name: "Start voice conversation" }).click();

  const newChat = await page.evaluate(() => window.__socketEvents.find((frame) => (
    frame.type === "ui_event" && frame.action === "new_chat" && frame.schema_version === "1"
  )));
  expect(newChat.payload).toEqual({
    schema_version: "1",
    connection_generation: scope.connection_generation,
    submission_id: newChat.submission_id,
    request_generation: newChat.request_generation,
  });
  expect(await page.evaluate(() => window.__gumCalls)).toBe(0);

  await receive(page, {
    type: "chat_created",
    schema_version: "1",
    connection_generation: scope.connection_generation,
    submission_id: "88888888-8888-4888-8888-888888888888",
    request_generation: newChat.request_generation,
    payload: {
      schema_version: "1",
      chat_id: CHAT_ID,
      from_message: false,
      connection_generation: scope.connection_generation,
      submission_id: "88888888-8888-4888-8888-888888888888",
      request_generation: newChat.request_generation,
    },
  });
  expect(await page.evaluate(() => window.__gumCalls)).toBe(0);

  await receive(page, {
    type: "chat_created",
    schema_version: "1",
    connection_generation: scope.connection_generation,
    submission_id: newChat.submission_id,
    request_generation: newChat.request_generation,
    payload: {
      schema_version: "1",
      chat_id: CHAT_ID,
      from_message: false,
      connection_generation: scope.connection_generation,
      submission_id: newChat.submission_id,
      request_generation: newChat.request_generation,
    },
  });
  await page.waitForFunction(() => window.__socketEvents.some((frame) => (
    frame.type === "ui_event" && frame.action === "load_chat" && frame.payload?.chat_id === "11111111-1111-4111-8111-111111111111"
  )));
  expect(await page.evaluate(() => window.__gumCalls)).toBe(0);

  const load = await page.evaluate(() => window.__socketEvents.findLast((frame) => (
    frame.type === "ui_event" && frame.action === "load_chat"
  )));
  await queueResponse(page, 201, sessionResponse(scope));
  await receive(page, {
    type: "conversation_snapshot",
    schema_version: 1,
    snapshot_id: "99999999-9999-4999-8999-999999999999",
    chat_id: CHAT_ID,
    connection_generation: scope.connection_generation,
    request_generation: load.request_generation,
    snapshot_purpose: "hydration",
    render_revision: 0,
    committed_at: "2026-07-31T12:00:00Z",
    transcript: [],
    canvas: { target: "canvas", components: [] },
  });
  await page.waitForFunction(() => window.__rooms.some((room) => room.connected));
  expect(await page.evaluate(() => window.__gumCalls)).toBe(1);

  await receive(page, {
    type: "conversation_commit_ready",
    schema_version: 1,
    chat_id: CHAT_ID,
    connection_generation: scope.connection_generation,
    request_generation: REQUEST_ID,
    render_revision: 1,
  });
  await receive(page, {
    type: "conversation_snapshot",
    schema_version: 1,
    snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    chat_id: CHAT_ID,
    connection_generation: scope.connection_generation,
    request_generation: REQUEST_ID,
    snapshot_purpose: "commit",
    render_revision: 1,
    committed_at: "2026-07-31T12:00:01Z",
    transcript: [{
      message_id: "voice-accepted-message",
      role: "user",
      created_at: "2026-07-31T12:00:01Z",
      parts: [{ type: "text", text: "Accepted spoken request" }],
      attachments: [],
    }],
    canvas: { target: "canvas", components: [] },
  });
  await receive(page, {
    type: "ui_render",
    target: "history",
    html: `<button type="button" data-chat-id="${CHAT_ID}">New Chat</button>`,
  });

  await expect(page.locator("#astral-chat")).toContainText("Accepted spoken request");
  await expect(page.locator("#astral-history")).toContainText("New Chat");
  await expect(page.locator("#astral-history")).not.toContainText("No conversations yet");
  await expect(page.locator("#astral-status")).toHaveText("");
});


test("takeover is explicit and rotates to the returned generation", async ({ page }) => {
  await installHarness(page);
  const scope = await registration(page);
  await receive(page, bindingFrame(scope));
  await receive(page, composerFrame(scope));
  await queueResponse(page, 409, {
    code: "voice_takeover_required",
    message: "Voice is active on another device",
    owner: {
      session_id: SESSION_ID,
      device_kind: "macos",
      generation: 1,
      media_grant_revision: 1,
      started_at: "2026-07-31T12:00:00Z",
    },
  });
  await page.getByRole("button", { name: "Start voice conversation" }).click();
  await expect(page.locator("#astral-voice-status")).toContainText("another device");

  const controls = structuredClone(fixtureComposer.voice.controls).map((control) => ({
    ...control,
    visible: control.action === "voice_session_takeover",
    enabled: control.action === "voice_session_takeover",
  }));
  await receive(page, composerFrame(scope, {
    state: "off",
    reason: "takeover_required",
    session_id: SESSION_ID,
    generation: 1,
    media_grant_revision: 1,
    owner_device: {
      device_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      device_kind: "macos",
      generation: 1,
    },
    controls,
  }, 8));
  await queueResponse(page, 200, sessionResponse(scope, { generation: 2 }));
  await page.getByRole("button", { name: "Take over voice session" }).click();
  await page.waitForFunction(() => window.__voiceFetches.some((request) => request.url.endsWith("/takeover")));
  const takeover = await page.evaluate(() => window.__voiceFetches.find((request) => request.url.endsWith("/takeover")));
  expect(takeover.body).toEqual(expect.objectContaining({
    expected_generation: 1,
    expected_media_grant_revision: 1,
    foreground_active: true,
  }));
  await page.waitForFunction(() => window.__rooms.some((room) => room.connected));
});


test("permission revocation, explicit stop, and server idle end tear down only media", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  await page.evaluate(() => window.__voiceTrack.end());
  await expect(page.locator("#astral-voice-feedback")).toHaveAttribute("data-reason", "permission_denied");
  expect(await page.evaluate(() => window.__rooms[0].disconnected)).toBe(true);
  await expect(page.locator("#astral-input")).toBeEnabled();

  // Re-establish synthetic media, then exercise the explicit server-owned end
  // control and verify accepted work is not represented as cancelled locally.
  await page.evaluate(() => {
    window.__voiceTrack.readyState = "live";
    window.__voiceTrack.enabled = true;
  });
  await receive(page, composerFrame(scope, {
    state: "listening",
    foreground_active: true,
    microphone_enabled: true,
    session_id: SESSION_ID,
    generation: 1,
    media_grant_revision: 1,
    owner_device: { device_id: scope.device_id, device_kind: "web", generation: 1 },
    controls: activeControls(),
  }, 8));
  const deletesBeforeStop = await page.evaluate(() => window.__voiceFetches.filter((request) => request.method === "DELETE").length);
  await queueResponse(page, 204, null);
  await page.getByRole("button", { name: "End voice conversation" }).click();
  await page.waitForFunction((prior) => (
    window.__voiceFetches.filter((request) => request.method === "DELETE").length > prior
  ), deletesBeforeStop);
  await expect(page.locator("#astral-voice-status")).toContainText("ended");

  await receive(page, sessionState(scope, "ended", "idle_expired", {
    foreground_active: false,
    microphone_enabled: false,
  }));
  await expect(page.locator("#astral-voice-status")).toContainText("idle");
  await expect(page.locator("#astral-input")).toBeEnabled();
  expect(await page.locator("#astral-chat").textContent()).not.toContain("cancelled");
});


test("terminal voice-request failures remain prominent, assertive, and preserve safe server text", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  const notice = page.locator("#astral-voice-turn-notice");
  const title = page.locator("#astral-voice-turn-notice-title");
  const detail = page.locator("#astral-voice-turn-notice-message");
  const scenarios = [
    ["failed", "Voice request did not complete."],
    ["refused", "Voice request did not start."],
    ["cancelled", "Voice request did not complete because it was cancelled."],
    ["abandoned", "Voice request did not complete."],
  ];

  for (const [index, [state, expectedTitle]] of scenarios.entries()) {
    const serverMessage = state === "abandoned"
      ? undefined
      : `Server detail ${index}: retry <safely> & keep typing.`;
    await receive(page, turnState(scope, state, {
      message: serverMessage,
      sequence: index + 1,
    }));
    await expect(notice).toBeVisible();
    await expect(notice).toHaveAttribute("role", "alert");
    await expect(notice).toHaveAttribute("aria-live", "assertive");
    await expect(notice).toHaveAttribute("data-state", state);
    await expect(title).toHaveText(expectedTitle);
    if (serverMessage === undefined) {
      await expect(detail).toBeHidden();
    } else {
      await expect(detail).toHaveText(serverMessage);
      await expect(detail.locator("*")).toHaveCount(0);
    }
    await expect(page.locator("#astral-input")).toBeEnabled();
  }

  await receive(page, sessionState(scope, "listening"));
  await expect(notice).toBeVisible();
  await expect(title).toHaveText("Voice request did not complete.");
  await expect(page.locator("#astral-input")).toBeEnabled();

  const retryTurnId = "99999999-9999-4999-8999-999999999999";
  await receive(page, turnState(scope, "processing", {
    turnId: retryTurnId,
    occurredAt: "2026-07-31T12:00:02Z",
  }));
  await expect(notice).toBeHidden();
  await receive(page, turnState(scope, "succeeded", {
    turnId: retryTurnId,
    sequence: 2,
    occurredAt: "2026-07-31T12:00:03Z",
  }));
  await expect(notice).toBeHidden();
});


test("server speech_error preserves its detail while identifying possible text output", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  await receive(page, sessionState(scope, "error", "speech_error", {
    message: "Assistant audio stopped.",
    microphone_enabled: false,
  }));

  await expect(page.locator("#astral-voice-status")).toHaveText(
    "Assistant audio stopped. The text result may still be available in chat.",
  );
  await expect(page.locator("#astral-voice-turn-notice")).toBeHidden();
  await expect(page.locator("#astral-input")).toBeEnabled();
});


test("out-of-contract terminal message cannot replace the persistent request notice", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  const notice = page.locator("#astral-voice-turn-notice");
  await receive(page, turnState(scope, "failed", { message: "Safe terminal detail." }));
  await expect(notice).toBeVisible();

  await receive(page, turnState(scope, "refused", {
    message: "x".repeat(241),
    sequence: 2,
  }));
  await expect(notice).toHaveAttribute("data-state", "failed");
  await expect(page.locator("#astral-voice-turn-notice-message"))
    .toHaveText("Safe terminal detail.");
});


for (const scenario of [
  {
    name: "a successful server response",
    status: 202,
    body: null,
    terminal: "fetch:resolved",
    rejects: false,
  },
  {
    name: "a failed fetch",
    status: null,
    body: null,
    terminal: "fetch:failed",
    rejects: true,
  },
]) {
  test(`stop speech fences local playout before ${scenario.name}`, async ({ page }) => {
    await installHarness(page);
    const scope = await startReadyVoice(page);
    const controls = activeControls().map((control) => (
      control.action === "voice_speech_stop"
        ? { ...control, visible: true, enabled: true }
        : control
    ));
    await receive(page, composerFrame(scope, {
      state: "speaking_progress",
      reason: "ready",
      session_id: SESSION_ID,
      generation: 1,
      media_grant_revision: 1,
      owner_device: { device_id: scope.device_id, device_kind: "web", generation: 1 },
      controls,
    }, 8));
    await startLongVoicePlayout(page);
    if (scenario.rejects) await queueRejectedResponse(page, 300);
    else await queueResponse(page, scenario.status, scenario.body, 300);

    await page.getByRole("button", { name: "Stop speaking" }).click();
    await page.waitForFunction(() => window.__stopTimeline.includes("fetch:start"));
    const localFence = await page.evaluate(() => ({
      timeline: [...window.__stopTimeline],
      processorConnected: window.__voiceProcessors[0].connected,
      subscriptions: [...window.__stopPublication.subscriptions],
    }));
    expect(localFence.timeline).toContain("playout:detach");
    expect(localFence.timeline.indexOf("playout:detach"))
      .toBeLessThan(localFence.timeline.indexOf("fetch:start"));
    expect(localFence.timeline).not.toContain("fetch:resolved");
    expect(localFence.timeline).not.toContain("fetch:failed");
    expect(localFence.processorConnected).toBe(false);
    expect(localFence.subscriptions.at(-1)).toBe(false);

    await page.waitForFunction((terminal) => window.__stopTimeline.includes(terminal), scenario.terminal);
    if (!scenario.rejects) {
      await expect(page.locator("#astral-voice-feedback"))
        .toHaveAttribute("data-state", "speaking_progress");
    } else {
      await expect(page.locator("#astral-voice-feedback")).toHaveAttribute("data-state", "error");
      await expect(page.locator("#astral-voice-feedback"))
        .toHaveAttribute("data-reason", "speech_error");
      await expect(page.locator("#astral-voice-status"))
        .toContainText("text result may still be available", { ignoreCase: true });
      await expect(page.locator("#astral-voice-turn-notice")).toBeHidden();
    }
    const request = await page.evaluate(() => window.__voiceFetches.find((item) => (
      new URL(item.url).pathname.endsWith("/speech/stop")
    )));
    expect(request.method).toBe("POST");
    expect(request.body).toEqual({
      expected_generation: 1,
      expected_media_grant_revision: 1,
    });
  });
}


test("autoplay denial exposes an explicit accessible resume action", async ({ page }) => {
  await installHarness(page, { audioBlocked: true });
  await startReadyVoice(page);
  const resume = page.getByRole("button", { name: "Enable voice audio" });
  await expect(resume).toBeVisible();
  await page.evaluate(() => { window.__audioBlocked = false; });
  await resume.click();
  await expect(resume).toBeHidden();
  expect(await page.evaluate(() => window.__rooms[0].startAudioCalls)).toBeGreaterThan(1);
});


test("visibility suspension refreshes credential-free state and rejoins without missed-audio autoplay", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  await queueResponse(page, 200, sessionResponse(scope, {
    state: "suspended",
    foregroundActive: false,
    microphoneEnabled: false,
  }).session);
  await setVisibility(page, "hidden");
  await page.waitForFunction(() => window.__voiceFetches.some((request) => (
    request.method === "PATCH" && request.body?.foreground_active === false
  )));

  expect(await page.evaluate(() => window.__rooms[0].disconnected)).toBe(true);
  await expect(page.locator("#astral-voice-feedback")).toHaveAttribute("data-state", "suspended");
  const suspend = await page.evaluate(() => window.__voiceFetches.find((request) => (
    request.method === "PATCH" && request.body?.foreground_active === false
  )));
  expect(suspend.body).toEqual(expect.objectContaining({
    expected_generation: 1,
    expected_media_grant_revision: 1,
    foreground_active: false,
    foreground_reason: "backgrounded",
    microphone_enabled: false,
  }));

  await queueResponse(page, 200, credentialFreeGrantState(scope, {
    state: "suspended",
    foregroundActive: false,
    microphoneEnabled: false,
  }));
  await queueRefreshResponse(page, 201, refreshedGrant(scope, { revision: 2 }));
  await queueResponse(page, 200, sessionResponse(scope, { revision: 2 }).session);
  await setVisibility(page, "visible");
  await page.waitForFunction(() => window.__rooms.length === 2 && window.__rooms[1].connected);

  const grantRequests = await page.evaluate(() => window.__voiceFetches.filter((request) => (
    new URL(request.url).pathname.endsWith("/media-grants")
  )));
  expect(grantRequests.map((request) => request.method)).toEqual(["GET", "POST"]);
  expect(grantRequests[0].body).toBeNull();
  expect(JSON.stringify(grantRequests[0])).not.toMatch(/join_token|participant_identity|room_name/u);
  expect(grantRequests[1].body).toEqual(expect.objectContaining({
    expected_generation: 1,
    expected_media_grant_revision: 1,
    device_id: scope.device_id,
  }));
  expect(grantRequests[1].body.refresh_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u);
  expect(await page.evaluate(() => window.__gumCalls)).toBe(2);
  expect(await page.evaluate(() => window.__rooms[1].startAudioCalls)).toBe(0);
  expect(await page.locator("#astral-voice-audio audio").count()).toBe(0);
  expect(await page.evaluate(() => window.__voiceTrack.enabled)).toBe(true);
});


test("chat navigation pauses capture and automatically rebinds the next spoken turn", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  await queueResponse(page, 200, sessionResponse(scope, {
    visibleChatId: OLDER_CHAT_ID,
  }).session, 250);
  await page.evaluate((chatId) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "astral-action";
    button.dataset.action = "load_chat";
    button.dataset.payload = JSON.stringify({ chat_id: chatId });
    document.body.appendChild(button);
    button.click();
  }, OLDER_CHAT_ID);
  await page.waitForFunction((chatId) => window.__voiceFetches.some((request) => (
    request.method === "PATCH" && request.body?.visible_chat_id === chatId
  )), OLDER_CHAT_ID);
  expect(await page.evaluate(() => window.__voiceTrack.enabled)).toBe(false);
  const update = await page.evaluate((chatId) => window.__voiceFetches.find((request) => (
    request.method === "PATCH" && request.body?.visible_chat_id === chatId
  )), OLDER_CHAT_ID);
  expect(update.body).toEqual(expect.objectContaining({
    expected_generation: 1,
    expected_media_grant_revision: 1,
    visible_chat_id: OLDER_CHAT_ID,
  }));
  await page.waitForFunction(() => window.__voiceTrack.enabled === true);
  expect(await page.evaluate((chatId) => window.__socketEvents.some((frame) => (
    frame.action === "load_chat" && frame.payload?.chat_id === chatId
  )), OLDER_CHAT_ID)).toBe(true);
});


test("a delayed stale join cannot publish through a replacement room", async ({ page }) => {
  await installHarness(page, { deferredConnect: true });
  const scope = await registration(page);
  await receive(page, bindingFrame(scope));
  await receive(page, composerFrame(scope));
  await queueResponse(page, 201, sessionResponse(scope));
  await page.getByRole("button", { name: "Start voice conversation" }).click();
  await page.waitForFunction(() => window.__rooms.length === 1 && window.__rooms[0].connectStarted);

  await queueResponse(page, 200, sessionResponse(scope, {
    state: "suspended",
    foregroundActive: false,
    microphoneEnabled: false,
  }).session);
  await setVisibility(page, "hidden");
  await page.waitForFunction(() => window.__rooms[0].disconnected);

  await queueResponse(page, 200, credentialFreeGrantState(scope, {
    state: "suspended",
    foregroundActive: false,
    microphoneEnabled: false,
  }));
  await queueRefreshResponse(page, 201, refreshedGrant(scope, { revision: 2 }));
  await queueResponse(page, 200, sessionResponse(scope, { revision: 2 }).session);
  await setVisibility(page, "visible");
  await page.waitForFunction(() => window.__rooms.length === 2 && window.__rooms[1].connectStarted);

  await page.evaluate(() => window.__rooms[0].resolveConnect());
  await page.waitForFunction(() => window.__rooms[0].connected === false);
  expect(await page.evaluate(() => window.__rooms[1].localParticipant.published.length)).toBe(0);

  await page.evaluate(() => window.__rooms[1].resolveConnect());
  await page.waitForFunction(() => window.__rooms[1].localParticipant.published.length === 1);
  expect(await page.evaluate(() => window.__rooms[1].connected)).toBe(true);
});


test("pagehide and offline stop media synchronously with bounded suspension reasons", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  await queueResponse(page, 200, sessionResponse(scope, {
    state: "suspended",
    foregroundActive: false,
    microphoneEnabled: false,
  }).session);
  await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
  await page.waitForFunction(() => window.__voiceFetches.some((request) => (
    request.method === "PATCH" && request.body?.foreground_reason === "backgrounded"
  )));
  expect(await page.evaluate(() => window.__rooms[0].disconnected)).toBe(true);
  expect(await page.evaluate(() => window.__voiceTrack.enabled)).toBe(false);

  // A network event after lifecycle suspension cannot restart or publish media.
  const roomCount = await page.evaluate(() => window.__rooms.length);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  expect(await page.evaluate(() => window.__rooms.length)).toBe(roomCount);
  await expect(page.locator("#astral-input")).toBeEnabled();
});


test("worker disconnect retries one stable refresh and preserves an unacknowledged transcript", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  await receive(page, transcriptFrame());
  const firstSubmission = await page.evaluate((id) => window.__socketEvents.find((frame) => (
    frame.action === "chat_message" && frame.submission_id === id
  )), SUBMISSION_ID);

  await queueResponse(page, 200, credentialFreeGrantState(scope));
  await queueResponse(page, 503, { code: "media_grant_apply_failed", message: "Retry", retryable: true });
  await queueRefreshResponse(page, 200, refreshedGrant(scope, { revision: 2, replayed: true }));
  await queueResponse(page, 200, sessionResponse(scope, { revision: 2 }).session);
  await page.evaluate((identity) => {
    window.__rooms[0].emit("participantDisconnected", { identity });
  }, WORKER_IDENTITY);
  await page.waitForFunction(() => window.__rooms.length === 2 && window.__rooms[1].connected);

  const refreshes = await page.evaluate(() => window.__voiceFetches.filter((request) => (
    request.method === "POST" && new URL(request.url).pathname.endsWith("/media-grants")
  )));
  expect(refreshes).toHaveLength(2);
  expect(refreshes[1].body).toEqual(refreshes[0].body);
  expect(refreshes[0].body.refresh_id).toMatch(/^[0-9a-f-]{36}$/u);
  const submissions = await page.evaluate((id) => window.__socketEvents.filter((frame) => (
    frame.action === "chat_message" && frame.submission_id === id
  )), SUBMISSION_ID);
  expect(submissions.length).toBeGreaterThanOrEqual(1);
  expect(submissions.every((frame) => (
    frame.request_generation === firstSubmission.request_generation
      && frame.payload.message === firstSubmission.payload.message
      && frame.payload.voice_origin.transcript_proof === firstSubmission.payload.voice_origin.transcript_proof
  ))).toBe(true);

  // A retained recognition-time envelope from revision 1 remains admissible
  // after the session rotates to revision 2; server proof validation is still decisive.
  await receive(page, transcriptFrame({
    turn_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    client_turn_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    submission_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    request_generation: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    sequence: 3,
  }));
  await page.waitForFunction(() => window.__socketEvents.some((frame) => (
    frame.submission_id === "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
  )));
});


test("terminal media recovery fails closed instead of leaving a zombie listening state", async ({ page }) => {
  await installHarness(page);
  const scope = await startReadyVoice(page);
  await queueResponse(page, 200, credentialFreeGrantState(scope));
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await queueResponse(page, 503, {
      code: "media_grant_apply_failed",
      message: "Voice media is still unavailable",
      retryable: true,
    });
  }
  await page.evaluate(() => window.__rooms[0].emit("disconnected"));
  await expect(page.locator("#astral-voice-feedback")).toHaveAttribute("data-state", "error", { timeout: 8000 });
  await expect(page.locator("#astral-voice-feedback")).toHaveAttribute("data-reason", "network_interrupted");
  await expect(page.locator("#astral-voice-status")).not.toContainText("Listening");
  await expect(page.locator("#astral-input")).toBeEnabled();
  await page.waitForFunction(() => window.__voiceFetches.some((request) => request.method === "DELETE"));
  expect(await page.evaluate(() => window.__voiceTrack.enabled)).toBe(false);
});


test("end remains operable during UI reconnect and re-fences a concurrent grant rotation", async ({ page }) => {
  await installHarness(page);
  const firstScope = await startReadyVoice(page);
  await receive(page, composerFrame(firstScope, {
    state: "listening",
    foreground_active: true,
    microphone_enabled: true,
    session_id: SESSION_ID,
    generation: 1,
    media_grant_revision: 1,
    owner_device: { device_id: firstScope.device_id, device_kind: "web", generation: 1 },
    controls: activeControls(),
  }, 8));
  await page.evaluate(() => window.__sockets.at(-1).close());
  const end = page.getByRole("button", { name: "End voice conversation" });
  await expect(end).toBeVisible();
  await end.click();
  await expect(page.locator("#astral-voice-status")).toContainText("ended");

  await page.waitForFunction(() => window.__socketEvents.filter(
    (frame) => frame.type === "register_ui",
  ).length >= 2, null, { timeout: 8000 });
  const secondScope = await page.evaluate(() => window.__socketEvents.filter(
    (frame) => frame.type === "register_ui",
  ).at(-1));
  expect(secondScope.connection_generation).not.toBe(firstScope.connection_generation);
  await queueResponse(page, 409, {
    code: "stale_media_grant_revision",
    message: "Voice media changed",
    retryable: false,
  });
  await queueResponse(page, 200, credentialFreeGrantState(secondScope, { revision: 2 }));
  await queueResponse(page, 204, null);
  await receive(page, bindingFrame(secondScope));
  await page.waitForFunction(() => window.__voiceFetches.filter(
    (request) => request.method === "DELETE",
  ).length === 2);
  const requests = await page.evaluate(() => window.__voiceFetches.filter(
    (item) => item.method === "DELETE",
  ));
  const request = requests[1];
  expect(request.headers["X-Astral-Connection-Generation"]).toBe(secondScope.connection_generation);
  expect(request.headers["X-Astral-Voice-Control-Binding"]).toBe(BINDING);
  expect(new URL(request.url).searchParams.get("expected_media_grant_revision")).toBe("2");
});


test("binding renewal proactively reconnects without replaying an acknowledged turn", async ({ page }) => {
  await installHarness(page);
  const firstScope = await startReadyVoice(page);
  await receive(page, transcriptFrame());
  await receive(page, {
    type: "user_message_acked",
    schema_version: "1",
    chat_id: CHAT_ID,
    message_id: 42,
    submission_id: SUBMISSION_ID,
    request_generation: REQUEST_ID,
    connection_generation: firstScope.connection_generation,
    voice_turn_id: TURN_ID,
  });
  const acceptedCount = await page.evaluate((id) => window.__socketEvents.filter((frame) => (
    frame.action === "chat_message" && frame.submission_id === id
  )).length, SUBMISSION_ID);
  await receive(page, {
    ...bindingFrame(firstScope),
    binding_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    expires_at: new Date(Date.now() + 1500).toISOString(),
  });

  await page.waitForFunction(() => window.__socketEvents.filter(
    (frame) => frame.type === "register_ui",
  ).length >= 2, null, { timeout: 8000 });
  const secondScope = await page.evaluate(() => window.__socketEvents.filter(
    (frame) => frame.type === "register_ui",
  ).at(-1));
  await queueResponse(page, 200, credentialFreeGrantState(secondScope));
  await queueRefreshResponse(page, 201, refreshedGrant(secondScope, { revision: 2 }));
  await queueResponse(page, 200, sessionResponse(secondScope, { revision: 2 }).session);
  await receive(page, bindingFrame(secondScope));
  await page.waitForFunction(() => window.__rooms.length === 2 && window.__rooms[1].connected);
  await page.waitForTimeout(VOICE_RETRY_SETTLE_MS);
  expect(await page.evaluate((id) => window.__socketEvents.filter((frame) => (
    frame.action === "chat_message" && frame.submission_id === id
  )).length, SUBMISSION_ID)).toBe(acceptedCount);
});


test("announcement manifests gate exact serialized PCM playout", async ({ page }) => {
  await installHarness(page);
  await startReadyVoice(page);
  const first = announcementFrame({
    announcementId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    sequence: 1,
    trackSid: "TR_worker_one",
    trackName: "astraldeep-announcement-one",
  });
  const second = announcementFrame({
    announcementId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    sequence: 2,
    trackSid: "TR_worker_two",
    trackName: "astraldeep-announcement-two",
  });
  await page.evaluate(({ firstManifest, secondManifest, workerIdentity }) => {
    const room = window.__rooms[0];
    const participant = { identity: workerIdentity };
    window.__voicePublications = [];
    function publish(manifest) {
      const track = {
        kind: "audio",
        sid: manifest.track_sid,
        mediaStreamTrack: {},
        detach: () => [],
      };
      const publication = {
        kind: "audio",
        trackSid: manifest.track_sid,
        trackName: manifest.track_name,
        subscriptions: [],
        setSubscribed(value) {
          this.subscriptions.push(value);
          if (value) queueMicrotask(() => room.emit("trackSubscribed", track, publication, participant));
        },
      };
      window.__voicePublications.push(publication);
      room.emit("trackPublished", publication, participant);
      room.emit(
        "dataReceived",
        new TextEncoder().encode(JSON.stringify(manifest)),
        participant,
        "reliable",
        "astraldeep.voice.announcement.v1",
      );
    }
    publish(firstManifest);
    publish(secondManifest);
  }, { firstManifest: first, secondManifest: second, workerIdentity: WORKER_IDENTITY });

  await page.waitForFunction(() => window.__voiceProcessors.length === 1);
  expect(await page.evaluate(() => window.__voicePublications.map((value) => value.subscriptions)))
    .toEqual([[false, true], [false]]);
  await page.evaluate(() => window.__voiceProcessors[0].pump());
  await page.waitForFunction(() => window.__voiceProcessors.length === 2);
  const trimmed = await page.evaluate(() => window.__voiceProcessors[0].lastOutput);
  expect(trimmed.slice(0, 100).every((value) => value === 0.25)).toBe(true);
  expect(trimmed.slice(100).every((value) => value === 0)).toBe(true);
  expect(await page.evaluate(() => window.__voicePublications.map((value) => value.subscriptions)))
    .toEqual([[false, true, false], [false, true]]);
  await page.evaluate(() => window.__voiceProcessors[1].pump());
  await page.waitForFunction(() => window.__socketEvents.filter(
    (frame) => frame.type === "voice_playout_event",
  ).length === 4);
  const events = await page.evaluate(() => window.__socketEvents.filter(
    (frame) => frame.type === "voice_playout_event",
  ).map((frame) => [frame.announcement_id, frame.phase]));
  expect(events).toEqual([
    [first.announcement_id, "started"],
    [first.announcement_id, "finished"],
    [second.announcement_id, "started"],
    [second.announcement_id, "finished"],
  ]);
});


test("logout tears down foreground media and ends the voice session without cancelling text work", async ({ page }) => {
  await installHarness(page);
  await startReadyVoice(page);
  await queueResponse(page, 204, null);
  await page.evaluate(() => {
    document.getElementById("logout").addEventListener("click", (event) => event.preventDefault());
    document.getElementById("logout").click();
  });
  await page.waitForFunction(() => window.__voiceFetches.some((request) => request.method === "DELETE"));
  expect(await page.evaluate(() => window.__rooms[0].disconnected)).toBe(true);
  expect(await page.evaluate(() => sessionStorage.getItem("astraldeep.token"))).toBeNull();
  await expect(page.locator("#astral-input")).toBeEnabled();
});
