// Deterministic client reducer/continuity contract suite. This intentionally
// uses a synthetic DOM and is never the feature-060 qualifying release proof.
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";


const ROOT = resolve(import.meta.dirname, "../../..");
const CLIENT_PATH = resolve(ROOT, "backend/webrender/static/client.js");
const MANIFEST_PATH = resolve(ROOT, "backend/shared/ui_protocol.json");
const ISSUER = "https://identity.example/realms/astral";
const SUBJECT = "continuity-user";
const CHAT_ID = "11111111-1111-4111-8111-111111111111";
const OTHER_CHAT_ID = "22222222-2222-4222-8222-222222222222";
const SNAPSHOT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SNAPSHOT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const OPERATION_A = "44444444-4444-4444-8444-444444444444";
const OPERATION_B = "55555555-5555-4555-8555-555555555555";
const OPERATION_C = "66666666-6666-4666-8666-666666666666";
const OPERATION_D = "77777777-7777-4777-8777-777777777777";
const OPERATION_E = "88888888-8888-4888-8888-888888888888";
const COMMITTED_AT = "2026-07-16T12:00:00Z";
const ADMISSION_REFUSAL_CODES = [
  "capacity_exceeded",
  "registration_required",
  "registration_timeout",
  "idempotency_conflict",
  "connection_closing",
  "service_draining",
  "invalid_input",
  "registration_queue_full",
  "operation_failed",
];


test("manifest pins the exact admission refusal contract", async () => {
  const manifest = JSON.parse(await readFile(MANIFEST_PATH, "utf8"));
  expect(manifest.frame_contracts?.admission_refusal).toEqual({
    type: "error",
    exact_fields: [
      "type",
      "submission_id",
      "accepted",
      "code",
      "message",
      "retryable",
      "retry_after_ms",
    ],
    submission_id: "canonical_lowercase_uuid4",
    accepted: false,
    additional_fields: false,
    codes: ADMISSION_REFUSAL_CODES,
  });
});


function base64url(value) {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
}


const TOKEN = `${base64url({ alg: "none" })}.${base64url({ iss: ISSUER, sub: SUBJECT })}.signature`;
const OTHER_TOKEN = `${base64url({ alg: "none" })}.${base64url({ iss: ISSUER, sub: "other-user" })}.signature`;
const LOCATOR_KEY = `astraldeep.active_chat.v1.${createHash("sha256")
  .update(ISSUER, "utf8")
  .update(Buffer.from([0]))
  .update(SUBJECT, "utf8")
  .digest("hex")}`;
const OTHER_LOCATOR_KEY = `astraldeep.active_chat.v1.${createHash("sha256")
  .update(ISSUER, "utf8")
  .update(Buffer.from([0]))
  .update("other-user", "utf8")
  .digest("hex")}`;


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
      <form id="astral-form"><input id="astral-input"><button type="submit">Send</button></form>
      <div id="astral-modal"></div>
    </main>
  </body></html>`;
}


async function installHarness(page, { locator = true, url = "https://candidate.example/" } = {}) {
  await page.addInitScript(({ token }) => {
    window.__ASTRAL_TOKEN__ = token;
    window.__ASTRAL_RESUMED__ = true;
    window.__socketEvents = [];
    window.__sockets = [];
    window.requestIdleCallback = () => 0;
    window.__sessionToken = token;
    window.fetch = async () => ({
      json: async () => ({
        authenticated: true,
        access_token: window.__sessionToken,
        resumed: true,
        user_id: window.__sessionSubject || "continuity-user",
      }),
    });

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
        window.__socketEvents.push({
          frame,
          locatorAtSend: localStorage.getItem(window.__locatorKey),
        });
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
  }, { token: TOKEN });

  await page.route("https://candidate.example/**", (route) => route.fulfill({
    contentType: "text/html",
    body: htmlShell(),
  }));
  await page.goto(url);
  await page.evaluate(({ key, chatId, shouldPersist }) => {
    window.__locatorKey = key;
    if (shouldPersist) {
      localStorage.setItem(key, JSON.stringify({
        schema_version: 1,
        chat_id: chatId,
        updated_at: "2026-07-16T11:59:00Z",
      }));
    }
  }, { key: LOCATOR_KEY, chatId: CHAT_ID, shouldPersist: locator });
  const source = await readFile(CLIENT_PATH, "utf8");
  await page.addScriptTag({ content: `${source}\n//# sourceURL=https://candidate.example/static/client.js` });
  await page.waitForFunction(() => window.__socketEvents.some((event) => event.frame.type === "register_ui"));
}


async function registration(page) {
  return page.evaluate(() => window.__socketEvents.find((event) => event.frame.type === "register_ui"));
}


async function receive(page, frame) {
  await page.evaluate((value) => window.__sockets.at(-1).receive(value), frame);
}


function presentation(id, text, workspace = { export: false, share: false }) {
  return {
    type: "text",
    component_id: id,
    content: text,
    _presentation: {
      target: "web",
      html: `<div class="astral-component" data-component-id="${id}">${text}</div>`,
      workspace,
    },
  };
}


function snapshot(scope, overrides = {}) {
  return {
    type: "conversation_snapshot",
    schema_version: 1,
    snapshot_id: SNAPSHOT_A,
    chat_id: CHAT_ID,
    connection_generation: scope.connection_generation,
    request_generation: scope.request_generation || scope.resume.request_generation,
    snapshot_purpose: "hydration",
    render_revision: 0,
    committed_at: COMMITTED_AT,
    transcript: [{
      message_id: "message-1",
      role: "assistant",
      created_at: COMMITTED_AT,
      parts: [{ type: "text", text: "Committed answer" }],
      attachments: [],
    }],
    canvas: {
      target: "canvas",
      components: [presentation("rote-new", "ROTE-adapted canvas")],
    },
    ...overrides,
  };
}


function operationStatus(scope, operationId, sequence, state, overrides = {}) {
  const terminal = ["completed", "failed", "cancelled", "retryable"].includes(state);
  const isError = ["failed", "cancelled", "retryable"].includes(state);
  return {
    type: "operation_status",
    operation_id: operationId,
    action: "chat_message",
    surface: "chat",
    chat_id: CHAT_ID,
    connection_generation: scope.connection_generation,
    request_generation: scope.request_generation || scope.resume.request_generation,
    sequence,
    state,
    phase: state,
    label: state === "completed" ? "Completed" : `Active ${operationId.slice(0, 1)}`,
    terminal,
    retryable: state === "retryable",
    error: isError ? { code: "operation_failed", message: "Visible terminal failure" } : null,
    retry_after_ms: state === "retryable" ? 500 : null,
    updated_at: `2026-07-16T12:00:0${sequence}Z`,
    ...overrides,
  };
}


test("locator is present before registration and equal hydration replaces atomically", async ({ page }) => {
  await installHarness(page);
  const event = await registration(page);
  expect(JSON.parse(event.locatorAtSend).chat_id).toBe(CHAT_ID);
  expect(event.frame.connection_generation).toMatch(/^[0-9a-f-]{36}$/u);
  expect(event.frame.resume).toEqual(expect.objectContaining({
    schema_version: 1,
    active_chat_id: CHAT_ID,
  }));

  await page.evaluate(() => {
    document.querySelector("#astral-chat").innerHTML = '<div id="old-transcript">Old transcript</div>';
    document.querySelector("#astral-canvas").innerHTML = '<div id="old-canvas">Old canvas</div>';
  });
  await receive(page, snapshot(event.frame));

  await expect(page.locator("#astral-chat")).toContainText("Committed answer");
  await expect(page.locator("#astral-chat #old-transcript")).toHaveCount(0);
  await expect(page.locator("#astral-canvas")).toContainText("ROTE-adapted canvas");
  await expect(page.locator("#astral-canvas #old-canvas")).toHaveCount(0);

  // The bounded legacy acknowledgement may race behind the authoritative
  // snapshot. It must not resurrect a completed hydration indicator.
  await receive(page, {type: "chat_loaded", chat: {id: CHAT_ID}});
  await expect(page.locator("#astral-status")).toHaveText("");
  await expect(page.locator("#astral-status")).toHaveAttribute("aria-busy", "false");
});


test("operation status is visible only while active and terminal failures stay settled", async ({ page }) => {
  await installHarness(page);
  const { frame: scope } = await registration(page);
  const status = page.locator("#astral-status");

  const bootstrap = await page.evaluate(() => window.__socketEvents.find((candidate) => (
    candidate.frame.type === "ui_event" && candidate.frame.action === "get_history"
  )).frame);
  // Startup metadata is protocol-visible but never presented as user work.
  await expect(status).toHaveText("");
  await expect(status).toHaveAttribute("aria-busy", "false");
  await receive(page, operationStatus({
    connection_generation: scope.connection_generation,
    request_generation: bootstrap.request_generation,
  }, OPERATION_D, 0, "accepted", {
    action: "get_history",
    surface: "history",
    chat_id: null,
  }));
  await expect(status).toHaveText("");
  await expect(status).toHaveAttribute("aria-busy", "false");
  await receive(page, operationStatus({
    connection_generation: scope.connection_generation,
    request_generation: bootstrap.request_generation,
  }, OPERATION_D, 1, "completed", {
    action: "get_history",
    surface: "history",
    chat_id: null,
  }));
  await expect(status).toHaveText("");
  await expect(status).toHaveAttribute("aria-busy", "false");

  await receive(page, operationStatus(scope, OPERATION_A, 0, "accepted", {
    label: "Preparing first operation…",
  }));
  await receive(page, operationStatus(scope, OPERATION_B, 0, "running", {
    label: "Working on second operation…",
  }));
  await expect(status).toHaveText("Working on second operation…");
  await expect(status).toHaveAttribute("aria-busy", "true");

  // Content commit is not a terminal operation and cannot clear progress.
  await receive(page, snapshot(scope));
  await expect(status).toHaveText("Working on second operation…");
  await expect(status).toHaveAttribute("aria-busy", "true");

  // Completing the visible operation restores another genuinely active one;
  // the terminal success label itself is never rendered.
  await receive(page, operationStatus(scope, OPERATION_B, 1, "completed"));
  await expect(status).toHaveText("Preparing first operation…");
  await expect(status).toHaveAttribute("aria-busy", "true");

  await receive(page, operationStatus(scope, OPERATION_C, 0, "failed"));
  await expect(status).toHaveText("Visible terminal failure");
  await expect(status).toHaveAttribute("aria-busy", "false");

  // A late generic chat terminal cannot erase another operation's error.
  await receive(page, {
    type: "chat_status",
    status: "done",
    chat_id: CHAT_ID,
    connection_generation: scope.connection_generation,
    request_generation: scope.request_generation,
  });
  await expect(status).toHaveText("Visible terminal failure");
  await expect(status).toHaveAttribute("aria-busy", "false");

  // A different success cannot erase the failure notice.
  await receive(page, operationStatus(scope, OPERATION_A, 1, "completed"));
  await expect(status).toHaveText("Visible terminal failure");
  await expect(status).toHaveAttribute("aria-busy", "false");

  // A new explicit request owns the line and its success returns it to idle.
  await page.locator("#astral-input").fill("Next request");
  await page.locator("#astral-form").evaluate((form) => form.requestSubmit());
  const nextScope = await page.evaluate(() => {
    const event = window.__socketEvents.findLast((candidate) => (
      candidate.frame.type === "ui_event" && candidate.frame.action === "chat_message"
    ));
    return {
      connection_generation: event.frame.connection_generation,
      request_generation: event.frame.request_generation,
    };
  });
  await expect(status).toHaveText("Submitting…");
  await expect(status).toHaveAttribute("aria-busy", "true");
  await receive(page, operationStatus(nextScope, OPERATION_E, 0, "completed"));
  await expect(status).toHaveText("");
  await expect(status).toHaveAttribute("aria-busy", "false");
  await expect(status).toHaveAttribute("data-status-state", "idle");

  await receive(page, {
    type: "chat_status",
    status: "thinking",
    chat_id: CHAT_ID,
    connection_generation: nextScope.connection_generation,
    request_generation: nextScope.request_generation,
  });
  await expect(status).toHaveText("Thinking…");
  await expect(status).toHaveAttribute("aria-busy", "true");
  await receive(page, {
    type: "chat_status",
    status: "done",
    chat_id: CHAT_ID,
    connection_generation: nextScope.connection_generation,
    request_generation: nextScope.request_generation,
  });
  await expect(status).toHaveText("");
  await expect(status).toHaveAttribute("aria-busy", "false");
});


test("accepted completion restores a newer unacknowledged local submission", async ({ page }) => {
  await installHarness(page);
  const { frame: registrationFrame } = await registration(page);
  const status = page.locator("#astral-status");

  await page.locator("#astral-newchat-btn").click();
  const first = await page.evaluate(() => window.__socketEvents.findLast((candidate) => (
    candidate.frame.type === "ui_event" && candidate.frame.action === "new_chat"
  )).frame);
  const firstScope = {
    connection_generation: registrationFrame.connection_generation,
    request_generation: first.request_generation,
  };
  await receive(page, operationStatus(firstScope, OPERATION_A, 0, "accepted", {
    action: "new_chat",
    surface: "operation",
    chat_id: null,
    label: "Starting first operation…",
  }));
  await expect(status).toHaveText("Starting first operation…");

  await page.locator("#astral-newchat-btn").click();
  const second = await page.evaluate(() => window.__socketEvents.findLast((candidate) => (
    candidate.frame.type === "ui_event" && candidate.frame.action === "new_chat"
  )).frame);
  await expect(status).toHaveText("Submitting…");

  // A late update from the first operation can temporarily own the line.
  await receive(page, operationStatus(firstScope, OPERATION_A, 1, "running", {
    action: "new_chat",
    surface: "operation",
    chat_id: null,
    label: "Finishing first operation…",
  }));
  await expect(status).toHaveText("Finishing first operation…");

  await receive(page, operationStatus(firstScope, OPERATION_A, 2, "completed", {
    action: "new_chat",
    surface: "operation",
    chat_id: null,
  }));
  await expect(status).toHaveText("Submitting…");
  await expect(status).toHaveAttribute("aria-busy", "true");

  await receive(page, operationStatus({
    connection_generation: registrationFrame.connection_generation,
    request_generation: second.request_generation,
  }, OPERATION_B, 0, "completed", {
    action: "new_chat",
    surface: "operation",
    chat_id: null,
  }));
  await expect(status).toHaveText("");
  await expect(status).toHaveAttribute("aria-busy", "false");
});


test("same-ID replay is inert while equal conflicts, commits, and old scope are rejected", async ({ page }) => {
  await installHarness(page);
  const { frame: scope } = await registration(page);
  const accepted = snapshot(scope);
  await receive(page, accepted);

  await receive(page, accepted);
  await receive(page, snapshot(scope, {
    canvas: { target: "canvas", components: [presentation("unsafe-replay", "Changed replay must not win")] },
  }));
  await receive(page, snapshot(scope, {
    snapshot_id: SNAPSHOT_B,
    canvas: { target: "canvas", components: [presentation("conflict", "Must not win")] },
  }));
  await receive(page, snapshot(scope, {
    snapshot_id: SNAPSHOT_B,
    snapshot_purpose: "commit",
    canvas: { target: "canvas", components: [presentation("equal-commit", "Must not win") ] },
  }));
  await receive(page, snapshot(scope, {
    snapshot_id: SNAPSHOT_B,
    connection_generation: "33333333-3333-4333-8333-333333333333",
    render_revision: 2,
    canvas: { target: "canvas", components: [presentation("old-generation", "Must not win")] },
  }));

  await expect(page.locator("#astral-canvas")).toContainText("ROTE-adapted canvas");
  await expect(page.locator("#astral-canvas")).not.toContainText("Must not win");
  await expect(page.locator("#astral-canvas")).not.toContainText("Changed replay must not win");
});


test("normal new-turn equal is rejected, next commit wins, and lower or old request stays stale", async ({ page }) => {
  await installHarness(page);
  const { frame: hydrationScope } = await registration(page);
  await receive(page, snapshot(hydrationScope));

  await page.locator("#astral-input").fill("Next turn");
  await page.locator("#astral-form").evaluate((form) => form.requestSubmit());
  const commitScope = await page.evaluate(() => {
    const event = window.__socketEvents.findLast((candidate) => (
      candidate.frame.type === "ui_event" && candidate.frame.action === "chat_message"
    ));
    return {
      connection_generation: event.frame.connection_generation,
      request_generation: event.frame.request_generation,
    };
  });
  await receive(page, snapshot(commitScope, {
    snapshot_id: SNAPSHOT_B,
    snapshot_purpose: "commit",
    canvas: { target: "canvas", components: [presentation("equal-new-turn", "Equal must not win")] },
  }));
  await expect(page.locator("#astral-canvas")).toContainText("ROTE-adapted canvas");
  await expect(page.locator("#astral-canvas")).not.toContainText("Equal must not win");

  await receive(page, snapshot(commitScope, {
    snapshot_id: SNAPSHOT_B,
    snapshot_purpose: "commit",
    render_revision: 1,
    transcript: [{
      message_id: "message-2",
      role: "assistant",
      created_at: COMMITTED_AT,
      parts: [{ type: "text", text: "Committed next turn" }],
      attachments: [],
    }],
    canvas: { target: "canvas", components: [presentation("commit-next", "Revision one canvas")] },
  }));
  await expect(page.locator("#astral-chat")).toContainText("Committed next turn");
  await expect(page.locator("#astral-chat [data-astral-transient-overlay]")).toHaveCount(0);
  await expect(page.locator("#astral-canvas")).toContainText("Revision one canvas");

  await receive(page, snapshot(commitScope, {
    snapshot_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    snapshot_purpose: "commit",
    render_revision: 0,
    canvas: { target: "canvas", components: [presentation("lower", "Lower must not win")] },
  }));
  await receive(page, snapshot(hydrationScope, {
    snapshot_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    render_revision: 2,
    canvas: { target: "canvas", components: [presentation("old-request", "Old request must not win")] },
  }));
  await expect(page.locator("#astral-canvas")).toContainText("Revision one canvas");
  await expect(page.locator("#astral-canvas")).not.toContainText("must not win");
});


test("semantic decoder renders structured, component, recovery, and attachment content", async ({ page }) => {
  await installHarness(page);
  const { frame: scope } = await registration(page);
  await receive(page, snapshot(scope, {
    transcript: [{
      message_id: "semantic",
      role: "assistant",
      created_at: COMMITTED_AT,
      parts: [
        { type: "text", text: "Unicode: 雪" },
        { type: "components", components: [presentation("inline", "Interactive semantic component")] },
        { type: "structured", value: { rolls: [6, 5] }, plain_text: "rolls: 6, 5" },
        { type: "recovery", code: "saved_content_unrenderable", message: "Saved content needs recovery." },
      ],
      attachments: [{ filename: "result.json" }],
    }],
  }));

  const transcript = page.locator("#astral-chat");
  await expect(transcript).toContainText("Unicode: 雪");
  await expect(transcript).toContainText("Interactive semantic component");
  await expect(transcript).toContainText("rolls: 6, 5");
  await expect(transcript).toContainText("Saved content needs recovery.");
  await expect(transcript).toContainText("result.json");
  await expect(transcript.locator("[data-structured-value]")).toHaveAttribute(
    "data-structured-value",
    '{"rolls":[6,5]}',
  );
});


test("sequenced transient overlay never mutates committed transcript or canvas", async ({ page }) => {
  await installHarness(page);
  const { frame: scope } = await registration(page);
  await receive(page, snapshot(scope));
  await page.locator("#astral-input").fill("Preview this turn");
  await page.locator("#astral-form").evaluate((form) => form.requestSubmit());
  const previewScope = await page.evaluate(() => {
    const event = window.__socketEvents.findLast((candidate) => (
      candidate.frame.type === "ui_event" && candidate.frame.action === "chat_message"
    ));
    return {
      connection_generation: event.frame.connection_generation,
      request_generation: event.frame.request_generation,
    };
  });
  const transient = {
    type: "ui_render",
    target: "canvas",
    html: '<div id="preview">Disposable preview</div>',
    chat_id: CHAT_ID,
    connection_generation: previewScope.connection_generation,
    request_generation: previewScope.request_generation,
    base_render_revision: 0,
    frame_sequence: 1,
  };
  await receive(page, transient);
  await receive(page, { ...transient, frame_sequence: 1, html: '<div>Duplicate must not win</div>' });
  await receive(page, { ...transient, frame_sequence: 2, base_render_revision: 9, html: '<div>Wrong base</div>' });
  await receive(page, {
    ...transient,
    target: "chat",
    frame_sequence: 2,
    html: '<div id="transient-answer">Transient answer</div>',
  });

  await expect(page.locator("#astral-canvas [data-astral-transient-overlay]")).toContainText("Disposable preview");
  await expect(page.locator("#astral-chat [data-astral-transient-overlay]")).toContainText("Transient answer");
  await expect(page.locator("#astral-canvas")).toContainText("ROTE-adapted canvas");
  await expect(page.locator("#astral-canvas")).not.toContainText("Duplicate must not win");
  await expect(page.locator("#astral-canvas")).not.toContainText("Wrong base");
  await expect(page.locator("#astral-chat")).toContainText("Committed answer");

  // A successful operation terminal may race ahead of the authoritative
  // snapshot. It settles activity but cannot discard the visible answer.
  await receive(page, operationStatus(previewScope, OPERATION_A, 0, "completed"));
  await expect(page.locator("#astral-status")).toHaveText("");
  await expect(page.locator("#astral-status")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#astral-chat [data-astral-transient-overlay]")).toContainText("Transient answer");
  await expect(page.locator("#astral-canvas [data-astral-transient-overlay]")).toContainText("Disposable preview");
});


test("web presentation flags replace the ROTE canvas and explicit empty clears it", async ({ page }) => {
  await installHarness(page);
  const { frame: scope } = await registration(page);
  await receive(page, snapshot(scope, {
    canvas: {
      target: "canvas",
      components: [presentation("flagged", "Flagged canvas", { export: true, share: true })],
    },
  }));
  await expect(page.locator("#astral-canvas .dynamic-renderer")).toHaveAttribute("data-astral-export", "true");
  await expect(page.locator("#astral-canvas .dynamic-renderer")).toHaveAttribute("data-astral-share", "true");

  await receive(page, snapshot(scope, {
    snapshot_id: SNAPSHOT_B,
    render_revision: 1,
    transcript: [{
      message_id: "empty-canvas",
      role: "assistant",
      created_at: COMMITTED_AT,
      parts: [{ type: "text", text: "Canvas intentionally empty" }],
      attachments: [],
    }],
    canvas: { target: "canvas", components: [] },
  }));
  await expect(page.locator("#astral-canvas .dynamic-renderer")).toHaveCount(0);
  await expect(page.locator("#astral-canvas-empty")).toContainText("Empty");
  await expect(page.locator("#astral-chat")).toContainText("Canvas intentionally empty");
});


test("invalid mixed presentation retains both committed surfaces", async ({ page }) => {
  await installHarness(page);
  const { frame: scope } = await registration(page);
  await receive(page, snapshot(scope));
  await receive(page, snapshot(scope, {
    snapshot_id: SNAPSHOT_B,
    render_revision: 1,
    transcript: [{
      message_id: "must-not-replace",
      role: "assistant",
      created_at: COMMITTED_AT,
      parts: [{ type: "text", text: "Transcript must not replace" }],
      attachments: [],
    }],
    canvas: {
      target: "canvas",
      components: [
        presentation("one", "One", { export: false, share: false }),
        presentation("two", "Two", { export: true, share: false }),
      ],
    },
  }));
  await expect(page.locator("#astral-chat")).toContainText("Committed answer");
  await expect(page.locator("#astral-chat")).not.toContainText("Transcript must not replace");
  await expect(page.locator("#astral-canvas")).toContainText("ROTE-adapted canvas");
});


test("only exact canonical admission refusals settle local submissions", async ({ page }) => {
  await installHarness(page, { locator: false });

  async function submit() {
    await page.locator("#astral-input").fill("Refusal contract probe");
    await page.locator("#astral-form").evaluate((form) => form.requestSubmit());
    return page.evaluate(() => window.__socketEvents.findLast((candidate) => (
      candidate.frame.type === "ui_event" && candidate.frame.action === "chat_message"
    )).frame);
  }

  function refusal(frame, overrides = {}) {
    return {
      type: "error",
      submission_id: frame.submission_id,
      accepted: false,
      code: "capacity_exceeded",
      message: "Canonical refusal",
      retryable: true,
      retry_after_ms: 1000,
      ...overrides,
    };
  }

  const malformed = [
    (value) => ({ ...value, unexpected: true }),
    (value) => { const copy = { ...value }; delete copy.retry_after_ms; return copy; },
    (value) => ({ ...value, submission_id: "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA" }),
    (value) => ({ ...value, code: "toString" }),
    (value) => ({ ...value, message: "  " }),
    (value) => ({ ...value, accepted: true }),
    (value) => ({ ...value, retryable: "true" }),
    (value) => ({ ...value, retryable: false, retry_after_ms: 1 }),
    (value) => ({ ...value, retry_after_ms: -1 }),
    (value) => ({ ...value, retry_after_ms: 1.5 }),
    (value) => ({ ...value, retry_after_ms: "1000" }),
  ];
  for (const mutate of malformed) {
    const frame = await submit();
    const canonical = refusal(frame);
    await receive(page, mutate(canonical));
    await receive(page, canonical);
    await expect(page.locator("#astral-status")).toContainText("Canonical refusal");
  }

  const firstPending = await submit();
  const secondPending = await submit();
  await receive(page, refusal(firstPending, {
    submission_id: null,
    message: "Null identity must not settle",
  }));
  await receive(page, refusal(firstPending, {
    submission_id: OTHER_CHAT_ID,
    message: "Foreign identity must not settle",
  }));
  await receive(page, refusal(firstPending, { message: "First matching refusal" }));
  await expect(page.locator("#astral-status")).toContainText("First matching refusal");
  await receive(page, refusal(secondPending, { message: "Second matching refusal" }));
  await expect(page.locator("#astral-status")).toContainText("Second matching refusal");

  for (const code of ADMISSION_REFUSAL_CODES) {
    const frame = await submit();
    await receive(page, refusal(frame, { code, retry_after_ms: null }));
    await expect(page.locator("#astral-status")).toContainText("Canonical refusal");
  }

  const nonRetryable = await submit();
  await receive(page, refusal(nonRetryable, {
    code: "registration_required",
    retryable: false,
    retry_after_ms: null,
  }));
  await expect(page.locator("#astral-status")).toContainText("Canonical refusal");

  await receive(page, { type: "error", code: "forbidden", message: "Legacy error remains visible" });
  await expect(page.locator("#astral-toasts .astral-toast").last()).toHaveText(
    "Legacy error remains visible (forbidden)",
  );
});


test("located chat suppresses unscoped welcome and hydration failure retains the locator", async ({ page }) => {
  await installHarness(page);
  await receive(page, {
    type: "ui_render",
    target: "canvas",
    html: '<div id="unintended-welcome">Welcome to a new chat</div>',
  });
  await expect(page.locator("#unintended-welcome")).toHaveCount(0);
  await receive(page, { type: "error", code: "hydration_failed", message: "Retry resume", chat_id: CHAT_ID });
  expect(await page.evaluate((key) => localStorage.getItem(key), LOCATOR_KEY)).not.toBeNull();
});


test("explicit new chat clears the locator while socket loss does not", async ({ page }) => {
  await installHarness(page);
  await page.evaluate(() => window.__sockets[0].onclose());
  expect(await page.evaluate((key) => localStorage.getItem(key), LOCATOR_KEY)).not.toBeNull();

  await page.getByRole("button", { name: "New chat" }).click();
  expect(await page.evaluate((key) => localStorage.getItem(key), LOCATOR_KEY)).toBeNull();
});


test("sign-out and confirmed deletion are definitive locator clears", async ({ page }) => {
  await installHarness(page);
  await receive(page, { type: "chat_deleted", chat_id: OTHER_CHAT_ID });
  expect(await page.evaluate((key) => localStorage.getItem(key), LOCATOR_KEY)).not.toBeNull();
  await receive(page, { type: "chat_deleted", chat_id: CHAT_ID });
  expect(await page.evaluate((key) => localStorage.getItem(key), LOCATOR_KEY)).toBeNull();

  await page.evaluate(({ key, chatId }) => localStorage.setItem(key, JSON.stringify({
    schema_version: 1,
    chat_id: chatId,
    updated_at: "2026-07-16T12:01:00Z",
  })), { key: LOCATOR_KEY, chatId: CHAT_ID });
  await page.locator("#logout").evaluate((link) => link.addEventListener("click", (event) => event.preventDefault()));
  await page.locator("#logout").click();
  expect(await page.evaluate((key) => localStorage.getItem(key), LOCATOR_KEY)).toBeNull();
});


test("authenticated account switch clears only the previous account locator", async ({ page }) => {
  await installHarness(page);
  await page.evaluate(({ key, chatId, token }) => {
    localStorage.setItem(key, JSON.stringify({
      schema_version: 1,
      chat_id: chatId,
      updated_at: "2026-07-16T12:02:00Z",
    }));
    window.__sessionToken = token;
    window.__sessionSubject = "other-user";
  }, { key: OTHER_LOCATOR_KEY, chatId: OTHER_CHAT_ID, token: OTHER_TOKEN });
  await receive(page, { type: "auth_required" });
  await page.waitForFunction((chatId) => window.__socketEvents.some((event) => (
    event.frame.type === "register_ui" && event.frame.resume?.active_chat_id === chatId
  )), OTHER_CHAT_ID);
  expect(await page.evaluate((key) => localStorage.getItem(key), LOCATOR_KEY)).toBeNull();
  expect(await page.evaluate((key) => localStorage.getItem(key), OTHER_LOCATOR_KEY)).not.toBeNull();
});


test("unknown locator schema is retained but never interpreted", async ({ page }) => {
  await page.addInitScript(({ key, chatId }) => {
    localStorage.setItem(key, JSON.stringify({
      schema_version: 2,
      chat_id: chatId,
      updated_at: "2026-07-16T12:03:00Z",
    }));
  }, { key: LOCATOR_KEY, chatId: CHAT_ID });
  await installHarness(page, { locator: false });
  const event = await registration(page);
  expect(event.frame.resume).toBeUndefined();
  expect(JSON.parse(await page.evaluate((key) => localStorage.getItem(key), LOCATOR_KEY)).schema_version).toBe(2);
});


test("URL-selected chat is persisted before its first registration", async ({ page }) => {
  await installHarness(page, {
    locator: false,
    url: `https://candidate.example/?chat=${OTHER_CHAT_ID}`,
  });
  const event = await registration(page);
  expect(event.frame.resume.active_chat_id).toBe(OTHER_CHAT_ID);
  expect(JSON.parse(event.locatorAtSend).chat_id).toBe(OTHER_CHAT_ID);
});
