import AstralCore
import XCTest

@testable import AstralWatch

@MainActor
final class StatusLifecycleTests: XCTestCase {
    private let chat = "11111111-1111-4111-8111-111111111111"
    private let connection = "22222222-2222-4222-8222-222222222222"
    private let request = "33333333-3333-4333-8333-333333333333"
    private let operation = "44444444-4444-4444-8444-444444444444"
    private let reconnect = "88888888-8888-4888-8888-888888888888"

    private func inbound(_ text: String) -> InboundFrame {
        InboundFrame.parse(text)!
    }

    private func capturedFrame(_ model: WatchModel, action: () -> Void) -> JSONValue {
        var captured: JSONValue?
        model.outboundTap = { text in
            captured = try? JSONValue.parse(Data(text.utf8))
        }
        action()
        return captured!
    }

    private func preparedModel() -> WatchModel {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        XCTAssertTrue(
            model.openConversationRequest(
                chatId: chat,
                requestGeneration: request,
                purpose: .commit))
        return model
    }

    func testWatchRendersOperationAndLifecycleWithoutReload() {
        let model = preparedModel()
        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"curated_example","surface":"chat","chat_id":"\(chat)",
                 "connection_generation":"\(connection)","request_generation":"\(request)",
                 "sequence":1,"state":"running","phase":"running","label":"Working…",
                 "terminal":false,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:00Z"}
                """))
        XCTAssertEqual(model.operationStatuses[operation]?.state, "running")
        XCTAssertEqual(model.statusText, "Working…")
        XCTAssertTrue(model.statusShowsActivity)

        model.handleFrame(
            inbound(
                """
                {"type":"agent_lifecycle","agent_id":"ua-dice","revision_id":null,
                 "runtime_instance_id":null,"lifecycle_generation":3,"state_revision":4,
                 "state":"offline","reason_code":"host_lost","label":"Agent offline",
                 "updated_at":"2026-07-16T12:00:01Z"}
                """))
        XCTAssertEqual(model.agentLifecycles["ua-dice"]?.state, "offline")
        XCTAssertEqual(model.statusText, "ua-dice: Agent offline")
        XCTAssertFalse(model.statusShowsActivity)
        XCTAssertEqual(model.rootStatusText, "ua-dice: Agent offline")
    }

    func testOnlyNonterminalChatProgressShowsActivity() {
        let model = WatchModel()

        model.handleFrame(
            inbound(
                #"{"type":"chat_status","status":"thinking","message":"Checking records…"}"#))
        XCTAssertEqual(model.statusText, "Checking records…")
        XCTAssertTrue(model.statusShowsActivity)

        model.handleFrame(
            inbound(
                #"{"type":"chat_step","step":{"name":"lookup","status":"completed"}}"#))
        XCTAssertEqual(model.statusText, "lookup")
        XCTAssertFalse(model.statusShowsActivity)

        model.handleFrame(
            inbound(
                #"{"type":"chat_status","status":"info","message":"Saved for later"}"#))
        XCTAssertEqual(model.statusText, "Saved for later")
        XCTAssertFalse(model.statusShowsActivity)

        model.handleFrame(
            inbound(
                #"{"type":"chat_status","status":"done","message":"Completed"}"#))
        XCTAssertNil(model.statusText)
        XCTAssertFalse(model.statusShowsActivity)
    }

    func testNewConversationSendBeforeActiveChatKeepsSurfaceFenceUntilTerminal() {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let sent = capturedFrame(model) { model.newConversation() }
        let submission = sent["submission_id"]!.stringValue!
        let surfaceRequest = sent["request_generation"]!.stringValue!

        XCTAssertEqual(sent["payload"]?["submission_id"]?.stringValue, submission)
        XCTAssertEqual(
            sent["payload"]?["request_generation"]?.stringValue,
            surfaceRequest)
        XCTAssertEqual(model.statusText, "Submitting…")
        XCTAssertTrue(model.statusShowsActivity)
        XCTAssertTrue(model.pendingSurfaceRequestGenerations.contains(surfaceRequest))

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"new_chat","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)",
                 "request_generation":"\(surfaceRequest)","sequence":0,
                 "state":"accepted","phase":"accepted","label":"Accepted",
                 "terminal":false,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:00Z"}
                """))
        XCTAssertEqual(model.operationStatuses[operation]?.state, "accepted")
        XCTAssertNotNil(model.localOperationSubmissions[submission])
        XCTAssertTrue(model.statusShowsActivity)

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"new_chat","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)",
                 "request_generation":"\(surfaceRequest)","sequence":1,
                 "state":"completed","phase":"completed","label":"Ready",
                 "terminal":true,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:01Z"}
                """))
        XCTAssertNil(model.localOperationSubmissions[submission])
        XCTAssertNil(model.statusText)
        XCTAssertNil(model.rootStatusText)
        XCTAssertFalse(model.statusShowsActivity)
    }

    func testTerminalChatSuccessBeforeSnapshotPreservesTransientAnswer() {
        let model = preparedModel()
        model.transientEntries = [
            .status(id: "preview-answer", text: "Visible result")
        ]

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"chat_message","surface":"chat","chat_id":"\(chat)",
                 "connection_generation":"\(connection)","request_generation":"\(request)",
                 "sequence":0,"state":"completed","phase":"completed","label":"Completed",
                 "terminal":true,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:00Z"}
                """))

        XCTAssertEqual(
            model.transientEntries,
            [.status(id: "preview-answer", text: "Visible result")])
        XCTAssertNil(model.statusText)
        XCTAssertFalse(model.statusShowsActivity)
    }

    func testDisconnectClearsPendingSurfaceGeneration() async {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        _ = capturedFrame(model) { model.newConversation() }
        XCTAssertFalse(model.localOperationSubmissions.isEmpty)

        await model.handle(.disconnected(reason: "test disconnect"))

        XCTAssertTrue(model.localOperationSubmissions.isEmpty)
        XCTAssertTrue(model.pendingSurfaceRequestGenerations.isEmpty)
    }

    func testAdmissionRefusalSettlesOnlyTheMatchingWatchSubmission() {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let first = capturedFrame(model) { model.newConversation() }
        let second = capturedFrame(model) { model.newConversation() }
        let firstSubmission = first["submission_id"]!.stringValue!
        let secondSubmission = second["submission_id"]!.stringValue!

        for invalidId in [
            "null",
            "\"AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA\"",
            "\"\(reconnect)\"",
        ] {
            model.handleFrame(
                inbound(
                    """
                    {"type":"error","submission_id":\(invalidId),"accepted":false,
                     "code":"capacity_exceeded","message":"Must not settle.",
                     "retryable":true,"retry_after_ms":250}
                    """))
            XCTAssertNotNil(model.localOperationSubmissions[firstSubmission])
            XCTAssertNotNil(model.localOperationSubmissions[secondSubmission])
        }

        model.handleFrame(
            inbound(
                """
                {"type":"error","submission_id":"\(firstSubmission)","accepted":false,
                 "code":"capacity_exceeded","message":"First refusal.",
                 "retryable":true,"retry_after_ms":250}
                """))
        XCTAssertNil(model.localOperationSubmissions[firstSubmission])
        XCTAssertNotNil(model.localOperationSubmissions[secondSubmission])
        XCTAssertEqual(model.statusText, "Submitting…")
        XCTAssertTrue(model.statusShowsActivity)

        model.handleFrame(
            inbound(
                """
                {"type":"error","submission_id":"\(secondSubmission)","accepted":false,
                 "code":"capacity_exceeded","message":"Second refusal.",
                 "retryable":true,"retry_after_ms":250}
                """))
        XCTAssertNil(model.localOperationSubmissions[secondSubmission])
        XCTAssertNil(model.statusText)
        XCTAssertEqual(model.errorBanner, "Second refusal.")
        XCTAssertFalse(model.statusShowsActivity)
    }

    func testQueuedSurfaceReconnectRestoresExactProjectionBeforeTerminal() async throws {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        var queued = ""
        model.outboundTap = { queued = $0 }
        model.newConversation()
        let replay = try XCTUnwrap(QueuedOperationReplay(frameText: queued))

        await model.handle(.disconnected(reason: "offline"))
        XCTAssertTrue(model.localOperationSubmissions.isEmpty)
        XCTAssertTrue(model.beginConversationConnection(reconnect))
        XCTAssertTrue(model.replayQueuedOperation(replay))
        XCTAssertEqual(model.statusText, "Submitting…")
        XCTAssertTrue(model.statusShowsActivity)
        XCTAssertEqual(
            model.localOperationSubmissions[replay.identity.submissionId]?.connectionGeneration,
            reconnect)

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"new_chat","surface":"operation","chat_id":null,
                 "connection_generation":"\(reconnect)",
                 "request_generation":"\(replay.identity.requestGeneration)","sequence":0,
                 "state":"accepted","phase":"accepted","label":"Accepted",
                 "terminal":false,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:00Z"}
                """))
        XCTAssertEqual(model.operationStatuses[operation]?.state, "accepted")
        XCTAssertTrue(model.statusShowsActivity)
        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"new_chat","surface":"operation","chat_id":null,
                 "connection_generation":"\(reconnect)",
                 "request_generation":"\(replay.identity.requestGeneration)","sequence":1,
                 "state":"completed","phase":"completed","label":"Ready",
                 "terminal":true,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:01Z"}
                """))
        XCTAssertNil(model.localOperationSubmissions[replay.identity.submissionId])
        XCTAssertNil(model.statusText)
        XCTAssertNil(model.rootStatusText)
        XCTAssertFalse(model.statusShowsActivity)
    }

    func testTerminalSuccessKeepsAnotherAcceptedOperationVisible() {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let first = capturedFrame(model) { model.newConversation() }
        let second = capturedFrame(model) { model.newConversation() }
        let firstRequest = first["request_generation"]!.stringValue!
        let secondRequest = second["request_generation"]!.stringValue!
        let secondOperation = "77777777-7777-4777-8777-777777777777"

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"new_chat","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(firstRequest)",
                 "sequence":0,"state":"accepted","phase":"accepted","label":"Starting first…",
                 "terminal":false,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:00Z"}
                """))
        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(secondOperation)",
                 "action":"new_chat","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(secondRequest)",
                 "sequence":0,"state":"running","phase":"running","label":"Starting second…",
                 "terminal":false,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:01Z"}
                """))
        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"new_chat","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(firstRequest)",
                 "sequence":1,"state":"completed","phase":"completed","label":"Ready",
                 "terminal":true,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:02Z"}
                """))

        XCTAssertEqual(model.statusText, "Starting second…")
        XCTAssertEqual(model.rootStatusText, "Starting second…")
        XCTAssertTrue(model.statusShowsActivity)
    }

    func testTerminalSuccessKeepsAnotherLocalSubmissionVisible() {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let first = capturedFrame(model) { model.newConversation() }
        let second = capturedFrame(model) { model.newConversation() }
        let firstRequest = first["request_generation"]!.stringValue!
        let secondSubmission = second["submission_id"]!.stringValue!

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"new_chat","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(firstRequest)",
                 "sequence":0,"state":"completed","phase":"completed","label":"Ready",
                 "terminal":true,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:00Z"}
                """))

        XCTAssertNotNil(model.localOperationSubmissions[secondSubmission])
        XCTAssertEqual(model.statusText, "Submitting…")
        XCTAssertEqual(model.rootStatusText, "Submitting…")
        XCTAssertTrue(model.statusShowsActivity)
    }

    func testTerminalFailureUsesBannerWithoutSpinnerStatus() {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let sent = capturedFrame(model) { model.newConversation() }
        let requestGeneration = sent["request_generation"]!.stringValue!

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"new_chat","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(requestGeneration)",
                 "sequence":0,"state":"failed","phase":"failed","label":"Failed",
                 "terminal":true,"retryable":false,
                 "error":{"code":"network_unavailable","message":"Could not start conversation."},
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:00Z"}
                """))

        XCTAssertNil(model.statusText)
        XCTAssertNil(model.rootStatusText)
        XCTAssertFalse(model.statusShowsActivity)
        XCTAssertEqual(model.errorBanner, "Could not start conversation.")
    }
}
