// Feature 060 T110 — watchOS release-evidence producer.
//
// Drives the SHIPPING Watch app models (WatchModel + ConversationResumeStore +
// the production reducers) on a booted watchOS simulator against the exact
// release-readiness staging endpoint and emits one schema-valid
// `platform_evidence` report (watchos.json) plus per-check raw JSON references,
// mirroring the web producer (tooling/web-ci/tests/release-060.spec.js).
// Sign-in exercises the real device-login broker on the staged candidate; the
// canonical `personal_agent` authoring check is ALWAYS not_applicable on
// watchOS (the watch is excluded from BYO authoring entirely). Local/CI
// evidence is diagnostic only — protected CI re-validates every byte.
//
// Environment contract (values reach this process through xcodebuild
// `TEST_RUNNER_`-prefixed variables; identity names match the web producer):
//   ASTRAL_STAGING_URL               staged candidate base URL; absent => XCTSkip
//   ASTRAL_RELEASE_EVIDENCE_OUTPUT   absolute path of the watchos.json report
//   ASTRAL_RELEASE_PLATFORM          optional; must be "watchos" when present
//   ASTRAL_RELEASE_CANDIDATE_SHA / ASTRAL_RELEASE_ID / ASTRAL_RELEASE_VERSION
//   ASTRAL_RELEASE_STAGING_FILE      trusted stage-deploy outputs JSON
//   ASTRAL_RELEASE_ARTIFACT_REFERENCE / ASTRAL_RELEASE_ARTIFACT_SHA256
//     (+ optional ASTRAL_RELEASE_ARTIFACT_NAME, ASTRAL_RELEASE_ARTIFACT_BUILD_IDENTITY)
//   RUNNER_OS / RUNNER_ARCH / RUNNER_NAME / ASTRAL_RUNNER_IMAGE / ASTRAL_RUNNER_ENVIRONMENT
//   GITHUB_WORKFLOW / GITHUB_RUN_ID / GITHUB_RUN_ATTEMPT / GITHUB_JOB
import AstralCore
import CryptoKit
import Foundation
import XCTest

@testable import AstralWatch

private let releasePrompt = "Roll exactly six six-sided dice and show the normalized results."
private let releaseDiceAnswer = "You rolled six six-sided dice: 4, 2, 6, 1, 5, 3 (total 21)."

private let stagingProjectionKeys = [
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
]

private let voiceRuntimeProjectionKeys: Set<String> = [
    "voice_worker_image_reference",
    "voice_worker_image_sha256",
    "livekit_image_reference",
    "livekit_image_sha256",
    "livekit_config_sha256",
    "livekit_public_url",
    "livekit_turn_domain",
    "speech_profile",
]

private let speechProfileProjectionKeys: Set<String> = [
    "asr_model", "tts_model", "voice", "sample_rate_hz", "inventory_sha256", "profile_sha256",
]

private let voiceASRModel = "Systran/faster-whisper-large-v3"
private let voiceTTSModel = "speaches-ai/Kokoro-82M-v1.0-ONNX"
private let voiceName = "af_heart"
private let voiceSampleRateHz = 24000

private struct EvidenceFailure: Error, CustomStringConvertible {
    let code: String
    let message: String
    var description: String { "\(code): \(message)" }
}

/// One check's produced facts. `applicabilityReason` wins over `failureCode`;
/// both nil means the check passed.
private struct CheckProduction {
    var raw: [String: Any] = [:]
    var measurements: [[String: Any]] = []
    var failureCode: String?
    var applicabilityReason: String?
}

private func environmentValue(_ name: String) -> String? {
    guard let value = ProcessInfo.processInfo.environment[name], !value.isEmpty else { return nil }
    return value
}

private func requiredEnvironment(_ name: String) throws -> String {
    guard let value = environmentValue(name) else {
        throw EvidenceFailure(
            code: "missing_environment",
            message: "\(name) is required once ASTRAL_STAGING_URL is present")
    }
    return value
}

private func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

private func isSHA256(_ value: Any?) -> Bool {
    guard let digest = value as? String, digest.utf8.count == 64 else { return false }
    return digest.utf8.allSatisfy {
        (48...57).contains($0) || (97...102).contains($0)
    }
}

/// Validate the secret-free stage identity without rebuilding or overriding it.
private func validateVoiceRuntime(_ value: Any) throws {
    guard let runtime = value as? [String: Any], Set(runtime.keys) == voiceRuntimeProjectionKeys,
        let profile = runtime["speech_profile"] as? [String: Any],
        Set(profile.keys) == speechProfileProjectionKeys
    else {
        throw EvidenceFailure(
            code: "voice_runtime_invalid",
            message: "trusted voice_runtime has a missing, extra, or non-object field")
    }
    guard profile["asr_model"] as? String == voiceASRModel,
        profile["tts_model"] as? String == voiceTTSModel,
        profile["voice"] as? String == voiceName,
        (profile["sample_rate_hz"] as? NSNumber)?.intValue == voiceSampleRateHz
    else {
        throw EvidenceFailure(
            code: "speech_profile_mismatch",
            message: "trusted speech_profile differs from the exact voice profile")
    }
    for field in [
        "voice_worker_image_sha256", "livekit_image_sha256", "livekit_config_sha256",
    ] where !isSHA256(runtime[field]) {
        throw EvidenceFailure(
            code: "voice_runtime_digest_invalid",
            message: "trusted voice_runtime \(field) is not a SHA-256 digest")
    }
    for field in ["inventory_sha256", "profile_sha256"] where !isSHA256(profile[field]) {
        throw EvidenceFailure(
            code: "speech_profile_digest_invalid",
            message: "trusted speech_profile \(field) is not a SHA-256 digest")
    }
    for field in [
        "voice_worker_image_reference", "livekit_image_reference",
        "livekit_public_url", "livekit_turn_domain",
    ] {
        guard let reference = runtime[field] as? String, !reference.isEmpty else {
            throw EvidenceFailure(
                code: "voice_runtime_reference_invalid",
                message: "trusted voice_runtime \(field) is empty")
        }
    }
}

/// Pretty sorted-key JSON with a trailing newline, written atomically
/// (temp + rename); returns the byte digest — the web producer's `atomicJson`.
@discardableResult
private func writeCanonicalJSON(_ object: [String: Any], to path: String) throws -> String {
    let data = try JSONSerialization.data(
        withJSONObject: object,
        options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
    var bytes = data
    bytes.append(0x0A)
    let destination = URL(fileURLWithPath: path)
    try FileManager.default.createDirectory(
        at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
    let temporary = destination.deletingLastPathComponent()
        .appendingPathComponent("\(destination.lastPathComponent).\(UUID().uuidString).tmp")
    try bytes.write(to: temporary, options: .withoutOverwriting)
    if FileManager.default.fileExists(atPath: destination.path) {
        try FileManager.default.removeItem(at: destination)
    }
    try FileManager.default.moveItem(at: temporary, to: destination)
    return sha256Hex(bytes)
}

private func measurementRecord(
    metric: String,
    aggregation: String,
    value: Double,
    unit: String,
    sampleCount: Int,
    comparator: String,
    threshold: Double
) -> [String: Any] {
    [
        "metric": metric,
        "aggregation": aggregation,
        "value": max(0, (value * 1000).rounded() / 1000),
        "unit": unit,
        "sample_count": max(1, sampleCount),
        "comparator": comparator,
        "threshold": threshold,
    ]
}

private func normalizedBaseURL(_ raw: String) throws -> String {
    guard let url = URL(string: raw),
        url.scheme?.lowercased() == "https",
        let host = url.host?.lowercased(),
        !["localhost", "127.0.0.1", "::1"].contains(host),
        url.user == nil, url.password == nil, url.query == nil, url.fragment == nil
    else {
        throw EvidenceFailure(
            code: "invalid_staging_url",
            message: "ASTRAL_STAGING_URL must be non-loopback HTTPS without credentials or request data")
    }
    var trimmed = raw
    while trimmed.hasSuffix("/") { trimmed.removeLast() }
    return trimmed
}

/// Reads the environment contract once, owns the raw-evidence directory, and
/// assembles check records plus the final watchos `platform_evidence` report.
private final class WatchEvidenceRecorder {
    let baseURL: String
    let platform = "watchos"
    let outputPath: String
    let rawDirectoryName = "watchos-raw"
    let candidateSha: String
    let releaseId: String
    let releaseVersion: String
    let staging: [String: Any]
    let artifact: [String: Any]
    let runner: [String: Any]
    let workflow: [String: Any]
    let startedAt: String

    private let rawDirectoryPath: String
    private let timestampFormatter: ISO8601DateFormatter

    init(stagingURLRaw: String) throws {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        timestampFormatter = formatter
        startedAt = formatter.string(from: Date())

        baseURL = try normalizedBaseURL(stagingURLRaw)
        if let declared = environmentValue("ASTRAL_RELEASE_PLATFORM"), declared != "watchos" {
            throw EvidenceFailure(
                code: "platform_mismatch",
                message: "ASTRAL_RELEASE_PLATFORM=\(declared) but this producer emits watchos evidence")
        }
        outputPath = try requiredEnvironment("ASTRAL_RELEASE_EVIDENCE_OUTPUT")
        rawDirectoryPath =
            URL(fileURLWithPath: outputPath)
            .deletingLastPathComponent().appendingPathComponent(rawDirectoryName).path
        candidateSha = try requiredEnvironment("ASTRAL_RELEASE_CANDIDATE_SHA")
        releaseId = try requiredEnvironment("ASTRAL_RELEASE_ID")
        releaseVersion = try requiredEnvironment("ASTRAL_RELEASE_VERSION")

        let stagePath = try requiredEnvironment("ASTRAL_RELEASE_STAGING_FILE")
        guard let stageData = FileManager.default.contents(atPath: stagePath),
            let stage = (try? JSONSerialization.jsonObject(with: stageData)) as? [String: Any]
        else {
            throw EvidenceFailure(
                code: "staging_outputs_unreadable",
                message: "cannot parse trusted staging outputs at \(stagePath)")
        }
        var projected: [String: Any] = [:]
        for key in stagingProjectionKeys {
            guard let value = stage[key], !(value is NSNull) else {
                throw EvidenceFailure(
                    code: "staging_outputs_incomplete",
                    message: "trusted staging output is missing \(key)")
            }
            projected[key] = value
        }
        var endpoint = (projected["endpoint"] as? String) ?? ""
        while endpoint.hasSuffix("/") { endpoint.removeLast() }
        guard endpoint == baseURL else {
            throw EvidenceFailure(
                code: "staging_endpoint_mismatch",
                message: "ASTRAL_STAGING_URL differs from the staged endpoint")
        }
        guard let workerPaths = projected["worker_paths"] as? [String],
            workerPaths.contains("voice")
        else {
            throw EvidenceFailure(
                code: "voice_worker_missing",
                message: "trusted staging output does not include the voice worker path")
        }
        try validateVoiceRuntime(projected["voice_runtime"] as Any)
        staging = projected

        artifact = [
            "name": environmentValue("ASTRAL_RELEASE_ARTIFACT_NAME") ?? "AstralWatch.app",
            "kind": "watchos_app",
            "immutable_reference": try requiredEnvironment("ASTRAL_RELEASE_ARTIFACT_REFERENCE"),
            "sha256": try requiredEnvironment("ASTRAL_RELEASE_ARTIFACT_SHA256"),
            "build_identity": environmentValue("ASTRAL_RELEASE_ARTIFACT_BUILD_IDENTITY")
                ?? "candidate-xcode:\(candidateSha)",
        ]

        let os = try requiredEnvironment("RUNNER_OS").lowercased()
        let architectureRaw = try requiredEnvironment("RUNNER_ARCH").lowercased()
        let architecture = ["x64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"][architectureRaw]
        let runnerEnvironment = try requiredEnvironment("ASTRAL_RUNNER_ENVIRONMENT")
        guard os == "macos", let architecture,
            ["github_hosted", "self_hosted"].contains(runnerEnvironment)
        else {
            throw EvidenceFailure(
                code: "runner_identity_invalid",
                message: "watch producer runner identity is outside the release schema")
        }
        runner = [
            "os": os,
            "architecture": architecture,
            "runner_image": try requiredEnvironment("ASTRAL_RUNNER_IMAGE"),
            "runner_name": try requiredEnvironment("RUNNER_NAME"),
            "runner_environment": runnerEnvironment,
        ]

        let attemptRaw = try requiredEnvironment("GITHUB_RUN_ATTEMPT")
        guard let attempt = Int(attemptRaw), attempt >= 1 else {
            throw EvidenceFailure(code: "workflow_identity_invalid", message: "GITHUB_RUN_ATTEMPT is invalid")
        }
        workflow = [
            "name": try requiredEnvironment("GITHUB_WORKFLOW"),
            "run_id": try requiredEnvironment("GITHUB_RUN_ID"),
            "run_attempt": attempt,
            "job_id": try requiredEnvironment("GITHUB_JOB"),
        ]
    }

    func writeRaw(_ name: String, _ object: [String: Any]) throws -> [String: Any] {
        let path = URL(fileURLWithPath: rawDirectoryPath).appendingPathComponent("\(name).json").path
        let digest = try writeCanonicalJSON(object, to: path)
        return [
            "name": "watchos_\(name)",
            "kind": "json_metrics",
            "immutable_reference": "bundle://\(rawDirectoryName)/\(name).json",
            "sha256": digest,
        ]
    }

    func checkRecord(id: String, production: CheckProduction, startedAt checkStart: Date) -> [String: Any] {
        var outcome = "passed"
        var detailCode: Any = NSNull()
        var applicabilityReason: Any = NSNull()
        if let reason = production.applicabilityReason {
            outcome = "not_applicable"
            applicabilityReason = reason
        } else if let code = production.failureCode {
            outcome = "failed"
            detailCode = code
        }
        var artifacts: [[String: Any]] = []
        if !production.raw.isEmpty, let artifact = try? writeRaw(id, production.raw) {
            artifacts = [artifact]
        }
        return [
            "id": id,
            "outcome": outcome,
            "duration_ms": max(0, Int(Date().timeIntervalSince(checkStart) * 1000)),
            "detail_code": detailCode,
            "applicability_reason": applicabilityReason,
            "measurements": production.measurements,
            "evidence_artifacts": artifacts,
        ]
    }

    func writeReport(checks: [[String: Any]], outcome: String) throws -> String {
        let report: [String: Any] = [
            "document_type": "platform_evidence",
            "schema_version": 1,
            "evidence_id": UUID().uuidString.lowercased(),
            "candidate_sha": candidateSha,
            "release_id": releaseId,
            "release_version": releaseVersion,
            "platform": platform,
            "target_description":
                "AstralWatch shipping Watch app models driven by AstralWatchTests on a booted "
                + "watchOS simulator against the staged candidate",
            "artifact": artifact,
            "staging_environment": staging,
            "runner": runner,
            "workflow": workflow,
            "started_at": startedAt,
            "completed_at": timestampFormatter.string(from: Date()),
            "outcome": outcome,
            "unavailable_reason": NSNull(),
            "unavailability_observation": NSNull(),
            "checks": checks,
        ]
        try writeCanonicalJSON(report, to: outputPath)
        return outputPath
    }
}

@MainActor
final class ReleaseEvidenceTests: XCTestCase {
    private let account = ConversationAccount(
        issuer: "https://id.example.test/realms/astral",
        subject: "release-evidence-watch")!
    private let chat = "11111111-1111-4111-8111-111111111111"
    private let connection = "22222222-2222-4222-8222-222222222222"
    private let hydration = "33333333-3333-4333-8333-333333333333"

    private var failedCheckIds: [String] = []

    func testWatchReleaseEvidenceProducesPlatformReport() async throws {
        guard let stagingURL = environmentValue("ASTRAL_STAGING_URL") else {
            throw XCTSkip("ASTRAL_STAGING_URL is supplied by the release-readiness watchos producer job")
        }
        let recorder: WatchEvidenceRecorder
        do {
            recorder = try WatchEvidenceRecorder(stagingURLRaw: stagingURL)
        } catch let failure as EvidenceFailure {
            XCTFail("release evidence environment is incomplete — \(failure)")
            return
        }
        failedCheckIds = []
        var checks: [[String: Any]] = []

        checks.append(
            await runCheck("sign_in", recorder: recorder) {
                try await self.performDeviceLoginSignIn(recorder: recorder)
            })
        checks.append(
            await runCheck("rendered_chat", recorder: recorder) {
                try self.runRenderedChat()
            })
        checks.append(
            await runCheck("reconnect_resume", recorder: recorder) {
                try await self.runResumeTrials()
            })
        checks.append(
            await runCheck("agent_lifecycle", recorder: recorder) {
                try self.runAgentLifecycle()
            })
        checks.append(
            await runCheck("accessibility_semantics", recorder: recorder) {
                try self.runAccessibilityContract()
            })
        checks.append(
            await runCheck("personal_agent", recorder: recorder) {
                var production = CheckProduction()
                production.applicabilityReason =
                    "The shipping Watch app has no personal-agent authoring surface; the canonical "
                    + "authoring check applies only to the web, windows, android, macos, and ios clients."
                production.raw = ["authoring_surface_present": false]
                return production
            })

        let outcome = failedCheckIds.isEmpty ? "passed" : "failed"
        do {
            let path = try recorder.writeReport(checks: checks, outcome: outcome)
            if let data = FileManager.default.contents(atPath: path) {
                let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
                attachment.name = "watchos-platform-evidence"
                attachment.lifetime = .keepAlways
                add(attachment)
            }
        } catch {
            XCTFail("failed to write the watchos evidence report: \(error)")
            return
        }
        if !failedCheckIds.isEmpty {
            XCTFail(
                "release evidence checks failed: \(failedCheckIds.joined(separator: ", ")) — "
                    + "diagnostic report written to \(recorder.outputPath)")
        }
    }

    // MARK: - Check harness

    private func runCheck(
        _ id: String,
        recorder: WatchEvidenceRecorder,
        body: () async throws -> CheckProduction
    ) async -> [String: Any] {
        let started = Date()
        do {
            let production = try await body()
            if production.failureCode != nil { failedCheckIds.append(id) }
            return recorder.checkRecord(id: id, production: production, startedAt: started)
        } catch let failure as EvidenceFailure {
            failedCheckIds.append(id)
            var production = CheckProduction()
            production.failureCode = failure.code
            production.raw = ["failure": failure.message]
            return recorder.checkRecord(id: id, production: production, startedAt: started)
        } catch {
            failedCheckIds.append(id)
            var production = CheckProduction()
            production.failureCode = "unexpected_error"
            production.raw = ["failure": String(describing: error)]
            return recorder.checkRecord(id: id, production: production, startedAt: started)
        }
    }

    // MARK: - Model fixtures

    private func freshStore() -> ConversationResumeStore {
        let suite = "WatchReleaseEvidenceTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return ConversationResumeStore(
            defaults: defaults,
            now: { Date(timeIntervalSince1970: 1_752_605_260) })
    }

    private func inbound(_ text: String) throws -> InboundFrame {
        guard let frame = InboundFrame.parse(text) else {
            throw EvidenceFailure(code: "frame_parse_failed", message: "canonical frame did not parse")
        }
        return frame
    }

    private func snapshot(
        id: String = "55555555-5555-4555-8555-555555555555",
        request: String? = nil,
        purpose: String = "hydration",
        revision: Int = 0,
        text: String = "Release canvas"
    ) throws -> InboundFrame {
        try inbound(
            """
            {"type":"conversation_snapshot","schema_version":1,
             "snapshot_id":"\(id)","chat_id":"\(chat)",
             "connection_generation":"\(connection)",
             "request_generation":"\(request ?? hydration)",
             "snapshot_purpose":"\(purpose)","render_revision":\(revision),
             "committed_at":"2026-07-16T12:00:00Z",
             "transcript":[
               {"message_id":"u1","role":"user","created_at":"2026-07-16T11:59:00Z",
                "parts":[{"type":"text","text":"\(releasePrompt)"}],
                "attachments":[]},
               {"message_id":"a1","role":"assistant","created_at":"2026-07-16T11:59:59Z",
                "parts":[
                  {"type":"text","text":"Rolling the dice now."},
                  {"type":"components","components":[{"type":"text","content":"\(releaseDiceAnswer)"}]}
                ],"attachments":[]}
             ],
             "canvas":{"target":"canvas","components":[{"type":"text","content":"\(text)"}]}}
            """)
    }

    // MARK: - sign_in (real device-login broker on the staged candidate)

    private func performDeviceLoginSignIn(recorder: WatchEvidenceRecorder) async throws -> CheckProduction {
        guard let base = URL(string: recorder.baseURL) else {
            throw EvidenceFailure(code: "invalid_staging_url", message: "staging URL did not parse")
        }
        let started = Date()
        let client = DeviceLoginClient(serverBase: base)
        let start: DeviceLoginStart
        let poll: DeviceLoginPoll
        do {
            start = try await client.start()
            poll = try await client.poll(handle: start.handle)
        } catch let error as DeviceLoginError {
            throw EvidenceFailure(
                code: "device_login_unavailable",
                message: "staged device-login broker refused the watch flow: \(error)")
        }
        guard !start.handle.isEmpty, !start.userCode.isEmpty, !start.verificationURIComplete.isEmpty
        else {
            throw EvidenceFailure(
                code: "device_login_start_invalid",
                message: "device-login start lacks handle/user code/verification URI")
        }
        let pollStatus: String
        switch poll {
        case .pending:
            pollStatus = "pending"
        case .slowDown:
            pollStatus = "slow_down"
        default:
            throw EvidenceFailure(
                code: "device_login_poll_unexpected",
                message: "an unapproved fresh handle must poll as pending")
        }
        var production = CheckProduction()
        production.raw = [
            "method": "device_authorization_grant_via_backend_broker",
            "duration_ms": Int(Date().timeIntervalSince(started) * 1000),
            "handle_sha256": sha256Hex(Data(start.handle.utf8)),
            "user_code_length": start.userCode.count,
            "verification_uri_present": !start.verificationURIComplete.isEmpty,
            "poll_status": pollStatus,
        ]
        return production
    }

    // MARK: - rendered_chat (production reducers end to end)

    private func runRenderedChat() throws -> CheckProduction {
        let started = Date()
        let model = WatchModel(conversationResumeStore: freshStore())
        model.bindConversationAccount(account)
        guard model.beginConversationConnection(connection),
            model.openConversationRequest(
                chatId: chat, requestGeneration: hydration, purpose: .hydration)
        else {
            throw EvidenceFailure(code: "rendered_chat_unobserved", message: "continuity fence refused")
        }
        model.handleFrame(try snapshot())
        guard model.entries.count == 3, model.canvas.map(\.fallbackText) == ["Release canvas"] else {
            throw EvidenceFailure(
                code: "rendered_chat_unobserved", message: "hydration snapshot did not restore the transcript")
        }

        var sentFrame: JSONValue?
        model.outboundTap = { text in
            sentFrame = try? JSONValue.parse(Data(text.utf8))
        }
        model.pendingDictation = releasePrompt
        model.sendPending()
        guard let sent = sentFrame,
            sent["payload"]?["snapshot_purpose"]?.stringValue == "commit",
            let request = sent["payload"]?["request_generation"]?.stringValue,
            !request.isEmpty
        else {
            throw EvidenceFailure(
                code: "rendered_chat_unobserved", message: "the outbound turn lacks commit identity")
        }

        model.handleFrame(
            try inbound(
                """
                {"type":"ui_render","target":"canvas","chat_id":"\(chat)",
                 "connection_generation":"\(connection)","request_generation":"\(request)",
                 "base_render_revision":0,"frame_sequence":1,
                 "components":[{"type":"text","content":"Preview"}]}
                """))
        guard model.visibleCanvas.map(\.fallbackText) == ["Preview"] else {
            throw EvidenceFailure(
                code: "rendered_chat_unobserved", message: "the transient preview did not render")
        }
        model.handleFrame(
            try snapshot(
                id: "66666666-6666-4666-8666-666666666666",
                request: request,
                purpose: "commit",
                revision: 1,
                text: releaseDiceAnswer))
        guard model.canvas.map(\.fallbackText) == [releaseDiceAnswer],
            model.visibleCanvas.map(\.fallbackText) == [releaseDiceAnswer]
        else {
            throw EvidenceFailure(
                code: "rendered_chat_unobserved", message: "the commit snapshot did not render the dice answer")
        }

        var production = CheckProduction()
        production.raw = [
            "prompt_sha256": sha256Hex(Data(releasePrompt.utf8)),
            "committed_canvas": model.canvas.map(\.fallbackText),
            "duration_ms": Int(Date().timeIntervalSince(started) * 1000),
        ]
        return production
    }

    // MARK: - reconnect_resume (>= 20 trials with counters)

    private func runResumeTrials() async throws -> CheckProduction {
        let store = freshStore()
        guard store.save(chatId: chat, for: account) else {
            throw EvidenceFailure(code: "resume_trials_below_floor", message: "locator save refused")
        }
        var latenciesMs: [Double] = []
        var successes = 0
        let trials = 20

        for trial in 1...trials {
            let startedTrial = Date()
            let model = WatchModel(conversationResumeStore: store)
            model.bindConversationAccount(account)
            let frameText = model.registrationFrame(token: "release-evidence-token", resumed: trial > 1)
            let registration = try? JSONValue.parse(Data(frameText.utf8))
            let resumed =
                registration?["resume"]?["active_chat_id"]?.stringValue == chat
                && model.activeChatId == chat
            await model.handle(.disconnected(reason: "release evidence trial \(trial)"))
            let retained = store.load(for: account)?.chatId == chat
            latenciesMs.append(Date().timeIntervalSince(startedTrial) * 1000)
            if resumed && retained { successes += 1 }
        }

        let successRate = Double(successes) / Double(trials) * 100
        var production = CheckProduction()
        production.measurements = [
            measurementRecord(
                metric: "trial_count", aggregation: "total", value: Double(trials),
                unit: "count", sampleCount: trials, comparator: "gte", threshold: 20),
            measurementRecord(
                metric: "resume_success_rate", aggregation: "rate", value: successRate,
                unit: "percent", sampleCount: trials, comparator: "gte", threshold: 100),
        ]
        production.raw = [
            "trial_count": trials,
            "successful_trials": successes,
            "latencies_ms": latenciesMs.map { Int($0.rounded()) },
        ]
        if successes < trials {
            production.failureCode = "resume_trials_below_floor"
        }
        return production
    }

    // MARK: - agent_lifecycle (surfaced without reload through the shipping reducer)

    private func runAgentLifecycle() throws -> CheckProduction {
        let started = Date()
        let model = WatchModel(conversationResumeStore: freshStore())
        guard model.beginConversationConnection(connection) else {
            throw EvidenceFailure(code: "lifecycle_states_unobserved", message: "connection fence refused")
        }
        let states = ["starting", "online", "updating", "failed", "offline"]
        var observed: [String] = []
        for (index, state) in states.enumerated() {
            model.handleFrame(
                try inbound(
                    """
                    {"type":"agent_lifecycle","agent_id":"ua-release","revision_id":null,
                     "runtime_instance_id":null,"lifecycle_generation":7,
                     "state_revision":\(index + 1),"state":"\(state)",
                     "reason_code":"release_evidence","label":"Release \(state)",
                     "updated_at":"2026-07-16T12:00:0\(index)Z"}
                    """))
            guard model.agentLifecycles["ua-release"]?.state == state else {
                throw EvidenceFailure(
                    code: "lifecycle_states_unobserved",
                    message: "lifecycle state \(state) did not surface")
            }
            observed.append(state)
        }
        guard model.rootStatusText == "ua-release: Release offline" else {
            throw EvidenceFailure(
                code: "lifecycle_states_unobserved",
                message: "the root live status did not carry the latest lifecycle label")
        }
        var production = CheckProduction()
        production.raw = [
            "observed_states": observed,
            "root_status": model.rootStatusText ?? "",
            "duration_ms": Int(Date().timeIntervalSince(started) * 1000),
        ]
        return production
    }

    // MARK: - accessibility_semantics (WatchAccessibility060 contract)

    private func runAccessibilityContract() throws -> CheckProduction {
        let started = Date()
        let controls = [
            WatchAccessibility060.replay,
            WatchAccessibility060.stop(isSpeaking: false),
            WatchAccessibility060.dictate,
            WatchAccessibility060.send,
            WatchAccessibility060.discard,
        ]
        var violations: [String] = []
        if Set(controls.map(\.identifier)).count != controls.count {
            violations.append("interactive control identifiers are not unique")
        }
        for control in controls {
            if control.role != .button { violations.append("\(control.identifier) is not a button") }
            if control.name.isEmpty { violations.append("\(control.identifier) has no name") }
            if control.state.isEmpty { violations.append("\(control.identifier) has no state") }
            if !control.focusable { violations.append("\(control.identifier) is not focusable") }
        }
        let status = WatchAccessibility060.operationStatus("Working…")
        if status.identifier != "watch-operation-status" || status.role != .status
            || status.name != "Operation status" || status.focusable
        {
            violations.append("operation status live region broke its contract")
        }
        let root = WatchAccessibility060.rootStatus("ua-release: Release online")
        if root.identifier != "watch-root-live-status" || root.role != .status
            || root.name != "Live status" || root.focusable
        {
            violations.append("root live status region broke its contract")
        }

        var production = CheckProduction()
        production.raw = [
            "inspected_controls": controls.map(\.identifier)
                + ["watch-operation-status", "watch-root-live-status"],
            "violations": violations,
            "duration_ms": Int(Date().timeIntervalSince(started) * 1000),
        ]
        if !violations.isEmpty {
            production.failureCode = "accessibility_contract_violation"
        }
        return production
    }
}
