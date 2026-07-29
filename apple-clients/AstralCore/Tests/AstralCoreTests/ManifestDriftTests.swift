// Feature 051 (FR-038) — manifest drift guard, the Swift twin of
// windows-client/tests/test_protocol_manifest.py and the Android
// VocabularyParityTest: if backend/shared/ui_protocol.json changes vocabulary
// and the Apple dispositions don't account for it, this suite fails CI.
import XCTest

@testable import AstralCore

final class ManifestDriftTests: XCTestCase {

    struct Manifest: Decodable {
        struct Named: Decodable { let name: String }
        struct AdmissionRefusalContract: Decodable {
            let type: String
            let exactFields: [String]
            let submissionId: String
            let accepted: Bool
            let additionalFields: Bool
            let codes: [String]

            enum CodingKeys: String, CodingKey {
                case type
                case exactFields = "exact_fields"
                case submissionId = "submission_id"
                case accepted
                case additionalFields = "additional_fields"
                case codes
            }
        }
        struct FrameContracts: Decodable {
            let admissionRefusal: AdmissionRefusalContract

            enum CodingKeys: String, CodingKey {
                case admissionRefusal = "admission_refusal"
            }
        }
        struct AdditiveField: Decodable {
            let field: String
            let carriedOn: [String]

            enum CodingKeys: String, CodingKey {
                case field
                case carriedOn = "carried_on"
            }
        }
        // ui_protocol.json shape: push_types are {name, category} objects;
        // component_types and accept_actions are plain strings.
        let pushTypes: [Named]
        let componentTypes: [String]
        let acceptActions: [String]
        let additiveFields: [AdditiveField]
        let frameContracts: FrameContracts

        enum CodingKeys: String, CodingKey {
            case pushTypes = "push_types"
            case componentTypes = "component_types"
            case acceptActions = "accept_actions"
            case additiveFields = "additive_fields"
            case frameContracts = "frame_contracts"
        }
    }

    /// Walk up from this file until the committed manifest is found.
    static func manifestURL() throws -> URL {
        var dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        for _ in 0..<8 {
            let candidate = dir.appendingPathComponent("backend/shared/ui_protocol.json")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            dir.deleteLastPathComponent()
        }
        throw XCTSkip("ui_protocol.json not found (package checked out standalone)")
    }

    func loadManifest() throws -> Manifest {
        let data = try Data(contentsOf: try Self.manifestURL())
        return try JSONDecoder().decode(Manifest.self, from: data)
    }

    func testManifestVocabularyMatchesEmbeddedLists() throws {
        let manifest = try loadManifest()
        XCTAssertEqual(
            Set(manifest.pushTypes.map(\.name)),
            Set(ClientDispositions.allPushTypes),
            "push_types drift — update Dispositions.swift + parity matrix")
        XCTAssertEqual(
            Set(manifest.componentTypes),
            Set(ClientDispositions.allComponentTypes),
            "component_types drift — update Dispositions.swift + parity matrix")
        // 58 = 51 + the seven feature-060 reliability frames
        // (conversation_snapshot, operation_status, agent_lifecycle,
        // conversation_commit_ready, and three agent_host_* control frames).
        // Host-only frames remain explicitly ignored by author-only clients;
        // macOS hosting is enabled only by feature 059.
        XCTAssertEqual(manifest.pushTypes.count, 58)
        XCTAssertEqual(manifest.componentTypes.count, 35)
        // 73 = 67 + the four feature-054 chrome_llm_sys_* admin actions
        //        + the two feature-055 component_refine/component_restore actions.
        // 87 = 73 + the feature-058 BYO authoring + agent-management chrome actions
        //        (chrome_author_* / chrome_agent_*).
        // 91 = 87 + the four feature-063 remote-compute actions
        //        (chrome_machine_add/probe/delete + remote_op_decision) —
        //        all posted through the generic SDUI action path, no new
        //        client code.
        // 94 = 91 + the T026 machine-credential/re-trust actions
        //        (chrome_machine_credential_set/delete + chrome_machine_retrust),
        //        same generic path.
        XCTAssertEqual(manifest.acceptActions.count, 94)
    }

    func testRuntimeReliabilityFramesAndRegistrationDisposition() throws {
        let manifest = try loadManifest()
        let required = Set([
            "conversation_snapshot", "operation_status", "agent_lifecycle",
            "agent_host_inventory_reconciled", "agent_host_registered",
            "agent_host_registration_refused", "conversation_commit_ready",
        ])
        XCTAssertTrue(required.isSubset(of: Set(manifest.pushTypes.map(\.name))))

        let registrations = manifest.additiveFields.filter {
            $0.field == "agent_host" && $0.carriedOn == ["register_ui"]
        }
        XCTAssertEqual(
            registrations.count, 1,
            "manifest must declare structured register_ui.agent_host exactly once")

        let hostFrames = [
            "agent_host_inventory_reconciled", "agent_host_registered",
            "agent_host_registration_refused",
        ]
        for client in [ClientDispositions.ios, ClientDispositions.macos, ClientDispositions.watch] {
            for frame in hostFrames {
                guard let disposition = client.frames[frame] else {
                    XCTFail("\(client.client) must classify \(frame)")
                    continue
                }
                if case .ignored(_) = disposition {
                    // Expected: Apple clients remain author-only until feature 059.
                } else {
                    XCTFail("\(client.client) must explicitly ignore host-only \(frame)")
                }
            }
        }
    }

    func testManifestPinsExactAdmissionRefusalContract() throws {
        let contract = try loadManifest().frameContracts.admissionRefusal
        XCTAssertEqual(contract.type, "error")
        XCTAssertEqual(
            contract.exactFields,
            [
                "type", "submission_id", "accepted", "code", "message",
                "retryable", "retry_after_ms",
            ])
        XCTAssertEqual(contract.submissionId, "canonical_lowercase_uuid4")
        XCTAssertFalse(contract.accepted)
        XCTAssertFalse(contract.additionalFields)
        XCTAssertEqual(
            contract.codes,
            [
                "capacity_exceeded", "registration_required", "registration_timeout",
                "idempotency_conflict", "connection_closing", "service_draining",
                "invalid_input", "registration_queue_full", "operation_failed",
            ])
    }

    func testEveryClientDispositionsEveryFrameAndComponent() throws {
        for client in [ClientDispositions.ios, ClientDispositions.macos, ClientDispositions.watch] {
            for name in ClientDispositions.allPushTypes {
                XCTAssertNotNil(
                    client.frames[name],
                    "\(client.client): missing frame disposition for \(name)")
            }
            for name in ClientDispositions.allComponentTypes {
                XCTAssertNotNil(
                    client.components[name],
                    "\(client.client): missing component disposition for \(name)")
            }
            // No disposition for a name the manifest doesn't have (stale row).
            for name in client.frames.keys {
                XCTAssertTrue(
                    ClientDispositions.allPushTypes.contains(name),
                    "\(client.client): stale frame disposition \(name)")
            }
            for name in client.components.keys {
                XCTAssertTrue(
                    ClientDispositions.allComponentTypes.contains(name),
                    "\(client.client): stale component disposition \(name)")
            }
        }
    }

    func testWatchHandlesNotificationForBackgroundContinuity() {
        // 055 background-task continuity: a completion notification must reach
        // the wrist. There is no watch test target to pin the reduce, so this
        // pins the disposition — a regression to .ignored would silently drop
        // background completions on the watch again.
        XCTAssertEqual(ClientDispositions.watch.frames["notification"], .handled)
    }

    func testWatchNativeSetIsWithinProfileVocabulary() {
        // The watch renders natively exactly what the watch ROTE profile can
        // emit — charts/tables/code are degraded server-side and must NOT be
        // advertised as natively supported.
        let native = Set(ClientDispositions.watch.nativeComponentTypes)
        for forbidden in [
            "bar_chart", "line_chart", "pie_chart", "plotly_chart",
            "table", "code", "tabs", "file_upload", "file_download",
        ] {
            XCTAssertFalse(
                native.contains(forbidden),
                "watch must not advertise \(forbidden) as native")
        }
        for required in ["text", "alert", "list", "metric", "keyvalue"] {
            XCTAssertTrue(native.contains(required))
        }
    }

    func testSupportedTypesRideOnDeviceDescriptors() {
        XCTAssertEqual(
            Set(
                DeviceDescriptor.watch(viewportWidth: 200, viewportHeight: 240)
                    .supportedTypes),
            Set(ClientDispositions.watch.nativeComponentTypes))
        XCTAssertFalse(
            DeviceDescriptor.ios(viewportWidth: 390, viewportHeight: 844)
                .supportedTypes.isEmpty)
    }
}
