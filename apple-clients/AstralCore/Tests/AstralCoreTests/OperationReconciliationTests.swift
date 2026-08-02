import Foundation
import XCTest

@testable import AstralCore

final class OperationReconciliationTests: XCTestCase {
    private let operationId = "22222222-2222-4222-8222-222222222222"
    private let submissionId = "33333333-3333-4333-8333-333333333333"
    private let request = "44444444-4444-4444-8444-444444444444"

    private func operationJSON(state: String = "completed") -> JSONValue {
        .object([
            "operation_id": .string(operationId),
            "operation_kind": .string("llm_credential_save"),
            "admission_class": .string("interactive"),
            "owner_scope": .string("user"),
            "chat_id": .null,
            "parent_operation_id": .null,
            "connection_generation": .string("11111111-1111-4111-8111-111111111111"),
            "request_generation": .string(request),
            "state": .string(state),
            "phase_code": .string(state),
            "terminal_code": .null,
            "safe_summary": .string("Saved"),
            "retry_after_ms": .null,
            "state_revision": .number(3),
            "accepted_at": .string("2026-07-15T18:41:00Z"),
            "queue_deadline_at": .null,
            "started_at": .string("2026-07-15T18:41:00Z"),
            "terminal_at": .string("2026-07-15T18:41:01Z"),
            "updated_at": .string("2026-07-15T18:41:01Z"),
            "purge_after": .string("2026-07-16T18:41:01Z"),
        ])
    }

    func testProjectionRejectsMalformedIdentityStateAndRevision() {
        XCTAssertEqual(OperationProjection(json: operationJSON())?.state, "completed")

        var malformedId = operationJSON().objectValue!
        malformedId["operation_id"] = .string("not-a-uuid")
        XCTAssertNil(OperationProjection(json: .object(malformedId)))

        var malformedState = operationJSON().objectValue!
        malformedState["state"] = .string("finished")
        XCTAssertNil(OperationProjection(json: .object(malformedState)))

        var malformedRevision = operationJSON().objectValue!
        malformedRevision["state_revision"] = .number(1.5)
        XCTAssertNil(OperationProjection(json: .object(malformedRevision)))
    }

    func testAuthenticatedOperationAndSubmissionLookupsDecodeRetainedResults() async throws {
        let operation = operationJSON()
        let operationId = operationId
        let submissionId = submissionId
        let client = RestClient(
            serverBase: URL(string: "https://astral.example.test")!,
            tokenProvider: { "token" },
            transport: { request in
                XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer token")
                XCTAssertEqual(request.value(forHTTPHeaderField: "Cache-Control"), "no-store")
                XCTAssertEqual(request.value(forHTTPHeaderField: "Pragma"), "no-cache")
                XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalAndRemoteCacheData)
                if request.url?.path == "/api/operations/\(operationId)" {
                    return (200, try operation.encoded())
                }
                if request.url?.path == "/api/operation-submissions/\(submissionId)" {
                    return (
                        200,
                        try JSONValue.object([
                            "accepted": .bool(true),
                            "operation": operation,
                        ]).encoded()
                    )
                }
                return (404, Data())
            })

        let retainedOperation = try await client.operation(id: operationId)
        let retainedSubmission = try await client.operationSubmission(id: submissionId)
        let missingOperation = try await client.operation(id: submissionId)

        XCTAssertEqual(retainedOperation?.requestGeneration, request)
        XCTAssertEqual(retainedSubmission, .accepted(try XCTUnwrap(OperationProjection(json: operation))))
        XCTAssertNil(missingOperation)
    }

    func testRefusalAndMalformedSuccessStayDistinct() async throws {
        let refused = RestClient(
            serverBase: URL(string: "https://astral.example.test")!,
            tokenProvider: { nil },
            transport: { _ in
                (
                    200,
                    try JSONValue.object([
                        "accepted": .bool(false),
                        "code": .string("capacity_exceeded"),
                        "retryable": .bool(true),
                        "retry_after_ms": .number(500),
                    ]).encoded()
                )
            })
        let refusal = try await refused.operationSubmission(id: submissionId)
        XCTAssertEqual(refusal, .refused(code: "capacity_exceeded", retryable: true, retryAfterMs: 500))

        let malformed = RestClient(
            serverBase: URL(string: "https://astral.example.test")!,
            tokenProvider: { nil },
            transport: { _ in (200, Data(#"{"accepted":true}"#.utf8)) })
        do {
            _ = try await malformed.operationSubmission(id: submissionId)
            XCTFail("malformed success must fail closed")
        } catch {
            XCTAssertEqual((error as? URLError)?.code, .cannotParseResponse)
        }
    }
}
