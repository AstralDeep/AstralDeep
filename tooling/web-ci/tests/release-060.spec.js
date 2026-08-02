import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";

import { expect, test } from "@playwright/test";

import { convertPlaywrightV8Coverage } from "../coverage-conversion.mjs";


const PROMPT = "Roll exactly six six-sided dice and show the normalized results.";
const REQUIRED_LIFECYCLE_STATES = new Set(["starting", "online", "updating", "failed", "offline"]);
// Quickstart §5 / SC-006: a reload must restore the committed conversation
// within five seconds; the reconnect_resume rate below is measured against it.
const RESUME_CONTRACT_MS = 5_000;
const VOICE_RUNTIME_FIELDS = new Set([
  "voice_worker_image_reference",
  "voice_worker_image_sha256",
  "livekit_image_reference",
  "livekit_image_sha256",
  "livekit_config_sha256",
  "livekit_public_url",
  "livekit_turn_domain",
  "speech_profile",
]);
const SPEECH_PROFILE_FIELDS = new Set([
  "asr_model",
  "tts_model",
  "voice",
  "sample_rate_hz",
  "inventory_sha256",
  "profile_sha256",
]);
const ASR_MODEL = "Systran/faster-whisper-large-v3";
const TTS_MODEL = "speaches-ai/Kokoro-82M-v1.0-ONNX";
const TTS_VOICE = "af_heart";
const TTS_SAMPLE_RATE_HZ = 24000;


function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the qualifying browser lane`);
  return value;
}


function canonicalSha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}


async function atomicBytes(path, bytes) {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${process.pid}.${randomUUID()}.tmp`;
  await writeFile(temporary, bytes, { flag: "wx", mode: 0o600 });
  await rename(temporary, path);
  return canonicalSha256(bytes);
}


async function atomicJson(path, value) {
  return atomicBytes(path, Buffer.from(`${JSON.stringify(value, null, 2)}\n`, "utf8"));
}


/** Build one schema-shaped quantitative measurement, failing a missed floor. */
function measurement(metric, aggregation, value, unit, sampleCount, comparator, threshold) {
  const satisfied = {
    eq: value === threshold,
    gt: value > threshold,
    gte: value >= threshold,
    lt: value < threshold,
    lte: value <= threshold,
  }[comparator];
  if (
    !Number.isFinite(value) || value < 0
    || !Number.isInteger(sampleCount) || sampleCount < 1
    || satisfied !== true
  ) {
    throw new Error(
      `quantitative release floor missed: ${metric}=${value} must satisfy ${comparator} ${threshold}`,
    );
  }
  return { metric, aggregation, value, unit, sample_count: sampleCount, comparator, threshold };
}


function normalizeBaseUrl(raw) {
  const parsed = new URL(raw);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password
      || parsed.search || parsed.hash
      || ["localhost", "127.0.0.1", "::1"].includes(parsed.hostname.toLowerCase())) {
    throw new Error("ASTRAL_RELEASE_BASE_URL must be non-loopback HTTPS without credentials or request data");
  }
  return raw.replace(/\/+$/u, "");
}


function hasExactKeys(value, expected) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).length === expected.size
    && Object.keys(value).every((field) => expected.has(field));
}


function validateVoiceRuntime(value) {
  if (!hasExactKeys(value, VOICE_RUNTIME_FIELDS)) {
    throw new Error("trusted voice_runtime has a missing, extra, or non-object field");
  }
  const profile = value.speech_profile;
  if (!hasExactKeys(profile, SPEECH_PROFILE_FIELDS)) {
    throw new Error("trusted speech_profile has a missing, extra, or non-object field");
  }
  if (profile.asr_model !== ASR_MODEL || profile.tts_model !== TTS_MODEL
      || profile.voice !== TTS_VOICE || profile.sample_rate_hz !== TTS_SAMPLE_RATE_HZ) {
    throw new Error("trusted speech_profile differs from the exact voice profile");
  }
  const digest = /^[0-9a-f]{64}$/u;
  for (const field of [
    "voice_worker_image_sha256",
    "livekit_image_sha256",
    "livekit_config_sha256",
  ]) {
    if (typeof value[field] !== "string" || !digest.test(value[field])) {
      throw new Error(`trusted voice_runtime ${field} is not a SHA-256 digest`);
    }
  }
  for (const field of ["inventory_sha256", "profile_sha256"]) {
    if (typeof profile[field] !== "string" || !digest.test(profile[field])) {
      throw new Error(`trusted speech_profile ${field} is not a SHA-256 digest`);
    }
  }
  for (const field of [
    "voice_worker_image_reference",
    "livekit_image_reference",
    "livekit_public_url",
    "livekit_turn_domain",
  ]) {
    if (typeof value[field] !== "string" || value[field].trim() === "") {
      throw new Error(`trusted voice_runtime ${field} is empty`);
    }
  }
}


function stagingEnvironment(stage, baseUrl) {
  const required = [
    "authentication_posture",
    "candidate_image_reference",
    "candidate_image_sha256",
    "database_posture",
    "deployed_at",
    "deployment_run_id",
    "endpoint",
    "environment_id",
    "fixture_manifest_sha256",
    "keycloak_realm_sha256",
    "macos_personal_agent_host",
    "migrated_schema_revision",
    "representative_dataset_sha256",
    "source_schema_revision",
    "topology",
    "voice_runtime",
    "worker_paths",
  ];
  for (const field of required) {
    if (stage[field] === undefined || stage[field] === null) {
      throw new Error(`trusted staging output is missing ${field}`);
    }
  }
  if (stage.endpoint.replace(/\/+$/u, "") !== baseUrl) {
    throw new Error("browser base URL differs from the staged endpoint");
  }
  if (!Array.isArray(stage.worker_paths) || !stage.worker_paths.includes("voice")) {
    throw new Error("trusted staging output does not include the voice worker path");
  }
  validateVoiceRuntime(stage.voice_runtime);
  return Object.fromEntries(required.map((field) => [field, stage[field]]));
}


function runnerIdentity(imageRef) {
  const os = requiredEnvironment("RUNNER_OS").toLowerCase();
  const rawArchitecture = requiredEnvironment("RUNNER_ARCH").toLowerCase();
  const architecture = { x64: "x86_64", x86_64: "x86_64", arm64: "arm64" }[rawArchitecture];
  const runnerEnvironment = requiredEnvironment("ASTRAL_RUNNER_ENVIRONMENT");
  if (os !== "linux" || !architecture
      || !["github_hosted", "self_hosted"].includes(runnerEnvironment)) {
    throw new Error("browser runner identity is outside the release schema");
  }
  return {
    os,
    architecture,
    runner_image: imageRef,
    runner_name: requiredEnvironment("RUNNER_NAME"),
    runner_environment: runnerEnvironment,
  };
}


function workflowIdentity() {
  const attempt = Number.parseInt(requiredEnvironment("GITHUB_RUN_ATTEMPT"), 10);
  if (!Number.isSafeInteger(attempt) || attempt < 1) throw new Error("GITHUB_RUN_ATTEMPT is invalid");
  return {
    name: requiredEnvironment("GITHUB_WORKFLOW"),
    run_id: requiredEnvironment("GITHUB_RUN_ID"),
    run_attempt: attempt,
    job_id: requiredEnvironment("GITHUB_JOB"),
  };
}


function passedCheck(id, durationMs, evidenceArtifacts, measurements = []) {
  return {
    id,
    outcome: "passed",
    duration_ms: Math.max(0, Math.round(durationMs)),
    detail_code: null,
    applicability_reason: null,
    measurements,
    evidence_artifacts: evidenceArtifacts,
  };
}


/** Map one Playwright coverage entry to its maintained candidate source path. */
function maintainedSourcePath(entry, appOrigin) {
  if (typeof entry?.url !== "string") return null;
  let parsed;
  try {
    parsed = new URL(entry.url);
  } catch {
    return null;
  }
  if (parsed.origin !== appOrigin || !parsed.pathname.startsWith("/static/")) return null;
  const sourcePath = `backend/webrender${parsed.pathname}`;
  if (
    sourcePath.includes("/static/vendor/")
    || sourcePath.endsWith(".min.js")
    || !/\.(?:js|mjs)$/u.test(sourcePath)
  ) {
    return null;
  }
  return sourcePath;
}


/** Merge per-navigation V8 entries additively, like the coverage:node producer. */
function mergedMaintainedEntries(rawCoverage, appOrigin) {
  const merged = new Map();
  for (const entry of rawCoverage) {
    const sourcePath = maintainedSourcePath(entry, appOrigin);
    if (sourcePath === null) continue;
    if (typeof entry.source !== "string") {
      throw new Error(`maintained coverage entry lacks source text: ${sourcePath}`);
    }
    const record = merged.get(sourcePath) ?? { source: entry.source, ranges: new Map() };
    if (record.source !== entry.source) {
      throw new Error(`candidate source changed between navigations: ${sourcePath}`);
    }
    for (const functionCoverage of entry.functions ?? []) {
      for (const { startOffset, endOffset, count } of functionCoverage.ranges ?? []) {
        const key = `${startOffset}:${endOffset}`;
        const mergedCount = (record.ranges.get(key)?.count ?? 0) + count;
        if (!Number.isSafeInteger(mergedCount)) {
          throw new Error(`coverage count overflow: ${sourcePath}`);
        }
        record.ranges.set(key, { startOffset, endOffset, count: mergedCount });
      }
    }
    merged.set(sourcePath, record);
  }
  return [...merged.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([sourcePath, record]) => ({
      sourcePath,
      source: record.source,
      functions: [{ ranges: [...record.ranges.values()] }],
    }));
}


async function installWireObserver(page) {
  const observed = {
    agentLifecycle: [],
    chatStatuses: [],
    operationStatuses: [],
    registerTokens: [],
    snapshots: [],
    socketCloses: 0,
    socketOpens: 0,
  };
  page.on("websocket", (socket) => {
    observed.socketOpens += 1;
    socket.on("framesent", ({ payload }) => {
      try {
        const frame = JSON.parse(payload);
        if (frame.type === "register_ui") observed.registerTokens.push(frame.token);
      } catch {
        // Non-JSON frames cannot be authentication registrations.
      }
    });
    socket.on("close", () => { observed.socketCloses += 1; });
    socket.on("framereceived", ({ payload }) => {
      let frame;
      try {
        frame = JSON.parse(payload);
      } catch {
        return;
      }
      if (frame.type === "operation_status") {
        observed.operationStatuses.push({
          action: frame.action,
          operationId: frame.operation_id,
          sequence: frame.sequence,
          state: frame.state,
          terminal: frame.terminal,
        });
      } else if (frame.type === "agent_lifecycle") {
        observed.agentLifecycle.push({
          agentId: frame.agent_id,
          generation: frame.lifecycle_generation,
          revision: frame.state_revision,
          state: frame.state,
        });
      } else if (frame.type === "conversation_snapshot") {
        observed.snapshots.push({
          chatId: frame.chat_id,
          purpose: frame.snapshot_purpose,
          renderRevision: frame.render_revision,
          snapshotId: frame.snapshot_id,
        });
      } else if (frame.type === "chat_status") {
        observed.chatStatuses.push(frame.status);
      }
    });
  });
  await page.addInitScript(() => {
    Object.defineProperty(window, "__astralReleaseWelcomeAfterLocatedChat", {
      value: false,
      writable: true,
    });
    const startWelcomeGuard = () => {
      const canvas = document.querySelector("#astral-canvas");
      if (!canvas) return;
      new MutationObserver(() => {
        const located = Object.keys(localStorage).some((key) => key.startsWith("astraldeep.active_chat.v1."));
        if (located && canvas.querySelector('[data-component-id^="wel_"]')) {
          window.__astralReleaseWelcomeAfterLocatedChat = true;
        }
      }).observe(canvas, { childList: true, subtree: true });
    };
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", startWelcomeGuard, { once: true });
    } else {
      startWelcomeGuard();
    }
  });
  return observed;
}


async function seedPriorPrincipalDecoy(page, baseUrl) {
  const staleToken = `x.${Buffer.from(JSON.stringify({
    iss: "https://stale.invalid/realms/Astral",
    sub: "prior-principal",
  })).toString("base64url")}.x`;
  const origin = new URL(baseUrl).origin;
  await page.addInitScript(({ expectedOrigin, value }) => {
    if (location.origin === expectedOrigin) {
      sessionStorage.setItem("astraldeep.token", value);
    }
  }, { expectedOrigin: origin, value: staleToken });
  return staleToken;
}


function jwtSubject(token) {
  const payload = token.split(".")[1];
  if (!payload) return null;
  return JSON.parse(Buffer.from(payload, "base64url").toString("utf8")).sub ?? null;
}


async function verifyCurrentSessionOwnsWebSocket(page, wire, staleToken) {
  const session = await page.evaluate(async () => {
    const response = await fetch("/auth/session", { credentials: "same-origin" });
    return response.json();
  });
  expect(session.authenticated).toBe(true);
  await expect.poll(() => wire.registerTokens.length, { timeout: 30_000 }).toBeGreaterThan(0);
  expect(wire.registerTokens[0]).toBe(session.access_token);
  expect(wire.registerTokens[0]).not.toBe(staleToken);
  expect(jwtSubject(wire.registerTokens[0])).toBe(session.user_id);
}


async function signInThroughKeycloak(page, baseUrl, username, password) {
  const started = performance.now();
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  const usernameField = page.locator('input[name="username"]');
  await usernameField.waitFor({ state: "visible", timeout: 30_000 });
  expect(new URL(page.url()).origin).not.toBe(new URL(baseUrl).origin);
  await usernameField.fill(username);
  await page.locator('input[name="password"]').fill(password);
  await page.locator('button[type="submit"], input[type="submit"]').first().click();
  await page.waitForURL((url) => url.origin === new URL(baseUrl).origin, { timeout: 60_000 });
  await expect(page.locator("#astral-input")).toBeVisible({ timeout: 30_000 });
  const leakedStorage = await page.evaluate(() => [...Object.keys(localStorage), ...Object.keys(sessionStorage)]
    .filter((key) => /(?:access|refresh)[_-]?token|password|secret/iu.test(key)));
  expect(leakedStorage).toEqual([]);
  return { durationMs: performance.now() - started, leakedStorageKeys: leakedStorage.length };
}


async function runRenderedChat(page) {
  const started = performance.now();
  await page.getByRole("button", { name: "New chat" }).click();
  const input = page.locator("#astral-input");
  await input.fill(PROMPT);
  await input.press("Enter");
  await expect(page.locator("#astral-chat")).toContainText(PROMPT, { timeout: 10_000 });
  const assistant = page.locator("#astral-chat > .justify-start").last();
  await expect(assistant).toBeVisible({ timeout: 240_000 });
  await expect(assistant).toContainText(/(?:six-sided|d6)/iu, { timeout: 240_000 });
  await expect(page.locator("#astral-status")).toHaveAttribute("aria-busy", "false", { timeout: 240_000 });
  await expect(page.locator("#astral-canvas-skeleton")).toHaveCount(0, { timeout: 30_000 });
  const transcript = (await page.locator("#astral-chat").innerText()).trim();
  const canvas = (await page.locator("#astral-canvas").innerText()).trim();
  expect(transcript).toMatch(/\b6\b/u);
  expect(transcript.toLowerCase()).toMatch(/(?:six-sided|d6)/u);
  return { canvas, durationMs: performance.now() - started, transcript };
}


async function runResumeTrials(page, expectedTranscript, expectedCanvas) {
  const latencies = [];
  for (let trial = 0; trial < 20; trial += 1) {
    const started = performance.now();
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect.poll(async () => (await page.locator("#astral-chat").innerText()).trim(), {
      timeout: 5_000,
    }).toBe(expectedTranscript);
    await expect.poll(async () => (await page.locator("#astral-canvas").innerText()).trim(), {
      timeout: 5_000,
    }).toBe(expectedCanvas);
    latencies.push(performance.now() - started);
  }
  expect(await page.evaluate(() => window.__astralReleaseWelcomeAfterLocatedChat)).toBe(false);
  return latencies;
}


async function openSettingsSurface(page, label) {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("menuitem", { name: label, exact: true }).click();
  await expect(page.locator("#astral-modal .astral-modal-card")).toBeVisible({ timeout: 15_000 });
}


async function runPersonalAgentAuthoring(page, wire) {
  const started = performance.now();
  await openSettingsSurface(page, "My agents");
  const name = `Release 060 ${randomUUID().slice(0, 8)}`;
  await page.locator('#astral-modal input[name="agent_name"]').fill(name);
  await page.locator('#astral-modal textarea[name="description"]').fill(
    "Greet only its owner using a deterministic local tool and no network access.",
  );
  await page.getByRole("button", { name: "Start", exact: true }).click();
  await expect(page.locator('#astral-modal textarea[name="specification"]')).toBeVisible({ timeout: 30_000 });
  await page.locator('#astral-modal textarea[name="specification"]').fill(
    "Greet the owner on request. Use one local greet tool and return a short plain-text greeting.",
  );
  await page.getByRole("button", { name: "Save & continue", exact: true }).click();
  await expect(page.locator("#astral-modal")).toContainText("Clarify", { timeout: 60_000 });
  const answers = page.locator('#astral-modal textarea[name^="q"]');
  for (let index = 0; index < await answers.count(); index += 1) {
    await answers.nth(index).fill("Use deterministic owner-only behavior with no external egress.");
  }
  await page.getByRole("button", { name: "Save & continue", exact: true }).click();
  await expect(page.locator('#astral-modal textarea[name="tools"]')).toBeVisible({ timeout: 60_000 });
  await page.locator('#astral-modal textarea[name="tools"]').fill("greet | tools:read | greet the owner");
  await page.locator('#astral-modal input[name="scopes"]').fill("tools:read");
  await page.getByRole("button", { name: "Save & continue", exact: true }).click();
  await expect(page.locator('#astral-modal textarea[name="tasks"]')).toBeVisible({ timeout: 60_000 });
  await page.locator('#astral-modal textarea[name="tasks"]').fill("Validate the request\nCall greet once\nReturn the greeting");
  await page.getByRole("button", { name: "Save & continue", exact: true }).click();
  await page.getByRole("button", { name: "Run Analyze", exact: true }).click();
  await expect(page.locator("#astral-modal")).toContainText("Analyze passed", { timeout: 60_000 });
  const statuses = wire.operationStatuses.filter((item) => item.action?.startsWith("chrome_author_"));
  const completedCount = statuses.filter((item) => item.terminal === true && item.state === "completed").length;
  const failedCount = statuses.filter((item) => item.terminal === true && item.state === "failed").length;
  expect(completedCount).toBeGreaterThan(0);
  expect(failedCount).toBe(0);
  return {
    completedCount,
    durationMs: performance.now() - started,
    failedCount,
    name,
    statusCount: statuses.length,
  };
}


async function verifyLifecycle(wire, agentId, expectedStates) {
  const started = performance.now();
  await expect.poll(() => {
    const events = wire.agentLifecycle.filter((event) => event.agentId === agentId);
    return [...new Set(events.map((event) => event.state))];
  }, { timeout: 120_000 }).toEqual(expect.arrayContaining(expectedStates));
  const events = wire.agentLifecycle.filter((event) => event.agentId === agentId);
  for (const event of events) {
    expect(Number.isSafeInteger(event.generation) && event.generation >= 0).toBe(true);
    expect(Number.isSafeInteger(event.revision) && event.revision >= 0).toBe(true);
    expect(REQUIRED_LIFECYCLE_STATES.has(event.state)).toBe(true);
  }
  return { durationMs: performance.now() - started, events };
}


async function verifyAccessibility(page) {
  const started = performance.now();
  await expect(page.getByRole("status", { name: "Application status" })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "New chat" })).toBeEnabled();
  await expect(page.locator("#astral-input")).toHaveAttribute("placeholder", /\S/u);
  const inaccessible = await page.locator("button, input:not([type=hidden]), textarea, select, a[href]").evaluateAll(
    (elements) => elements.filter((element) => {
      const style = getComputedStyle(element);
      if (style.display === "none" || style.visibility === "hidden") return false;
      const id = element.getAttribute("id");
      const label = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
      return !(
        element.getAttribute("aria-label")
        || element.getAttribute("title")
        || element.getAttribute("placeholder")
        || element.textContent?.trim()
        || label?.textContent?.trim()
      );
    }).map((element) => element.outerHTML.slice(0, 200)),
  );
  expect(inaccessible).toEqual([]);
  await page.locator("body").press("Tab");
  await expect.poll(() => page.evaluate(() => document.activeElement !== document.body)).toBe(true);
  return {
    durationMs: performance.now() - started,
    inspectedControls: await page.locator("button, input, textarea, select, a[href]").count(),
    unnamedControls: inaccessible.length,
  };
}


test("real Keycloak candidate browser release flow", async ({ context, page }) => {
  test.setTimeout(15 * 60 * 1000);
  const baseUrl = normalizeBaseUrl(requiredEnvironment("ASTRAL_RELEASE_BASE_URL"));
  const candidateSha = requiredEnvironment("ASTRAL_RELEASE_CANDIDATE_SHA");
  const output = resolve(requiredEnvironment("ASTRAL_RELEASE_OUTPUT"));
  const coverageOutput = resolve(requiredEnvironment("ASTRAL_RELEASE_COVERAGE_OUTPUT"));
  const istanbulOutput = resolve(requiredEnvironment("ASTRAL_RELEASE_COVERAGE_ISTANBUL_OUTPUT"));
  const stage = JSON.parse(await readFile(requiredEnvironment("ASTRAL_RELEASE_STAGING_FILE"), "utf8"));
  const staging = stagingEnvironment(stage, baseUrl);
  const imageRef = requiredEnvironment("ASTRAL_PLAYWRIGHT_IMAGE");
  const lifecycleAgent = requiredEnvironment("ASTRAL_RELEASE_LIFECYCLE_AGENT_ID");
  const lifecycleStates = requiredEnvironment("ASTRAL_RELEASE_LIFECYCLE_STATES").split(",").filter(Boolean);
  if (!lifecycleStates.length || lifecycleStates.some((state) => !REQUIRED_LIFECYCLE_STATES.has(state))) {
    throw new Error("ASTRAL_RELEASE_LIFECYCLE_STATES is invalid");
  }
  expect(await context.cookies()).toEqual([]);
  const wire = await installWireObserver(page);
  const stalePriorPrincipalToken = await seedPriorPrincipalDecoy(page, baseUrl);
  await page.coverage.startJSCoverage({ reportAnonymousScripts: false, resetOnNavigation: false });
  const startedAt = new Date().toISOString();
  const rawRoot = resolve(dirname(output), "web-raw");
  const rawArtifacts = new Map();
  const screenshotArtifacts = new Map();
  const writeRaw = async (name, value) => {
    const path = resolve(rawRoot, `${name}.json`);
    const sha256 = await atomicJson(path, value);
    const artifact = {
      name: `web_${name}`,
      kind: "json_metrics",
      immutable_reference: `bundle://web-raw/${basename(path)}`,
      sha256,
    };
    rawArtifacts.set(name, artifact);
    return artifact;
  };
  const writeScreenshot = async (name) => {
    const path = resolve(rawRoot, `${name}.png`);
    const sha256 = await atomicBytes(path, await page.screenshot());
    const artifact = {
      name: `web_${name}_screenshot`,
      kind: "screenshot",
      immutable_reference: `bundle://web-raw/${basename(path)}`,
      sha256,
    };
    screenshotArtifacts.set(name, artifact);
    return artifact;
  };

  const signIn = await signInThroughKeycloak(
    page,
    baseUrl,
    requiredEnvironment("ASTRAL_RELEASE_USERNAME"),
    requiredEnvironment("ASTRAL_RELEASE_PASSWORD"),
  );
  await verifyCurrentSessionOwnsWebSocket(page, wire, stalePriorPrincipalToken);
  const signInArtifact = await writeRaw("sign_in", {
    authenticatedOrigin: new URL(page.url()).origin,
    durationMs: Math.round(signIn.durationMs),
    freshContext: true,
    leakedStorageKeys: signIn.leakedStorageKeys,
    method: "keycloak_ui_authorization_code_pkce",
    sharedTabStaleTokenRejected: true,
    websocketPrincipalMatchesCookieSession: true,
  });
  const signInScreenshot = await writeScreenshot("sign_in");

  const chat = await runRenderedChat(page);
  const chatArtifact = await writeRaw("rendered_chat", {
    durationMs: Math.round(chat.durationMs),
    normalizedDiceContractObserved: true,
    promptSha256: canonicalSha256(Buffer.from(PROMPT, "utf8")),
    transcriptCharacters: chat.transcript.length,
  });
  const chatScreenshot = await writeScreenshot("rendered_chat");

  const resumeStarted = performance.now();
  const resumeLatencies = await runResumeTrials(page, chat.transcript, chat.canvas);
  const resumeDuration = performance.now() - resumeStarted;
  const resumeWithinContract = resumeLatencies.filter((value) => value <= RESUME_CONTRACT_MS).length;
  const resumeSuccessRate = (resumeWithinContract / resumeLatencies.length) * 100;
  const resumeMaxLatencyMs = Math.round(Math.max(...resumeLatencies));
  const resumeArtifact = await writeRaw("reconnect_resume", {
    contractMs: RESUME_CONTRACT_MS,
    latenciesMs: resumeLatencies.map(Math.round),
    maxLatencyMs: resumeMaxLatencyMs,
    successfulTrials: resumeWithinContract,
    trialCount: 20,
  });
  const resumeScreenshot = await writeScreenshot("reconnect_resume");

  const authoring = await runPersonalAgentAuthoring(page, wire);
  const authoringArtifact = await writeRaw("personal_agent", {
    analyzePassed: true,
    completedOperations: authoring.completedCount,
    durationMs: Math.round(authoring.durationMs),
    failedOperations: authoring.failedCount,
    generatedTestIdentitySha256: canonicalSha256(Buffer.from(authoring.name, "utf8")),
  });
  const authoringScreenshot = await writeScreenshot("personal_agent");

  const lifecycle = await verifyLifecycle(wire, lifecycleAgent, lifecycleStates);
  const distinctLifecycleStates = new Set(lifecycle.events.map((event) => event.state)).size;
  const lifecycleArtifact = await writeRaw("agent_lifecycle", {
    agentIdSha256: canonicalSha256(Buffer.from(lifecycleAgent, "utf8")),
    distinctStates: distinctLifecycleStates,
    durationMs: Math.round(lifecycle.durationMs),
    events: lifecycle.events.map(({ generation, revision, state }) => ({ generation, revision, state })),
    requiredStates: lifecycleStates,
  });
  const lifecycleScreenshot = await writeScreenshot("agent_lifecycle");

  const accessibility = await verifyAccessibility(page);
  const accessibilityArtifact = await writeRaw("accessibility_semantics", accessibility);
  const accessibilityScreenshot = await writeScreenshot("accessibility_semantics");

  const coverage = await page.coverage.stopJSCoverage();
  await atomicJson(coverageOutput, coverage);
  // The lock-pinned producer converts and executable-syntax-filters the raw V8
  // ranges into the exact Istanbul statement envelope the coverage gate parses.
  const istanbul = await convertPlaywrightV8Coverage(
    mergedMaintainedEntries(coverage, new URL(baseUrl).origin),
    (entry) => entry.sourcePath,
  );
  expect(Object.keys(istanbul.coverage)).toContain("backend/webrender/static/client.js");
  await atomicJson(istanbulOutput, istanbul);
  const completedAt = new Date().toISOString();
  const report = {
    document_type: "platform_evidence",
    schema_version: 1,
    evidence_id: randomUUID(),
    candidate_sha: candidateSha,
    release_id: requiredEnvironment("ASTRAL_RELEASE_ID"),
    release_version: requiredEnvironment("ASTRAL_RELEASE_VERSION"),
    platform: "web",
    target_description: "Backend-served web client in the pinned official Playwright Chromium image",
    artifact: {
      name: "astraldeep-web-deployment",
      kind: "web_deployment",
      immutable_reference: `oci://${staging.candidate_image_reference}`,
      sha256: staging.candidate_image_sha256,
      build_identity: `candidate-container:${candidateSha}`,
    },
    staging_environment: staging,
    runner: runnerIdentity(imageRef),
    workflow: workflowIdentity(),
    started_at: startedAt,
    completed_at: completedAt,
    outcome: "passed",
    unavailable_reason: null,
    unavailability_observation: null,
    checks: [
      passedCheck("sign_in", signIn.durationMs, [signInArtifact, signInScreenshot], [
        measurement("sign_in_duration_ms", "maximum", Math.round(signIn.durationMs), "milliseconds", 1, "lte", 180_000),
        measurement("credential_storage_leaks", "total", signIn.leakedStorageKeys, "count", 1, "eq", 0),
      ]),
      passedCheck("rendered_chat", chat.durationMs, [chatArtifact, chatScreenshot], [
        measurement("rendered_chat_duration_ms", "maximum", Math.round(chat.durationMs), "milliseconds", 1, "lte", 900_000),
        measurement("transcript_characters", "total", chat.transcript.length, "count", 1, "gte", 1),
      ]),
      passedCheck("reconnect_resume", resumeDuration, [resumeArtifact, resumeScreenshot], [
        measurement("trial_count", "total", resumeLatencies.length, "count", 20, "gte", 20),
        measurement("resume_success_rate", "rate", resumeSuccessRate, "percent", 20, "gte", 100),
        measurement("resume_latency_max_ms", "maximum", resumeMaxLatencyMs, "milliseconds", 20, "lte", RESUME_CONTRACT_MS),
      ]),
      passedCheck("agent_lifecycle", lifecycle.durationMs, [lifecycleArtifact, lifecycleScreenshot], [
        measurement("distinct_lifecycle_states", "total", distinctLifecycleStates, "count", lifecycle.events.length, "gte", lifecycleStates.length),
        measurement("lifecycle_events_observed", "total", lifecycle.events.length, "count", lifecycle.events.length, "gte", 1),
      ]),
      passedCheck("personal_agent", authoring.durationMs, [authoringArtifact, authoringScreenshot], [
        measurement("authoring_operations_completed", "total", authoring.completedCount, "count", authoring.statusCount, "gte", 1),
        measurement("authoring_operations_failed", "total", authoring.failedCount, "count", authoring.statusCount, "eq", 0),
      ]),
      passedCheck("accessibility_semantics", accessibility.durationMs, [accessibilityArtifact, accessibilityScreenshot], [
        measurement("unnamed_visible_controls", "total", accessibility.unnamedControls, "count", accessibility.inspectedControls, "eq", 0),
        measurement("inspected_controls", "total", accessibility.inspectedControls, "count", accessibility.inspectedControls, "gte", 1),
      ]),
    ],
  };
  await atomicJson(output, report);
  expect(rawArtifacts.size).toBe(6);
  expect(screenshotArtifacts.size).toBe(6);
});
