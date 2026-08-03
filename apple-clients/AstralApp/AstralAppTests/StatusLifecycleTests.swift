import AstralCore
import XCTest

@testable import AstralDeep

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

    private func capturedFrame(_ model: AppModel, action: () -> Void) -> JSONValue {
        var captured: JSONValue?
        model.outboundTap = { text in
            captured = try? JSONValue.parse(Data(text.utf8))
        }
        action()
        return captured!
    }

    private func preparedModel() -> AppModel {
        let model = AppModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        XCTAssertTrue(
            model.openConversationRequest(
                chatId: chat,
                requestGeneration: request,
                purpose: .commit))
        return model
    }

    func testOperationStatusRendersMonotonicallyWithoutReload() {
        let model = preparedModel()
        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"curated_example","surface":"chat","chat_id":"\(chat)",
                 "connection_generation":"\(connection)","request_generation":"\(request)",
                 "sequence":0,"state":"accepted","phase":"accepted","label":"Accepted",
                 "terminal":false,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:00Z"}
                """))
        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"curated_example","surface":"chat","chat_id":"\(chat)",
                 "connection_generation":"\(connection)","request_generation":"\(request)",
                 "sequence":1,"state":"running","phase":"running","label":"Working…",
                 "terminal":false,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:01Z"}
                """))

        XCTAssertEqual(model.operationStatuses[operation]?.state, "running")
        XCTAssertEqual(model.statusText, "Working…")
        XCTAssertTrue(model.statusShowsActivity)
        XCTAssertEqual(model.screen, .chat)
    }

    func testLifecycleUpdatesTheVisibleAgentProjection() {
        let model = preparedModel()
        model.handleFrame(
            inbound(
                """
                {"type":"agent_lifecycle","agent_id":"ua-dice","revision_id":null,
                 "runtime_instance_id":null,"lifecycle_generation":9,"state_revision":4,
                 "state":"offline","reason_code":"host_lost","label":"Agent offline",
                 "updated_at":"2026-07-16T12:00:00Z"}
                """))

        XCTAssertEqual(model.agentLifecycles["ua-dice"]?.state, "offline")
        XCTAssertEqual(model.errorBanner, "ua-dice: Agent offline")
    }

    func testGenericErrorPrefersSafeProviderErrorClassOverOuterEnvelopeCode() {
        let model = AppModel()
        model.handleFrame(
            inbound(
                """
                {"type":"error","code":"llm_config_invalid",
                 "error_class":"provider_unavailable",
                 "message":"The provider is temporarily unavailable."}
                """))

        XCTAssertEqual(
            model.errorBanner,
            "The provider is temporarily unavailable. (provider_unavailable)")
    }

    func testSurfaceSendBeforeActiveChatCorrelatesThroughTerminalStatus() {
        let model = AppModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let sent = capturedFrame(model) {
            model.sendEvent(
                "chrome_open",
                .object(["surface": .string("llm_settings")]))
        }
        let submission = sent["submission_id"]!.stringValue!
        let surfaceRequest = sent["request_generation"]!.stringValue!

        XCTAssertEqual(sent["payload"]?["submission_id"]?.stringValue, submission)
        XCTAssertEqual(
            sent["payload"]?["request_generation"]?.stringValue,
            surfaceRequest)
        XCTAssertEqual(model.statusText, "Submitting…")
        XCTAssertTrue(model.statusShowsActivity)
        XCTAssertEqual(model.localOperationSubmissions[submission]?.chatId, nil)
        XCTAssertTrue(model.pendingSurfaceRequestGenerations.contains(surfaceRequest))

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"chrome_open","surface":"llm_settings","chat_id":null,
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
                 "action":"chrome_open","surface":"llm_settings","chat_id":null,
                 "connection_generation":"\(connection)",
                 "request_generation":"\(surfaceRequest)","sequence":1,
                 "state":"completed","phase":"completed","label":"Opened",
                 "terminal":true,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:01Z"}
                """))
        XCTAssertEqual(model.operationStatuses[operation]?.state, "completed")
        XCTAssertNil(model.localOperationSubmissions[submission])
        XCTAssertFalse(model.pendingSurfaceRequestGenerations.contains(surfaceRequest))
        XCTAssertNil(model.statusText)
        XCTAssertFalse(model.statusShowsActivity)
    }

    func testAdmissionRefusalClearsOnlyItsCorrelatedLocalSubmission() {
        let model = AppModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let first = capturedFrame(model) {
            model.sendEvent("discover_agents")
        }
        let second = capturedFrame(model) {
            model.sendEvent("get_history")
        }
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
                 "code":"capacity_exceeded","message":"Try again shortly.",
                 "retryable":true,"retry_after_ms":250}
                """))

        XCTAssertNil(model.localOperationSubmissions[firstSubmission])
        XCTAssertNotNil(model.localOperationSubmissions[secondSubmission])
        XCTAssertEqual(model.statusText, "Submitting…")
        XCTAssertEqual(model.errorBanner, "Try again shortly.")
        XCTAssertTrue(model.bannerIsError)
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
    }

    func testDisconnectClearsPendingSurfaceGeneration() async {
        let model = AppModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let sent = capturedFrame(model) {
            model.sendEvent("discover_agents")
        }
        let surfaceRequest = sent["request_generation"]!.stringValue!

        XCTAssertEqual(model.statusText, "Submitting…")
        XCTAssertTrue(model.pendingSurfaceRequestGenerations.contains(surfaceRequest))

        await model.handle(.disconnected(reason: "test disconnect"))

        XCTAssertTrue(model.localOperationSubmissions.isEmpty)
        XCTAssertFalse(model.pendingSurfaceRequestGenerations.contains(surfaceRequest))
        XCTAssertNil(model.statusText)
    }

    func testQueuedSurfaceDisconnectReconnectRestoresProjectionBeforeTerminal() async throws {
        let model = AppModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        var queued = ""
        model.outboundTap = { queued = $0 }
        model.sendEvent(
            "chrome_open",
            .object(["surface": .string("llm_settings")]))
        let replay = try XCTUnwrap(QueuedOperationReplay(frameText: queued))

        await model.handle(.disconnected(reason: "offline"))
        XCTAssertTrue(model.localOperationSubmissions.isEmpty)
        XCTAssertTrue(model.beginConversationConnection(reconnect))
        XCTAssertTrue(model.replayQueuedOperation(replay))
        XCTAssertEqual(model.statusText, "Submitting…")
        XCTAssertEqual(
            model.localOperationSubmissions[replay.identity.submissionId]?.connectionGeneration,
            reconnect)

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"chrome_open","surface":"llm_settings","chat_id":null,
                 "connection_generation":"\(reconnect)",
                 "request_generation":"\(replay.identity.requestGeneration)","sequence":0,
                 "state":"accepted","phase":"accepted","label":"Accepted",
                 "terminal":false,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:00Z"}
                """))
        XCTAssertEqual(model.operationStatuses[operation]?.state, "accepted")
        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"chrome_open","surface":"llm_settings","chat_id":null,
                 "connection_generation":"\(reconnect)",
                 "request_generation":"\(replay.identity.requestGeneration)","sequence":1,
                 "state":"completed","phase":"completed","label":"Opened",
                 "terminal":true,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:01Z"}
                """))
        XCTAssertNil(model.localOperationSubmissions[replay.identity.submissionId])
        XCTAssertNil(model.statusText)
        XCTAssertFalse(model.statusShowsActivity)
    }

    func testQueuedChatReconnectAcceptsCommitSnapshotAndLateTerminal() async throws {
        let model = preparedModel()
        var queued = ""
        model.outboundTap = { queued = $0 }
        model.sendChat("queued turn")
        let replay = try XCTUnwrap(QueuedOperationReplay(frameText: queued))

        await model.handle(.disconnected(reason: "offline"))
        XCTAssertTrue(model.localOperationSubmissions.isEmpty)
        XCTAssertTrue(model.beginConversationConnection(reconnect))
        XCTAssertTrue(model.replayQueuedOperation(replay))
        XCTAssertTrue(
            model.pendingChatRequestGenerations.contains(
                replay.identity.requestGeneration))

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"chat_message","surface":"chat","chat_id":"\(chat)",
                 "connection_generation":"\(reconnect)",
                 "request_generation":"\(replay.identity.requestGeneration)","sequence":0,
                 "state":"accepted","phase":"accepted","label":"Accepted",
                 "terminal":false,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:00Z"}
                """))
        XCTAssertEqual(model.operationStatuses[operation]?.state, "accepted")
        model.handleFrame(
            inbound(
                """
                {"type":"conversation_commit_ready","schema_version":1,
                 "chat_id":"\(chat)","connection_generation":"\(reconnect)",
                 "request_generation":"\(replay.identity.requestGeneration)",
                 "render_revision":1}
                """))
        model.handleFrame(
            inbound(
                """
                {"type":"conversation_snapshot","schema_version":1,
                 "snapshot_id":"55555555-5555-4555-8555-555555555555",
                 "chat_id":"\(chat)","connection_generation":"\(reconnect)",
                 "request_generation":"\(replay.identity.requestGeneration)",
                 "snapshot_purpose":"commit","render_revision":1,
                 "committed_at":"2026-07-16T12:00:01Z","transcript":[],
                 "canvas":{"target":"canvas","components":[]}}
                """))
        XCTAssertEqual(model.lastCommittedRenderRevision, 1)
        XCTAssertNotNil(model.localOperationSubmissions[replay.identity.submissionId])

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"chat_message","surface":"chat","chat_id":"\(chat)",
                 "connection_generation":"\(reconnect)",
                 "request_generation":"\(replay.identity.requestGeneration)","sequence":1,
                 "state":"completed","phase":"completed","label":"Completed",
                 "terminal":true,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:02Z"}
                """))
        XCTAssertNil(model.localOperationSubmissions[replay.identity.submissionId])
        XCTAssertNil(model.statusText)
        XCTAssertFalse(model.statusShowsActivity)
    }

    func testTerminalSuccessDoesNotClearAnotherAcceptedOperation() {
        let model = AppModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let first = capturedFrame(model) { model.sendEvent("discover_agents") }
        let second = capturedFrame(model) { model.sendEvent("get_history") }
        let firstRequest = first["request_generation"]!.stringValue!
        let secondRequest = second["request_generation"]!.stringValue!
        let secondOperation = "77777777-7777-4777-8777-777777777777"

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"discover_agents","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(firstRequest)",
                 "sequence":0,"state":"accepted","phase":"accepted","label":"Loading agents…",
                 "terminal":false,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:00Z"}
                """))
        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(secondOperation)",
                 "action":"get_history","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(secondRequest)",
                 "sequence":0,"state":"running","phase":"running","label":"Loading history…",
                 "terminal":false,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:01Z"}
                """))
        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"discover_agents","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(firstRequest)",
                 "sequence":1,"state":"completed","phase":"completed","label":"Completed",
                 "terminal":true,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:02Z"}
                """))

        XCTAssertEqual(model.statusText, "Loading history…")
        XCTAssertTrue(model.statusShowsActivity)
        XCTAssertTrue(model.pendingSurfaceRequestGenerations.contains(secondRequest))
    }

    func testTerminalSuccessPreservesAnotherLocallySubmittingOperation() {
        let model = AppModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let first = capturedFrame(model) { model.sendEvent("discover_agents") }
        let second = capturedFrame(model) { model.sendEvent("get_history") }
        let firstRequest = first["request_generation"]!.stringValue!
        let secondSubmission = second["submission_id"]!.stringValue!

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"discover_agents","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(firstRequest)",
                 "sequence":0,"state":"completed","phase":"completed","label":"Completed",
                 "terminal":true,"retryable":false,"error":null,"retry_after_ms":null,
                 "updated_at":"2026-07-16T12:00:00Z"}
                """))

        XCTAssertNotNil(model.localOperationSubmissions[secondSubmission])
        XCTAssertEqual(model.statusText, "Submitting…")
        XCTAssertTrue(model.statusShowsActivity)
    }

    func testTerminalFailureUsesBannerWithoutActivityStatus() {
        let model = AppModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let sent = capturedFrame(model) { model.sendEvent("discover_agents") }
        let requestGeneration = sent["request_generation"]!.stringValue!

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"discover_agents","surface":"operation","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(requestGeneration)",
                 "sequence":0,"state":"failed","phase":"failed","label":"Failed",
                 "terminal":true,"retryable":false,
                 "error":{"code":"network_unavailable","message":"Could not load agents."},
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:00Z"}
                """))

        XCTAssertNil(model.statusText)
        XCTAssertFalse(model.statusShowsActivity)
        XCTAssertEqual(model.errorBanner, "Could not load agents.")
        XCTAssertTrue(model.bannerIsError)
    }

    func testTerminalChatFailureRemovesSkeletonAndProgressPresentation() {
        let model = AppModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        let sent = capturedFrame(model) { model.sendChat("hello") }
        let requestGeneration = sent["request_generation"]!.stringValue!
        XCTAssertTrue(model.showSkeleton)
        XCTAssertTrue(model.statusShowsActivity)

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"chat_message","surface":"chat","chat_id":null,
                 "connection_generation":"\(connection)","request_generation":"\(requestGeneration)",
                 "sequence":0,"state":"failed","phase":"failed","label":"Failed",
                 "terminal":true,"retryable":false,
                 "error":{"code":"operation_failed","message":"The request failed."},
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:00Z"}
                """))

        XCTAssertNil(model.statusText)
        XCTAssertFalse(model.statusShowsActivity)
        XCTAssertFalse(model.showSkeleton)
        XCTAssertEqual(model.errorBanner, "The request failed.")
    }

    func testTerminalChatSuccessBeforeSnapshotPreservesTransientTranscript() {
        let model = preparedModel()
        let sent = capturedFrame(model) { model.sendChat("hello") }
        let requestGeneration = sent["request_generation"]!.stringValue!
        model.transientTurns.append(
            .init(id: "preview-1", role: "assistant", text: "Visible result"))

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"chat_message","surface":"chat","chat_id":"\(chat)",
                 "connection_generation":"\(connection)","request_generation":"\(requestGeneration)",
                 "sequence":0,"state":"completed","phase":"completed","label":"Completed",
                 "terminal":true,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:00Z"}
                """))

        XCTAssertEqual(model.visibleTurns.map(\.text), ["hello", "Visible result"])
        XCTAssertNil(model.statusText)
        XCTAssertFalse(model.statusShowsActivity)
        XCTAssertFalse(model.turnActive)
        XCTAssertFalse(model.showSkeleton)
    }

    func testChatTerminalAfterSnapshotUsesRetainedSubmissionFence() {
        let model = preparedModel()
        let sent = capturedFrame(model) { model.sendChat("hello") }
        let submission = sent["submission_id"]!.stringValue!
        let submittedRequest = sent["request_generation"]!.stringValue!

        model.handleFrame(
            inbound(
                """
                {"type":"conversation_snapshot","schema_version":1,
                 "snapshot_id":"55555555-5555-4555-8555-555555555555",
                 "chat_id":"\(chat)","connection_generation":"\(connection)",
                 "request_generation":"\(submittedRequest)","snapshot_purpose":"commit",
                 "render_revision":1,"committed_at":"2026-07-16T12:00:00Z",
                 "transcript":[],"canvas":{"target":"canvas","components":[]}}
                """))
        let next = "66666666-6666-4666-8666-666666666666"
        XCTAssertTrue(
            model.openConversationRequest(
                chatId: chat,
                requestGeneration: next,
                purpose: .hydration))

        model.handleFrame(
            inbound(
                """
                {"type":"operation_status","operation_id":"\(operation)",
                 "action":"chat_message","surface":"chat","chat_id":"\(chat)",
                 "connection_generation":"\(connection)",
                 "request_generation":"\(submittedRequest)","sequence":1,
                 "state":"completed","phase":"completed","label":"Completed",
                 "terminal":true,"retryable":false,"error":null,
                 "retry_after_ms":null,"updated_at":"2026-07-16T12:00:01Z"}
                """))

        XCTAssertEqual(model.operationStatuses[operation]?.state, "completed")
        XCTAssertNil(model.localOperationSubmissions[submission])
    }
}
