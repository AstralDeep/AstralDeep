// Feature 060 T110 — Apple release-evidence producer (iOS + macOS destinations).
//
// Drives the SHIPPING AstralDeep app against the exact release-readiness staging
// endpoint and emits one schema-valid `platform_evidence` report ({ios|macos}.json)
// plus per-check raw JSON references, mirroring the web producer
// (tooling/web-ci/tests/release-060.spec.js). Local/CI evidence is diagnostic only —
// protected CI re-validates every byte (`protected_release_authorization: false`).
//
// Environment contract (values reach this runner through xcodebuild
// `TEST_RUNNER_`-prefixed variables; identity names match the web producer):
//   ASTRAL_STAGING_URL                 staged candidate base URL; absent => XCTSkip
//   ASTRAL_RELEASE_EVIDENCE_OUTPUT     absolute path of the platform report JSON
//   ASTRAL_RELEASE_PLATFORM            "ios" | "macos" (must match this build)
//   ASTRAL_RELEASE_CANDIDATE_SHA / ASTRAL_RELEASE_ID / ASTRAL_RELEASE_VERSION
//   ASTRAL_RELEASE_STAGING_FILE        trusted stage-deploy outputs JSON (stage-owned
//                                      staging identity; endpoint must equal the base URL)
//   ASTRAL_RELEASE_ARTIFACT_REFERENCE / ASTRAL_RELEASE_ARTIFACT_SHA256
//     (+ optional ASTRAL_RELEASE_ARTIFACT_NAME, ASTRAL_RELEASE_ARTIFACT_BUILD_IDENTITY)
//   ASTRAL_RELEASE_USERNAME / ASTRAL_RELEASE_PASSWORD   staging Keycloak identity
//                                      (pre-provisioned with an LLM configuration)
//   RUNNER_OS / RUNNER_ARCH / RUNNER_NAME / ASTRAL_RUNNER_IMAGE / ASTRAL_RUNNER_ENVIRONMENT
//   GITHUB_WORKFLOW / GITHUB_RUN_ID / GITHUB_RUN_ATTEMPT / GITHUB_JOB
//   ASTRAL_STAGING_AUTHORITY           optional Keycloak authority launch override
//   ASTRAL_RELEASE_LIFECYCLE_AGENT_ID  optional agent display-name fragment that must
//                                      appear among the observed lifecycle labels
//   ASTRAL_STAGING_CAPABILITY_FILE     macOS only: candidate-owned capability map JSON.
//                                      Missing or malformed records a FAILED
//                                      macos_personal_agent_host check — never N/A.
import CryptoKit
import Foundation
import XCTest

#if os(macOS)
    private let compiledPlatform = "macos"
#else
    private let compiledPlatform = "ios"
#endif

private let releasePrompt = "Roll exactly six six-sided dice and show the normalized results."

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
/// both nil means the check passed. Measurements survive a failed floor so the
/// report stays quantitative either way.
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

/// Ceil-rank percentile over a sorted sample (sibling continuity convention).
private func percentile(_ fraction: Double, sorted: [Double]) -> Double {
    guard !sorted.isEmpty else { return 0 }
    let rank = max(0, min(sorted.count - 1, Int(ceil(fraction * Double(sorted.count))) - 1))
    return sorted[rank]
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

/// Reads the full environment contract once, owns the raw-evidence directory,
/// and assembles check records plus the final `platform_evidence` report.
private final class EvidenceRecorder {
    let baseURL: String
    let platform: String
    let outputPath: String
    let rawDirectoryName: String
    let candidateSha: String
    let releaseId: String
    let releaseVersion: String
    let staging: [String: Any]
    let artifact: [String: Any]
    let runner: [String: Any]
    let workflow: [String: Any]
    let username: String
    let password: String
    let authorityOverride: String?
    let lifecycleAgentFragment: String?
    let startedAt: String

    private let rawDirectoryPath: String
    private let timestampFormatter: ISO8601DateFormatter

    init(stagingURLRaw: String) throws {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        timestampFormatter = formatter
        startedAt = formatter.string(from: Date())

        baseURL = try normalizedBaseURL(stagingURLRaw)
        platform = try requiredEnvironment("ASTRAL_RELEASE_PLATFORM")
        guard platform == compiledPlatform else {
            throw EvidenceFailure(
                code: "platform_mismatch",
                message: "ASTRAL_RELEASE_PLATFORM=\(platform) but this build is \(compiledPlatform)")
        }
        outputPath = try requiredEnvironment("ASTRAL_RELEASE_EVIDENCE_OUTPUT")
        rawDirectoryName = "\(platform)-raw"
        rawDirectoryPath =
            URL(fileURLWithPath: outputPath)
            .deletingLastPathComponent().appendingPathComponent(rawDirectoryName).path
        candidateSha = try requiredEnvironment("ASTRAL_RELEASE_CANDIDATE_SHA")
        releaseId = try requiredEnvironment("ASTRAL_RELEASE_ID")
        releaseVersion = try requiredEnvironment("ASTRAL_RELEASE_VERSION")
        username = try requiredEnvironment("ASTRAL_RELEASE_USERNAME")
        password = try requiredEnvironment("ASTRAL_RELEASE_PASSWORD")
        authorityOverride = environmentValue("ASTRAL_STAGING_AUTHORITY")
        lifecycleAgentFragment = environmentValue("ASTRAL_RELEASE_LIFECYCLE_AGENT_ID")

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
            "name": environmentValue("ASTRAL_RELEASE_ARTIFACT_NAME") ?? "AstralDeep.app",
            "kind": platform == "macos" ? "macos_app" : "ios_app",
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
                message: "Apple producer runner identity is outside the release schema")
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
            "name": "\(platform)_\(name)",
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
        let description =
            platform == "macos"
            ? "AstralDeep macOS app driven by XCUITest against the staged candidate"
            : "AstralDeep iOS app driven by XCUITest on a booted simulator against the staged candidate"
        let report: [String: Any] = [
            "document_type": "platform_evidence",
            "schema_version": 1,
            "evidence_id": UUID().uuidString.lowercased(),
            "candidate_sha": candidateSha,
            "release_id": releaseId,
            "release_version": releaseVersion,
            "platform": platform,
            "target_description": description,
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

final class ReleaseEvidenceUITests: XCTestCase {
    private var app: XCUIApplication!
    private var failedCheckIds: [String] = []

    override func setUp() {
        super.setUp()
        continueAfterFailure = true
    }

    override func tearDown() {
        app?.terminate()
        app = nil
        super.tearDown()
    }

    func testReleaseEvidenceProducesPlatformReport() throws {
        guard let stagingURL = environmentValue("ASTRAL_STAGING_URL") else {
            throw XCTSkip("ASTRAL_STAGING_URL is supplied by the release-readiness Apple producer jobs")
        }
        let recorder: EvidenceRecorder
        do {
            recorder = try EvidenceRecorder(stagingURLRaw: stagingURL)
        } catch let failure as EvidenceFailure {
            XCTFail("release evidence environment is incomplete — \(failure)")
            return
        }
        failedCheckIds = []
        var checks: [[String: Any]] = []

        // apple_first_login_llm runs first: its 30 fixture-driven launches are
        // independent of the live staging session and must not disturb it.
        checks.append(
            runCheck("apple_first_login_llm", recorder: recorder) {
                try self.runFirstLoginTrials()
            })

        var liveBlocked: String?
        checks.append(
            runCheck("sign_in", recorder: recorder) {
                do {
                    return try self.performLiveSignIn(recorder: recorder)
                } catch {
                    liveBlocked = "sign_in"
                    throw error
                }
            })

        checks.append(
            runCheck("rendered_chat", recorder: recorder, blockedBy: liveBlocked) {
                do {
                    return try self.runRenderedChat()
                } catch {
                    liveBlocked = liveBlocked ?? "rendered_chat"
                    throw error
                }
            })

        checks.append(
            runCheck("reconnect_resume", recorder: recorder, blockedBy: liveBlocked) {
                try self.runResumeTrials()
            })

        checks.append(
            runCheck("agent_lifecycle", recorder: recorder, blockedBy: liveBlocked) {
                try self.observeAgentLifecycle(recorder: recorder)
            })

        checks.append(
            runCheck("personal_agent", recorder: recorder, blockedBy: liveBlocked) {
                try self.runAuthoringFlow()
            })

        checks.append(
            runCheck("accessibility_semantics", recorder: recorder, blockedBy: liveBlocked) {
                try self.runAccessibilityInspection()
            })

        if recorder.platform == "macos" {
            checks.append(
                runCheck("macos_personal_agent_host", recorder: recorder) {
                    self.macOSPersonalAgentHostProduction(recorder: recorder, liveBlocked: liveBlocked)
                })
        }

        let outcome = failedCheckIds.isEmpty ? "passed" : "failed"
        do {
            let path = try recorder.writeReport(checks: checks, outcome: outcome)
            if let data = FileManager.default.contents(atPath: path) {
                let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
                attachment.name = "\(recorder.platform)-platform-evidence"
                attachment.lifetime = .keepAlways
                add(attachment)
            }
        } catch {
            XCTFail("failed to write the platform evidence report: \(error)")
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
        recorder: EvidenceRecorder,
        blockedBy prerequisite: String? = nil,
        body: () throws -> CheckProduction
    ) -> [String: Any] {
        let started = Date()
        if let prerequisite {
            failedCheckIds.append(id)
            var production = CheckProduction()
            production.failureCode = "prerequisite_check_failed"
            production.raw = ["blocked_by": prerequisite]
            return recorder.checkRecord(id: id, production: production, startedAt: started)
        }
        do {
            let production = try body()
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

    // MARK: - apple_first_login_llm (30 fixture trials; helpers duplicated from
    // LLMFirstLoginUITests by design — that file is owned by T072 and not edited)

    private func runFirstLoginTrials() throws -> CheckProduction {
        var acknowledgementMs: [Double] = []
        var responsiveMs: [Double] = []
        var terminalMs: [Double] = []
        var successesWithinFiveSeconds = 0
        let trials = 30

        for trial in 1...trials {
            let fixture = XCUIApplication()
            fixture.launchArguments = ["--astral-ui-test-first-login", "slow-success"]
            fixture.launchEnvironment["ASTRAL_UI_TESTING"] = "1"
            fixture.launch()
            defer { fixture.terminate() }

            let apiKey = fixture.secureTextFields["param-field-api_key"]
            guard apiKey.waitForExistence(timeout: 10) else {
                throw EvidenceFailure(
                    code: "first_login_fixture_unavailable",
                    message: "provider form did not appear on trial \(trial)")
            }
            apiKey.tap()
            apiKey.typeText("ui-only-placeholder")
            let save = fixture.buttons["llm-save-button"]
            guard save.waitForExistence(timeout: 2) else {
                throw EvidenceFailure(
                    code: "first_login_fixture_unavailable",
                    message: "save control did not appear on trial \(trial)")
            }

            save.tap()
            let tappedAt = Date()
            let status = fixture.descendants(matching: .any)["llm-save-status"]
            let acknowledged = status.waitForExistence(timeout: 2)
            let acknowledgedAt = Date()
            let acknowledgementLatency = acknowledgedAt.timeIntervalSince(tappedAt) * 1000
            acknowledgementMs.append(acknowledgementLatency)

            // Responsiveness while the operation is active: one accessibility
            // round trip through the app's main run loop must stay prompt.
            let probeStart = Date()
            _ = apiKey.isEnabled
            responsiveMs.append(Date().timeIntervalSince(probeStart) * 1000)

            let form = fixture.descendants(matching: .any)["llm-provider-form-title"]
            let completed = form.waitForNonExistence(timeout: 10)
            let terminal = Date().timeIntervalSince(tappedAt) * 1000
            terminalMs.append(completed ? terminal : 10_000)
            if acknowledged && completed && terminal - acknowledgementLatency <= 5000 {
                successesWithinFiveSeconds += 1
            }
        }

        let ackP95 = percentile(0.95, sorted: acknowledgementMs.sorted())
        let responsiveP95 = percentile(0.95, sorted: responsiveMs.sorted())
        let terminalMax = terminalMs.max() ?? 0
        let successRate = Double(successesWithinFiveSeconds) / Double(trials) * 100

        var production = CheckProduction()
        production.measurements = [
            measurementRecord(
                metric: "trial_count", aggregation: "total", value: Double(trials),
                unit: "count", sampleCount: trials, comparator: "gte", threshold: 30),
            measurementRecord(
                metric: "acknowledgement_p95_ms", aggregation: "p95", value: ackP95,
                unit: "milliseconds", sampleCount: acknowledgementMs.count,
                comparator: "lte", threshold: 250),
            measurementRecord(
                metric: "success_within_five_seconds_percent", aggregation: "rate",
                value: successRate, unit: "percent", sampleCount: trials,
                comparator: "gte", threshold: 95),
            measurementRecord(
                metric: "terminal_max_ms", aggregation: "maximum", value: terminalMax,
                unit: "milliseconds", sampleCount: terminalMs.count,
                comparator: "lte", threshold: 10_000),
            measurementRecord(
                metric: "responsive_interaction_p95_ms", aggregation: "p95",
                value: responsiveP95, unit: "milliseconds",
                sampleCount: responsiveMs.count, comparator: "lte", threshold: 250),
        ]
        production.raw = [
            "scenario": "slow-success",
            "trial_count": trials,
            "acknowledgement_ms": acknowledgementMs.map { Int($0.rounded()) },
            "responsive_ms": responsiveMs.map { Int($0.rounded()) },
            "terminal_ms": terminalMs.map { Int($0.rounded()) },
            "successes_within_five_seconds": successesWithinFiveSeconds,
        ]
        if ackP95 > 250 || responsiveP95 > 250 || terminalMax > 10_000 || successRate < 95 {
            production.failureCode = "first_login_floor_missed"
        }
        return production
    }

    // MARK: - sign_in (live Keycloak, ASWebAuthenticationSession)

    private func launchLiveApp(recorder: EvidenceRecorder) {
        if app == nil { app = XCUIApplication() }
        var arguments = ["-serverBase", recorder.baseURL]
        if let authority = recorder.authorityOverride {
            arguments.append(contentsOf: ["-authority", authority])
        }
        app.launchArguments = arguments
        app.launch()
    }

    private func performLiveSignIn(recorder: EvidenceRecorder) throws -> CheckProduction {
        let started = Date()
        launchLiveApp(recorder: recorder)
        let composer = app.textFields["chat-composer-input"]
        if !composer.waitForExistence(timeout: 5) {
            let ssoButton = app.buttons.matching(
                NSPredicate(format: "label CONTAINS[c] %@", "sign in")
            ).firstMatch
            guard ssoButton.waitForExistence(timeout: 20) else {
                throw EvidenceFailure(
                    code: "sign_in_entry_unavailable",
                    message: "neither the workspace nor the SSO sign-in affordance appeared")
            }
            ssoButton.tap()
            confirmAuthenticationSessionConsent()

            let webView = app.webViews.firstMatch
            guard webView.waitForExistence(timeout: 45) else {
                throw EvidenceFailure(
                    code: "sign_in_web_form_unavailable",
                    message: "the Keycloak web form was not presented")
            }
            let usernameField = webView.textFields.firstMatch
            guard usernameField.waitForExistence(timeout: 30) else {
                throw EvidenceFailure(
                    code: "sign_in_web_form_unavailable",
                    message: "the Keycloak username field was not exposed")
            }
            usernameField.tap()
            usernameField.typeText(recorder.username)
            let passwordField = webView.secureTextFields.firstMatch
            guard passwordField.waitForExistence(timeout: 10) else {
                throw EvidenceFailure(
                    code: "sign_in_web_form_unavailable",
                    message: "the Keycloak password field was not exposed")
            }
            passwordField.tap()
            passwordField.typeText(recorder.password)
            let submit = webView.buttons.matching(
                NSPredicate(format: "label CONTAINS[c] %@ OR label CONTAINS[c] %@", "sign in", "log in")
            ).firstMatch
            if submit.exists {
                submit.tap()
            } else {
                passwordField.typeText("\n")
            }
            guard composer.waitForExistence(timeout: 90) else {
                throw EvidenceFailure(
                    code: "sign_in_flow_incomplete",
                    message: "the authenticated workspace did not appear after Keycloak sign-in "
                        + "(the staging identity must be pre-provisioned with an LLM configuration)")
            }
        }
        let duration = Date().timeIntervalSince(started)
        var production = CheckProduction()
        production.raw = [
            "method": "keycloak_ui_authorization_code_pkce_aswebauthenticationsession",
            "duration_ms": Int(duration * 1000),
            "authenticated_workspace_reached": true,
        ]
        return production
    }

    private func confirmAuthenticationSessionConsent() {
        #if os(iOS)
            let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
            let confirm = springboard.buttons["Continue"]
            if confirm.waitForExistence(timeout: 10) { confirm.tap() }
        #else
            let confirm = app.buttons["Continue"]
            if confirm.waitForExistence(timeout: 10) { confirm.tap() }
        #endif
    }

    // MARK: - rendered_chat

    private func userPromptElement() -> XCUIElement {
        app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS[c] %@", "Roll exactly six")
        ).firstMatch
    }

    private func assistantDiceElement() -> XCUIElement {
        app.staticTexts.matching(
            NSPredicate(
                format: "(label CONTAINS[c] %@ OR label CONTAINS[c] %@) AND NOT (label CONTAINS[c] %@)",
                "six-sided", "d6", "Roll exactly six")
        ).firstMatch
    }

    private func runRenderedChat() throws -> CheckProduction {
        let started = Date()
        let composer = app.textFields["chat-composer-input"]
        guard composer.waitForExistence(timeout: 15) else {
            throw EvidenceFailure(code: "rendered_chat_unobserved", message: "chat composer is unavailable")
        }
        composer.tap()
        composer.typeText(releasePrompt + "\n")
        guard userPromptElement().waitForExistence(timeout: 15) else {
            throw EvidenceFailure(
                code: "rendered_chat_unobserved", message: "the submitted prompt did not render")
        }
        guard assistantDiceElement().waitForExistence(timeout: 240) else {
            throw EvidenceFailure(
                code: "rendered_chat_unobserved",
                message: "no normalized dice response rendered within the release budget")
        }
        var production = CheckProduction()
        production.raw = [
            "prompt_sha256": sha256Hex(Data(releasePrompt.utf8)),
            "duration_ms": Int(Date().timeIntervalSince(started) * 1000),
            "normalized_dice_contract_observed": true,
        ]
        return production
    }

    // MARK: - reconnect_resume (>= 20 relaunch trials with counters)

    private func runResumeTrials() throws -> CheckProduction {
        var latenciesMs: [Double] = []
        var successes = 0
        let trials = 20

        for trial in 1...trials {
            app.terminate()
            let startedTrial = Date()
            app.launch()
            var restored = userPromptElement().waitForExistence(timeout: 5)
            if restored {
                let remaining = max(0.1, 5 - Date().timeIntervalSince(startedTrial))
                restored = assistantDiceElement().waitForExistence(timeout: remaining)
            }
            let latency = Date().timeIntervalSince(startedTrial) * 1000
            latenciesMs.append(latency)
            if restored && latency <= 5000 { successes += 1 }
            if trial == trials {
                let screenshot = XCTAttachment(screenshot: app.screenshot())
                screenshot.name = "\(compiledPlatform)-release-resume-twentieth-relaunch"
                screenshot.lifetime = .keepAlways
                add(screenshot)
            }
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
            "max_latency_ms": Int((latenciesMs.max() ?? 0).rounded()),
        ]
        if successes < trials {
            production.failureCode = "resume_trials_below_floor"
        }
        return production
    }

    // MARK: - agent_lifecycle (surfaced lifecycle labels, no reload)

    private func openSettingsMenuItem(_ label: String) throws {
        let settings = app.buttons["Settings"]
        guard settings.waitForExistence(timeout: 10) else {
            throw EvidenceFailure(code: "chrome_menu_unavailable", message: "the Settings control is missing")
        }
        settings.tap()
        #if os(macOS)
            let item = app.menuItems[label]
        #else
            let item = app.buttons[label]
        #endif
        guard item.waitForExistence(timeout: 10) else {
            throw EvidenceFailure(
                code: "chrome_menu_unavailable",
                message: "the server-owned menu does not expose \(label)")
        }
        item.tap()
    }

    private func waitUntil(timeout: TimeInterval, condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        }
        return condition()
    }

    private func observeAgentLifecycle(recorder: EvidenceRecorder) throws -> CheckProduction {
        let started = Date()
        try openSettingsMenuItem("Agents & permissions")
        let lifecycleTexts = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS %@", " status: "))
        guard waitUntil(timeout: 120, condition: { lifecycleTexts.count > 0 }) else {
            throw EvidenceFailure(
                code: "lifecycle_states_unobserved",
                message: "no agent lifecycle label surfaced on the Agents screen "
                    + "(the readiness workflow must induce a lifecycle transition)")
        }
        let observed = (0..<min(lifecycleTexts.count, 16)).map {
            lifecycleTexts.element(boundBy: $0).label
        }
        if let fragment = recorder.lifecycleAgentFragment,
            !observed.contains(where: { $0.localizedCaseInsensitiveContains(fragment) })
        {
            throw EvidenceFailure(
                code: "lifecycle_states_unobserved",
                message: "no lifecycle label matched ASTRAL_RELEASE_LIFECYCLE_AGENT_ID")
        }
        var production = CheckProduction()
        production.raw = [
            "observed_labels": observed,
            "duration_ms": Int(Date().timeIntervalSince(started) * 1000),
        ]
        return production
    }

    // MARK: - personal_agent (five-phase Analyze-gated authoring, native SDUI surface)

    private func firstEmptyOrType(_ element: XCUIElement, _ text: String) {
        let current = (element.value as? String) ?? ""
        if current.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            element.tap()
            element.typeText(text)
        }
    }

    private func runAuthoringFlow() throws -> CheckProduction {
        let started = Date()
        try openSettingsMenuItem("My agents")

        let name = "Release 060 \(String(UUID().uuidString.lowercased().prefix(8)))"
        let nameField = app.textFields["param-field-agent_name"]
        guard nameField.waitForExistence(timeout: 60) else {
            throw EvidenceFailure(
                code: "authoring_flow_incomplete", message: "the authoring start form did not render")
        }
        nameField.tap()
        nameField.typeText(name)
        let descriptionField = app.textViews["param-field-description"]
        guard descriptionField.waitForExistence(timeout: 10) else {
            throw EvidenceFailure(
                code: "authoring_flow_incomplete", message: "the authoring description field is missing")
        }
        descriptionField.tap()
        descriptionField.typeText(
            "Greet only its owner using a deterministic local tool and no network access.")
        app.buttons["param-action-chrome_author_start"].tap()

        // Specify — assistant-drafted on the owner's LLM; keep or seed the artifact.
        let specification = app.textViews["param-field-specification"]
        guard specification.waitForExistence(timeout: 180) else {
            throw EvidenceFailure(
                code: "authoring_flow_incomplete", message: "the Specify phase did not render")
        }
        firstEmptyOrType(
            specification,
            "Greet the owner on request. Use one local greet tool and return a short plain-text greeting.")
        app.buttons["param-action-chrome_author_advance"].tap()

        // Clarify — the HARD GATE: every question must carry an answer.
        let answers = app.textViews.matching(
            NSPredicate(format: "identifier BEGINSWITH %@", "param-field-q"))
        guard waitUntil(timeout: 180, condition: { answers.count > 0 }) else {
            throw EvidenceFailure(
                code: "authoring_flow_incomplete", message: "the Clarify phase did not render")
        }
        for index in 0..<answers.count {
            firstEmptyOrType(
                answers.element(boundBy: index),
                "Use deterministic owner-only behavior with no external egress.")
        }
        app.buttons["param-action-chrome_author_clarify"].tap()

        // Plan
        let tools = app.textViews["param-field-tools"]
        guard tools.waitForExistence(timeout: 180) else {
            throw EvidenceFailure(
                code: "authoring_flow_incomplete", message: "the Plan phase did not render")
        }
        firstEmptyOrType(tools, "greet | tools:read | greet the owner")
        firstEmptyOrType(app.textFields["param-field-scopes"], "tools:read")
        app.buttons["param-action-chrome_author_advance"].tap()

        // Tasks
        let tasks = app.textViews["param-field-tasks"]
        guard tasks.waitForExistence(timeout: 180) else {
            throw EvidenceFailure(
                code: "authoring_flow_incomplete", message: "the Tasks phase did not render")
        }
        firstEmptyOrType(tasks, "Validate the request\nCall greet once\nReturn the greeting")
        app.buttons["param-action-chrome_author_advance"].tap()

        // Analyze — a violation produces no code; only an explicit pass counts.
        let analyze = app.buttons["Run Analyze"]
        guard analyze.waitForExistence(timeout: 180) else {
            throw EvidenceFailure(
                code: "authoring_flow_incomplete", message: "the Analyze phase did not render")
        }
        analyze.tap()
        let passed = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS %@", "Analyze passed")
        ).firstMatch
        guard passed.waitForExistence(timeout: 180) else {
            throw EvidenceFailure(
                code: "authoring_flow_incomplete", message: "Analyze did not report a pass")
        }

        let back = app.buttons["← My agents"]
        if back.exists { back.tap() }
        let newChat = app.buttons["New chat"]
        if newChat.waitForExistence(timeout: 10) { newChat.tap() }

        var production = CheckProduction()
        production.raw = [
            "analyze_passed": true,
            "generated_test_identity_sha256": sha256Hex(Data(name.utf8)),
            "duration_ms": Int(Date().timeIntervalSince(started) * 1000),
        ]
        return production
    }

    // MARK: - accessibility_semantics

    private func runAccessibilityInspection() throws -> CheckProduction {
        let started = Date()
        let composer = app.textFields["chat-composer-input"]
        guard composer.waitForExistence(timeout: 15) else {
            throw EvidenceFailure(
                code: "accessibility_contract_violation", message: "chat composer is unavailable")
        }
        var violations: [String] = []
        if composer.label != "Message AstralDeep" {
            violations.append("composer label is \(composer.label)")
        }
        if !composer.isEnabled { violations.append("composer is disabled") }
        if !composer.isHittable { violations.append("composer is not hittable") }

        for (name, element) in [
            ("New chat", app.buttons["New chat"]),
            ("Recent chats", app.buttons["Recent chats"]),
            ("Settings", app.buttons["Settings"]),
            ("Attach a file", app.buttons["Attach a file"]),
        ] {
            if !element.waitForExistence(timeout: 5) {
                violations.append("\(name) control is missing its accessibility name")
            } else if !element.isEnabled {
                violations.append("\(name) control is disabled")
            }
        }

        #if os(iOS)
            composer.tap()
            if !app.keyboards.firstMatch.waitForExistence(timeout: 3) {
                violations.append("the system keyboard did not appear for the composer")
            }
        #endif

        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "\(compiledPlatform)-release-accessibility"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        let hierarchy = XCTAttachment(
            data: Data(app.debugDescription.utf8), uniformTypeIdentifier: "public.plain-text")
        hierarchy.name = "\(compiledPlatform)-release-accessibility-hierarchy"
        hierarchy.lifetime = .keepAlways
        add(hierarchy)

        var production = CheckProduction()
        production.raw = [
            "inspected_controls": ["chat-composer-input", "New chat", "Recent chats", "Settings", "Attach a file"],
            "violations": violations,
            "duration_ms": Int(Date().timeIntervalSince(started) * 1000),
        ]
        if !violations.isEmpty {
            production.failureCode = "accessibility_contract_violation"
        }
        return production
    }

    // MARK: - macos_personal_agent_host (branched ONLY from the recorded candidate capability)

    private func macOSPersonalAgentHostProduction(
        recorder: EvidenceRecorder,
        liveBlocked: String?
    ) -> CheckProduction {
        var production = CheckProduction()
        guard let path = environmentValue("ASTRAL_STAGING_CAPABILITY_FILE") else {
            production.failureCode = "capability_map_missing"
            production.raw = ["failure": "ASTRAL_STAGING_CAPABILITY_FILE was not provided"]
            return production
        }
        guard let data = FileManager.default.contents(atPath: path),
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else {
            production.failureCode = "capability_map_unreadable"
            production.raw = ["failure": "cannot parse the candidate capability file", "path": path]
            return production
        }
        let capability = (object["macos_personal_agent_host"] as? [String: Any]) ?? object
        guard let supported = capability["supported"] as? Bool,
            let versionsRaw = capability["runtime_contract_versions"] as? [Any],
            capability["source"] as? String == "candidate_capability_map"
        else {
            production.failureCode = "capability_map_malformed"
            production.raw = ["failure": "capability map lacks the canonical shape"]
            return production
        }
        if let stagedCapability = recorder.staging["macos_personal_agent_host"] as? [String: Any],
            let stagedSupported = stagedCapability["supported"] as? Bool,
            stagedSupported != supported
        {
            production.failureCode = "capability_map_inconsistent"
            production.raw = [
                "failure": "capability file and trusted stage outputs disagree on supported"
            ]
            return production
        }

        if supported == false {
            let sourceFeature = capability["source_feature"]
            guard versionsRaw.isEmpty, sourceFeature == nil || sourceFeature is NSNull else {
                production.failureCode = "capability_map_malformed"
                production.raw = [
                    "failure": "supported=false requires empty runtime_contract_versions and null source_feature"
                ]
                return production
            }
            production.applicabilityReason =
                "candidate capability map records macos_personal_agent_host supported=false with no "
                + "runtime contract versions (source: candidate_capability_map)"
            production.raw = [
                "supported": false,
                "runtime_contract_versions": [Int](),
                "source": "candidate_capability_map",
            ]
            return production
        }

        // supported == true: the exercised macOS artifact must complete the
        // structured v2 registration and the server must acknowledge with
        // agent_host_registered; anything unobserved is a host FAILURE.
        let versions = versionsRaw.compactMap { $0 as? Int }
        guard versions.contains(2), capability["source_feature"] as? String == "059" else {
            production.failureCode = "capability_map_malformed"
            production.raw = [
                "failure": "supported=true requires runtime contract version 2 and source_feature 059"
            ]
            return production
        }
        if liveBlocked != nil {
            production.failureCode = "prerequisite_check_failed"
            production.raw = ["blocked_by": liveBlocked ?? "sign_in"]
            return production
        }
        do {
            try openSettingsMenuItem("My agents")
            let hostOnline = app.staticTexts.matching(
                NSPredicate(
                    format: "label CONTAINS %@",
                    "Your agents run on your desktop host, not on the server.")
            ).firstMatch
            guard hostOnline.waitForExistence(timeout: 60) else {
                production.failureCode = "agent_host_registered_not_observed"
                production.raw = [
                    "failure": "the macOS client never surfaced an acknowledged v2 host registration"
                ]
                return production
            }
            production.raw = [
                "supported": true,
                "runtime_contract_versions": versions,
                "source_feature": "059",
                "host_online_note_observed": true,
            ]
            return production
        } catch let failure as EvidenceFailure {
            production.failureCode = failure.code
            production.raw = ["failure": failure.message]
            return production
        } catch {
            production.failureCode = "unexpected_error"
            production.raw = ["failure": String(describing: error)]
            return production
        }
    }
}
