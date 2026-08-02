import AVFoundation
import AstralCore
import Foundation
import XCTest

@testable import AstralWatch

@MainActor
final class VoiceContract065Tests: XCTestCase {
    private let device = "00000000-0000-4000-8000-000000000001"
    private let connection = "00000000-0000-4000-8000-000000000002"
    private let otherConnection = "00000000-0000-4000-8000-000000000012"
    private let session = "00000000-0000-4000-8000-000000000003"
    private let chat = "00000000-0000-4000-8000-000000000004"
    private let otherChat = "00000000-0000-4000-8000-00000000000d"
    private let turn = "00000000-0000-4000-8000-000000000005"
    private let clientTurn = "00000000-0000-4000-8000-000000000006"
    private let submission = "00000000-0000-4000-8000-000000000007"
    private let request = "00000000-0000-4000-8000-000000000008"
    private let announcement = "00000000-0000-4000-8000-000000000009"
    private let worker = "voice-worker-01"
    private let result = "result-01"

    func testWatchRegistrationAdvertisesStableBoundPCMCapability() throws {
        let model = WatchModel()
        model.voiceBridge = MockWatchVoiceBridge(permission: .authorized)
        model.voiceTokenProvider = { "keycloak-token" }

        let first = try JSONValue.parse(
            Data(model.registrationFrame(token: "token", resumed: false).utf8))
        let second = try JSONValue.parse(
            Data(model.registrationFrame(token: "token", resumed: true).utf8))

        XCTAssertEqual(first["device_id"]?.stringValue, model.voiceDeviceId)
        XCTAssertEqual(second["device_id"]?.stringValue, model.voiceDeviceId)
        XCTAssertNotEqual(
            first["connection_generation"]?.stringValue,
            second["connection_generation"]?.stringValue)
        XCTAssertTrue(first["capabilities"]?.arrayValue?.contains(.string("voice")) == true)
        XCTAssertEqual(first["device"]?["voice_transport"]?.stringValue, "watch_pcm_websocket")
        XCTAssertEqual(first["device"]?["microphone_permission"]?.stringValue, "authorized")
        XCTAssertEqual(first["device"]?["full_duplex"]?.boolValue, false)
    }

    func testComposerIsServerOwnedAndExtraFieldsFailClosed() throws {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        model.handleFrame(InboundFrame.parse(try composerJSON(revision: 7))!)

        XCTAssertEqual(model.voiceComposer?.revision, 7)
        XCTAssertEqual(model.primaryVoiceControl?.action, "voice_session_start")
        XCTAssertEqual(model.voiceStatusLabel, "Voice conversation off")

        var invalid = try JSONValue.parse(Data(try composerJSON(revision: 8).utf8)).objectValue!
        invalid["unexpected"] = .bool(true)
        model.handleFrame(
            InboundFrame(name: "composer_state", payload: .object(invalid)))
        XCTAssertEqual(model.voiceComposer?.revision, 7)

        let backgroundReconnect = InboundFrame.parse(
            try composerJSON(revision: 9, state: "reconnecting"))!
        XCTAssertNotNil(
            WatchVoiceComposer(frame: backgroundReconnect, expectedConnection: connection))
        let foregroundError = InboundFrame.parse(
            try composerJSON(revision: 10, state: "error", foreground: true))!
        XCTAssertNotNil(
            WatchVoiceComposer(frame: foregroundError, expectedConnection: connection))
    }

    func testComposerRevisionIsScopedToConnectionGenerationAndStrictlyMonotonic() throws {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        model.handleFrame(InboundFrame.parse(try composerJSON(revision: 7))!)
        XCTAssertEqual(model.primaryVoiceControl?.action, "voice_session_start")

        XCTAssertTrue(model.beginConversationConnection(otherConnection))
        model.handleFrame(
            InboundFrame.parse(
                try composerJSON(
                    revision: 0,
                    connection: otherConnection,
                    state: "listening",
                    foreground: true,
                    actions: ["voice_session_end"]))!)
        XCTAssertEqual(model.voiceComposer?.revision, 0)
        XCTAssertEqual(model.primaryVoiceControl?.action, "voice_session_end")

        model.handleFrame(
            InboundFrame.parse(
                try composerJSON(
                    revision: 0,
                    connection: otherConnection,
                    actions: ["voice_session_start"]))!)
        XCTAssertEqual(model.primaryVoiceControl?.action, "voice_session_end")
    }

    func testTerminalRequestNoticeSurvivesSessionChurnAndClearsForNewWorkOrReset() throws {
        let model = try configuredVoiceModel()
        model.voiceSession = try XCTUnwrap(
            WatchVoiceSession(
                json: voiceSessionJSON(
                    deviceId: model.voiceDeviceId, visibleChatId: chat)))

        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceTurnJSON(
                        state: "failed",
                        message: "The safe server explanation remains visible.",
                        occurredAt: "2099-07-31T12:05:00Z"))))
        let failedNotice = try XCTUnwrap(model.voiceTerminalNotice)
        XCTAssertEqual(failedNotice.title, "Request did not complete.")
        XCTAssertEqual(
            failedNotice.serverMessage, "The safe server explanation remains visible.")

        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceSessionStateJSON(
                        state: "listening", reason: "ready"))))
        XCTAssertEqual(
            model.voiceTerminalNotice, failedNotice,
            "ordinary session churn cannot hide the terminal result")

        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceTurnJSON(state: "processing", sequence: 2))))
        XCTAssertEqual(
            model.voiceTerminalNotice, failedNotice,
            "same-turn lifecycle churn cannot erase its terminal notice")

        let nextTurn = "00000000-0000-4000-8000-000000000015"
        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceTurnJSON(
                        state: "accepted", turnId: nextTurn, sequence: 1,
                        occurredAt: "2099-07-31T12:04:59Z"))))
        XCTAssertEqual(
            model.voiceTerminalNotice, failedNotice,
            "an older different-turn frame cannot erase a newer notice")

        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceTurnJSON(
                        state: "accepted", turnId: nextTurn, sequence: 2,
                        occurredAt: "2099-07-31T12:05:01Z"))))
        XCTAssertNil(model.voiceTerminalNotice)

        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceTurnJSON(
                        state: "abandoned", turnId: nextTurn, sequence: 3,
                        message: "The request ended before dispatch.",
                        occurredAt: "2099-07-31T12:05:02Z"))))
        XCTAssertEqual(model.voiceTerminalNotice?.title, "Request did not complete.")
        XCTAssertEqual(
            model.voiceTerminalNotice?.serverMessage,
            "The request ended before dispatch.")

        model.handleFrame(InboundFrame.parse(try composerJSON(revision: 25))!)
        XCTAssertNil(model.voiceTerminalNotice, "an explicit server reset clears the notice")
        model.pendingDictation = "Typed chat remains available."
        XCTAssertEqual(model.pendingDictation, "Typed chat remains available.")
    }

    func testServerSpeechErrorAfterTextSuccessDoesNotCallTheRequestFailed() throws {
        let model = try configuredVoiceModel()
        model.voiceSession = try XCTUnwrap(
            WatchVoiceSession(
                json: voiceSessionJSON(
                    deviceId: model.voiceDeviceId, visibleChatId: chat)))

        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceTurnJSON(
                        state: "succeeded", sequence: 1,
                        occurredAt: "2099-07-31T12:05:00Z"))))
        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceSessionStateJSON(
                        state: "error", reason: "speech_error",
                        message: "The result audio could not be delivered."))))

        let notice = try XCTUnwrap(model.voiceTerminalNotice)
        XCTAssertEqual(notice.kind, .speechFailure)
        XCTAssertEqual(notice.turnId, turn)
        XCTAssertEqual(notice.occurredAt, "2099-07-31T12:05:00Z")
        XCTAssertEqual(notice.title, "Speech playback failed.")
        XCTAssertEqual(notice.serverMessage, "The result audio could not be delivered.")
        XCTAssertTrue(notice.displayText.contains("text result may still be available"))
        XCTAssertFalse(notice.displayText.localizedCaseInsensitiveContains("request failed"))
        XCTAssertFalse(notice.displayText.contains("Request did not complete"))
    }

    func testExactTurnSpeechFailureKeepsCommittedTextNoticeAndRejectsOlderFailure() throws {
        let model = try configuredVoiceModel()
        model.voiceSession = try XCTUnwrap(
            WatchVoiceSession(
                json: voiceSessionJSON(
                    deviceId: model.voiceDeviceId, visibleChatId: chat)))

        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceTurnJSON(
                        state: "succeeded", sequence: 1,
                        message: "The result audio could not be delivered.",
                        occurredAt: "2099-07-31T12:05:00Z", speechOutcome: "failed"))))

        let failedSpeech = try XCTUnwrap(model.voiceTerminalNotice)
        XCTAssertEqual(failedSpeech.kind, .speechFailure)
        XCTAssertEqual(failedSpeech.turnId, turn)
        XCTAssertEqual(failedSpeech.occurredAt, "2099-07-31T12:05:00Z")
        XCTAssertTrue(failedSpeech.displayText.contains("text result is still available"))
        XCTAssertFalse(failedSpeech.displayText.localizedCaseInsensitiveContains("request failed"))
        XCTAssertEqual(model.voiceState, .error)
        XCTAssertEqual(model.voiceReason, "speech_error")

        let newerTurn = "00000000-0000-4000-8000-000000000015"
        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceTurnJSON(
                        state: "succeeded", turnId: newerTurn, sequence: 1,
                        message: "Newer result audio was unavailable.",
                        occurredAt: "2099-07-31T12:05:01Z", speechOutcome: "failed"))))
        let newerNotice = try XCTUnwrap(model.voiceTerminalNotice)
        XCTAssertEqual(newerNotice.turnId, newerTurn)

        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try voiceTurnJSON(
                        state: "succeeded", sequence: 2,
                        message: "This older audio failure must stay hidden.",
                        occurredAt: "2099-07-31T12:05:00Z", speechOutcome: "failed"))))
        XCTAssertEqual(model.voiceTerminalNotice, newerNotice)
        XCTAssertEqual(model.voiceMessage, newerNotice.displayText)
    }

    func testCorrelatedSubmissionRejectionStopsRetryAndShowsExplicitRetryNotice() async throws {
        let model = try configuredVoiceModel()
        model.connected = true
        model.voiceSession = try XCTUnwrap(
            WatchVoiceSession(
                json: voiceSessionJSON(
                    deviceId: model.voiceDeviceId, visibleChatId: chat)))
        model.voiceGrant = try XCTUnwrap(WatchVoiceBridgeGrant(json: bridgeGrantJSON()))
        var sent: [String] = []
        model.currentConnectionVoiceSendOverride = { sent.append($0) }
        let transcript = try XCTUnwrap(WatchVoiceTranscript(json: finalTranscriptJSON()))

        model.consumeVoiceTranscript(transcript)
        XCTAssertEqual(sent.count, 1)
        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try submissionRejectionJSON(
                        message: "This stale socket must be ignored.",
                        retryPolicy: "explicit_user_retry"
                    ).replacingOccurrences(
                        of: "\"connection_generation\":\"\(connection)\"",
                        with: "\"connection_generation\":\"\(otherConnection)\""))))
        XCTAssertNil(model.voiceTerminalNotice)
        model.handleFrame(
            try XCTUnwrap(
                InboundFrame.parse(
                    try submissionRejectionJSON(
                        message: "Capacity is temporarily full.",
                        retryPolicy: "explicit_user_retry"))))

        XCTAssertEqual(model.voiceTerminalNotice?.title, "Request did not start.")
        XCTAssertEqual(
            model.voiceTerminalNotice?.serverMessage, "Capacity is temporarily full.")
        XCTAssertEqual(
            model.voiceTerminalNotice?.guidance,
            "Please say it again when you are ready.")
        try await Task.sleep(for: .milliseconds(2700))
        XCTAssertEqual(
            sent.count, 1,
            "a terminal rejection cancels exact-wire retry and never auto-replays")
    }

    func testActivationDeniedByMicrophonePermissionNeverCallsVoiceREST() async throws {
        let model = try configuredVoiceModel(permission: .denied)
        model.connected = true
        model.activeChatId = chat
        let recorder = RequestRecorder()
        model.voiceRESTTransport = { request in
            await recorder.record(request)
            return (500, Data())
        }
        model.handleFrame(
            InboundFrame.parse(
                try composerJSON(
                    revision: 14,
                    actions: ["voice_session_start"]))!)

        model.performVoiceAction("voice_session_start")
        try await Task.sleep(for: .milliseconds(50))

        XCTAssertEqual(model.voiceState, .unavailable)
        XCTAssertEqual(model.voiceReason, "permission_denied")
        let requests = await recorder.values()
        XCTAssertTrue(requests.isEmpty)
    }

    func testVoiceActivationNewChatUsesCurrentConnectionTransportWithoutQueueAlert() async throws {
        let model = try configuredVoiceModel()
        model.connected = true
        var liveOnly: [String] = []
        model.currentConnectionVoiceSendOverride = { liveOnly.append($0) }
        model.handleFrame(
            InboundFrame.parse(
                try composerJSON(
                    revision: 15,
                    actions: ["voice_session_start"]))!)

        model.performVoiceAction("voice_session_start")
        try await Task.sleep(for: .milliseconds(50))

        let wire = try XCTUnwrap(liveOnly.first)
        let frame = try XCTUnwrap(InboundFrame.parse(wire))
        XCTAssertNotNil(CorrelatedVoiceNewChat(frame: frame))
        XCTAssertEqual(liveOnly.count, 1)
        XCTAssertNil(model.errorBanner)
    }

    func testTakeoverActionCarriesServerGenerationAndGrantRevision() async throws {
        let model = try configuredVoiceModel()
        model.connected = true
        model.activeChatId = chat
        let recorder = RequestRecorder()
        model.voiceRESTTransport = { request in
            await recorder.record(request)
            return (409, Data("{\"code\":\"stale_generation\"}".utf8))
        }
        model.handleFrame(
            InboundFrame.parse(
                try composerJSON(
                    revision: 15,
                    actions: ["voice_session_takeover"],
                    sessionId: session,
                    generation: 7,
                    mediaGrantRevision: 9))!)

        model.performVoiceAction("voice_session_takeover")
        try await Task.sleep(for: .milliseconds(75))

        let recorded = await recorder.value()
        let request = try XCTUnwrap(recorded)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertTrue(request.url?.path.hasSuffix("/api/voice/sessions/\(session)/takeover") == true)
        let body = try JSONValue.parse(try XCTUnwrap(request.httpBody))
        XCTAssertEqual(body["expected_generation"]?.numberValue, 7)
        XCTAssertEqual(body["expected_media_grant_revision"]?.numberValue, 9)
    }

    func testADVCCodecUsesExactBigEndianHeaderLengthsAndSequenceGate() throws {
        let payload = Data(repeating: 0x7f, count: WatchVoicePCMFrame.capturePayloadLength)
        let frame = try XCTUnwrap(
            WatchVoicePCMFrame(
                kind: .microphone,
                sequence: 0x0102_0304_0506_0708,
                timestampMicroseconds: 0x1112_1314_1516_1718,
                payload: payload))
        let encoded = frame.encoded

        XCTAssertEqual(encoded.count, 26 + 640)
        XCTAssertEqual(Array(encoded.prefix(8)), [0x41, 0x44, 0x56, 0x43, 1, 1, 0, 0])
        XCTAssertEqual(Array(encoded[8..<16]), [1, 2, 3, 4, 5, 6, 7, 8])
        XCTAssertEqual(Array(encoded[24..<26]), [0x02, 0x80])
        XCTAssertEqual(WatchVoicePCMFrame(data: encoded), frame)

        var flagged = encoded
        flagged[7] = 1
        XCTAssertNil(WatchVoicePCMFrame(data: flagged))

        var gate = WatchVoicePCMSequenceGate()
        XCTAssertTrue(gate.accept(frame))
        let next = try XCTUnwrap(
            WatchVoicePCMFrame(
                kind: .microphone,
                sequence: frame.sequence + 1,
                timestampMicroseconds: frame.timestampMicroseconds + 20_000,
                payload: payload))
        XCTAssertTrue(gate.accept(next))
        let gap = try XCTUnwrap(
            WatchVoicePCMFrame(
                kind: .microphone,
                sequence: next.sequence + 2,
                timestampMicroseconds: next.timestampMicroseconds + 40_000,
                payload: payload))
        XCTAssertFalse(gate.accept(gap))
    }

    func testBridgeGrantReadyAndAnnouncementRequireExactProfileWorkerAndRange() throws {
        let grant = try XCTUnwrap(WatchVoiceBridgeGrant(json: bridgeGrantJSON()))
        XCTAssertEqual(grant.workerIdentity, worker)
        XCTAssertNil(
            WatchVoiceBridgeReady(
                json: readyJSON(worker: "voice-worker-evil"),
                grant: grant))
        XCTAssertNotNil(WatchVoiceBridgeReady(json: readyJSON(worker: worker), grant: grant))

        let valid = try XCTUnwrap(
            WatchVoiceAnnouncement(json: announcementJSON(lastSequence: 149)))
        XCTAssertTrue(valid.matches(grant: grant))
        XCTAssertEqual(valid.durationSamples, 24_000)
        XCTAssertNil(WatchVoiceAnnouncement(json: announcementJSON(lastSequence: 148)))
    }

    func testAnnouncementLedgerRejectsQuantumOrderAndThirtySecondOverrun() throws {
        var ledger = WatchVoiceAnnouncementLedger()
        let opening = try XCTUnwrap(
            WatchVoiceAnnouncement(
                json: resultAnnouncementJSON(
                    announcementSequence: 1,
                    quantumRole: "result_opening",
                    quantumIndex: 0,
                    durationSamples: 24_000,
                    firstMediaSequence: 0,
                    reservedSamples: 24_000)))
        XCTAssertTrue(ledger.accept(opening))

        for index in 1...7 {
            let continuation = try XCTUnwrap(
                WatchVoiceAnnouncement(
                    json: resultAnnouncementJSON(
                        announcementSequence: index + 1,
                        quantumRole: "result_continuation",
                        quantumIndex: index,
                        durationSamples: 96_000,
                        firstMediaSequence: index * 200,
                        reservedSamples: 96_000)))
            XCTAssertTrue(ledger.accept(continuation))
        }
        let overrun = try XCTUnwrap(
            WatchVoiceAnnouncement(
                json: resultAnnouncementJSON(
                    announcementSequence: 9,
                    quantumRole: "result_continuation",
                    quantumIndex: 8,
                    durationSamples: 96_000,
                    firstMediaSequence: 1_600,
                    reservedSamples: 96_000)))
        XCTAssertFalse(ledger.accept(overrun))

        var orderLedger = WatchVoiceAnnouncementLedger()
        let wrongOpeningIndex = try XCTUnwrap(
            WatchVoiceAnnouncement(
                json: resultAnnouncementJSON(
                    announcementSequence: 1,
                    quantumRole: "result_continuation",
                    quantumIndex: 1,
                    durationSamples: 48_000,
                    firstMediaSequence: 0,
                    reservedSamples: 48_000)))
        XCTAssertFalse(orderLedger.accept(wrongOpeningIndex))
    }

    func testAudioSessionInterruptionSignalsImmediatePauseAndRecovery() async {
        let audio = WatchVoiceAudioEngine()
        var events: [WatchVoiceAudioEvent] = []
        audio.setEventHandler { events.append($0) }

        NotificationCenter.default.post(
            name: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(),
            userInfo: [
                AVAudioSessionInterruptionTypeKey:
                    AVAudioSession.InterruptionType.began.rawValue
            ])
        try? await Task.sleep(for: .milliseconds(30))
        NotificationCenter.default.post(
            name: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(),
            userInfo: [
                AVAudioSessionInterruptionTypeKey:
                    AVAudioSession.InterruptionType.ended.rawValue
            ])
        try? await Task.sleep(for: .milliseconds(30))

        XCTAssertEqual(events, [.interrupted("audio_interrupted"), .recovered])
    }

    func testFinalTranscriptUsesOrdinaryChatPathAndAckStopsRetry() async throws {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        model.connected = true
        model.voiceGrant = try XCTUnwrap(WatchVoiceBridgeGrant(json: bridgeGrantJSON()))
        var sent: [JSONValue] = []
        model.currentConnectionVoiceSendOverride = { text in
            if let value = try? JSONValue.parse(Data(text.utf8)) { sent.append(value) }
        }
        let transcript = try XCTUnwrap(WatchVoiceTranscript(json: finalTranscriptJSON()))

        model.consumeVoiceTranscript(transcript)

        let frame = try XCTUnwrap(sent.first)
        XCTAssertEqual(frame["type"]?.stringValue, "ui_event")
        XCTAssertEqual(frame["action"]?.stringValue, "chat_message")
        XCTAssertEqual(frame["session_id"]?.stringValue, chat)
        XCTAssertEqual(frame["submission_id"]?.stringValue, submission)
        XCTAssertEqual(frame["request_generation"]?.stringValue, request)
        XCTAssertEqual(frame["payload"]?["message"]?.stringValue, transcript.text)
        XCTAssertEqual(frame["payload"]?["voice_origin"]?["turn_id"]?.stringValue, turn)
        XCTAssertNil(frame["payload"]?["voice_origin"]?["text"])

        model.handleFrame(
            InboundFrame.parse(
                """
                {"type":"user_message_acked","schema_version":"1","chat_id":"\(chat)",
                 "message_id":41,"submission_id":"\(submission)",
                 "request_generation":"\(request)","connection_generation":"\(connection)",
                 "voice_turn_id":"\(turn)"}
                """)!)
        try await Task.sleep(for: .milliseconds(2700))
        XCTAssertEqual(sent.count, 1)
        XCTAssertNil(model.errorBanner)
    }

    func testPendingFinalReframesForNewUIConnectionWithoutChangingProofIdentity() throws {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        model.connected = true
        model.voiceGrant = try XCTUnwrap(WatchVoiceBridgeGrant(json: bridgeGrantJSON()))
        var sent: [JSONValue] = []
        model.currentConnectionVoiceSendOverride = { text in
            if let value = try? JSONValue.parse(Data(text.utf8)) { sent.append(value) }
        }
        let transcript = try XCTUnwrap(WatchVoiceTranscript(json: finalTranscriptJSON()))

        model.consumeVoiceTranscript(transcript)
        model.connected = false
        XCTAssertTrue(model.beginConversationConnection(otherConnection))
        model.connected = true
        model.consumeVoiceTranscript(transcript)

        XCTAssertEqual(sent.count, 2)
        let first = try XCTUnwrap(sent.first)
        let reframed = try XCTUnwrap(sent.last)
        XCTAssertEqual(first["connection_generation"]?.stringValue, connection)
        XCTAssertEqual(reframed["connection_generation"]?.stringValue, otherConnection)
        XCTAssertEqual(
            reframed["payload"]?["connection_generation"]?.stringValue,
            otherConnection)
        XCTAssertEqual(first["submission_id"], reframed["submission_id"])
        XCTAssertEqual(first["request_generation"], reframed["request_generation"])
        XCTAssertEqual(first["payload"]?["message"], reframed["payload"]?["message"])
        XCTAssertEqual(
            first["payload"]?["voice_origin"],
            reframed["payload"]?["voice_origin"],
            "the transcript proof, digest, turn, and client-turn identities stay exact")

        model.handleFrame(
            InboundFrame.parse(
                """
                {"type":"user_message_acked","schema_version":"1","chat_id":"\(chat)",
                 "message_id":41,"submission_id":"\(submission)",
                 "request_generation":"\(request)","connection_generation":"\(connection)",
                 "voice_turn_id":"\(turn)"}
                """)!)
        model.consumeVoiceTranscript(transcript)
        XCTAssertEqual(sent.count, 3, "an acknowledgement from the replaced socket is ignored")

        let currentAcknowledgementFrame = try XCTUnwrap(
            InboundFrame.parse(
                """
                {"type":"user_message_acked","schema_version":"1","chat_id":"\(chat)",
                 "message_id":42,"submission_id":"\(submission)",
                 "request_generation":"\(request)",
                 "connection_generation":"\(otherConnection)","voice_turn_id":"\(turn)"}
                """))
        let currentAcknowledgement = try XCTUnwrap(
            VoiceMessageAcknowledgement(frame: currentAcknowledgementFrame))
        XCTAssertEqual(currentAcknowledgement.connectionGeneration, otherConnection)
        XCTAssertEqual(currentAcknowledgement.chatId, transcript.chatId)
        XCTAssertEqual(currentAcknowledgement.submissionId, transcript.submissionId)
        XCTAssertEqual(currentAcknowledgement.requestGeneration, transcript.requestGeneration)
        XCTAssertEqual(currentAcknowledgement.voiceTurnId, transcript.turnId)
        model.handleFrame(currentAcknowledgementFrame)
        model.consumeVoiceTranscript(transcript)
        XCTAssertEqual(sent.count, 3, "the current socket acknowledgement settles the retry")
    }

    func testVoicePlayoutUsesCurrentConnectionTransportWithoutQueueAlert() throws {
        let model = WatchModel()
        XCTAssertTrue(model.beginConversationConnection(connection))
        model.connected = true
        var liveOnly: [String] = []
        model.currentConnectionVoiceSendOverride = { liveOnly.append($0) }
        let announcement = try XCTUnwrap(
            WatchVoiceAnnouncement(json: announcementJSON(lastSequence: 149)))

        model.sendVoicePlayout(
            WatchVoicePlayoutObservation(
                announcement: announcement,
                phase: .started))

        let wire = try XCTUnwrap(liveOnly.first)
        XCTAssertNotNil(
            VoicePlayoutEvent(
                frame: try XCTUnwrap(InboundFrame.parse(wire)),
                packetBytes: wire.utf8.count))
        XCTAssertEqual(liveOnly.count, 1)
        XCTAssertNil(model.errorBanner)
    }

    func testVoiceRESTUsesBearerAndEveryBindingHeaderWithoutTicketInURL() async throws {
        let binding = try XCTUnwrap(
            WatchVoiceControlBinding(
                frame: InboundFrame.parse(bindingJSON(deviceId: device))!,
                expectedDeviceId: device,
                expectedConnection: connection))
        let recorder = RequestRecorder()
        let response = try voiceSessionGrantJSON().encoded()
        let client = WatchVoiceRESTClient(
            serverBase: URL(string: "https://astraldeep.example/")!,
            deviceId: device,
            connectionGeneration: connection,
            controlBinding: binding,
            tokenProvider: { "keycloak-token" },
            transport: { request in
                await recorder.record(request)
                return (201, response)
            })

        _ = try await client.createSession(
            chatId: chat,
            activationId: announcement,
            permission: .authorized)
        let recorded = await recorder.value()
        let request = try XCTUnwrap(recorded)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer keycloak-token")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Astral-Device-Id"), device)
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "X-Astral-Connection-Generation"),
            connection)
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "X-Astral-Voice-Control-Binding"),
            "synthetic-binding-value-000000000000")
        XCTAssertNil(request.url?.query)
        let body = try JSONValue.parse(try XCTUnwrap(request.httpBody))
        XCTAssertEqual(body["capability"]?["transport"]?.stringValue, "watch_pcm_websocket")
        XCTAssertEqual(body["capability"]?["full_duplex"]?.boolValue, false)
    }

    func testVisibleChatComposerActionDispatchesBoundSessionUpdate() async throws {
        let model = try configuredVoiceModel()
        model.activeChatId = otherChat
        model.voiceSession = try XCTUnwrap(
            WatchVoiceSession(
                json: voiceSessionJSON(
                    deviceId: model.voiceDeviceId,
                    visibleChatId: chat)))
        let recorder = RequestRecorder()
        model.voiceRESTTransport = { request in
            await recorder.record(request)
            return (409, Data("{\"code\":\"stale_generation\"}".utf8))
        }
        model.handleFrame(
            InboundFrame.parse(
                try composerJSON(
                    revision: 11,
                    actions: ["voice_visible_chat_update"]))!)

        model.performVoiceAction("voice_visible_chat_update")
        try await Task.sleep(for: .milliseconds(100))

        let recorded = await recorder.value()
        let request = try XCTUnwrap(recorded)
        XCTAssertEqual(request.httpMethod, "PATCH")
        XCTAssertTrue(request.url?.path.hasSuffix("/api/voice/sessions/\(session)") == true)
        let body = try JSONValue.parse(try XCTUnwrap(request.httpBody))
        XCTAssertEqual(body["visible_chat_id"]?.stringValue, otherChat)
        XCTAssertEqual(body["expected_generation"]?.numberValue, 1)
    }

    func testSensitiveRecapComposerActionUsesExactResultAndTurnConsent() async throws {
        let model = try configuredVoiceModel()
        model.voiceSession = try XCTUnwrap(
            WatchVoiceSession(
                json: voiceSessionJSON(
                    deviceId: model.voiceDeviceId,
                    visibleChatId: chat)))
        let recorder = RequestRecorder()
        model.voiceRESTTransport = { request in
            await recorder.record(request)
            return (202, Data())
        }
        model.handleFrame(InboundFrame.parse(try sensitiveTurnJSON())!)
        model.handleFrame(
            InboundFrame.parse(
                try composerJSON(
                    revision: 12,
                    actions: ["voice_sensitive_recap_request"]))!)

        model.performVoiceAction("voice_sensitive_recap_request")
        try await Task.sleep(for: .milliseconds(100))

        let recorded = await recorder.value()
        let request = try XCTUnwrap(recorded)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertTrue(
            request.url?.path.hasSuffix(
                "/api/voice/sessions/\(session)/results/\(result)/read-consent") == true)
        let body = try JSONValue.parse(try XCTUnwrap(request.httpBody))
        XCTAssertEqual(body["turn_id"]?.stringValue, turn)
        XCTAssertEqual(body["consent_method"]?.stringValue, "tap")
        XCTAssertEqual(body["expected_media_grant_revision"]?.numberValue, 2)
    }

    func testForegroundLeaseRenewsWithoutInteractionAndCancelsOnBackground() async throws {
        let model = try configuredVoiceModel()
        model.voiceSession = try XCTUnwrap(
            WatchVoiceSession(
                json: voiceSessionJSON(
                    deviceId: model.voiceDeviceId,
                    visibleChatId: chat)))
        model.voiceLeaseInterval = .milliseconds(20)
        let foregroundResponse = try voiceSessionJSON(
            deviceId: model.voiceDeviceId,
            visibleChatId: chat
        ).encoded()
        let backgroundResponse = try voiceSessionJSON(
            deviceId: model.voiceDeviceId,
            visibleChatId: chat,
            state: "suspended",
            foreground: false,
            foregroundReason: "backgrounded",
            microphone: false
        ).encoded()
        let recorder = RequestRecorder()
        model.voiceRESTTransport = { request in
            await recorder.record(request)
            let body = request.httpBody.flatMap { try? JSONValue.parse($0) }
            return (
                200,
                body?["foreground_active"]?.boolValue == false
                    ? backgroundResponse : foregroundResponse
            )
        }

        model.startVoiceLeaseRenewal()
        try await Task.sleep(for: .milliseconds(75))
        let renewalRequests = await recorder.values()
        let renewalBodies = renewalRequests.compactMap {
            $0.httpBody.flatMap { try? JSONValue.parse($0) }
        }
        XCTAssertGreaterThanOrEqual(renewalBodies.count, 2)
        XCTAssertTrue(
            renewalBodies.allSatisfy {
                $0["foreground_active"]?.boolValue == true
                    && $0["foreground_reason"]?.stringValue == "foreground"
                    && $0["interaction"] == nil
            })

        model.handleVoiceScenePhase(.background)
        try await Task.sleep(for: .milliseconds(40))
        let afterCancellation = await recorder.values()
        let foregroundCountAfterCancellation = afterCancellation.filter {
            $0.httpBody.flatMap { try? JSONValue.parse($0) }?["foreground_active"]?.boolValue
                == true
        }.count
        try await Task.sleep(for: .milliseconds(70))
        let finalRequests = await recorder.values()
        let finalForegroundCount = finalRequests.filter {
            $0.httpBody.flatMap { try? JSONValue.parse($0) }?["foreground_active"]?.boolValue
                == true
        }.count
        XCTAssertEqual(finalForegroundCount, foregroundCountAfterCancellation)
    }

    func testForegroundLeaseCancelsOnExplicitVoiceEnd() async throws {
        let model = try configuredVoiceModel()
        model.voiceSession = try XCTUnwrap(
            WatchVoiceSession(
                json: voiceSessionJSON(
                    deviceId: model.voiceDeviceId,
                    visibleChatId: chat)))
        model.voiceLeaseInterval = .milliseconds(20)
        let response = try voiceSessionJSON(
            deviceId: model.voiceDeviceId,
            visibleChatId: chat
        ).encoded()
        let recorder = RequestRecorder()
        model.voiceRESTTransport = { request in
            await recorder.record(request)
            return request.httpMethod == "DELETE" ? (204, Data()) : (200, response)
        }
        model.handleFrame(
            InboundFrame.parse(
                try composerJSON(
                    revision: 13,
                    actions: ["voice_session_end"]))!)
        model.startVoiceLeaseRenewal()
        try await Task.sleep(for: .milliseconds(55))

        model.performVoiceAction("voice_session_end")
        try await Task.sleep(for: .milliseconds(40))
        let afterEnd = await recorder.values()
        let renewalsAfterEnd = afterEnd.filter { $0.httpMethod == "PATCH" }.count
        XCTAssertTrue(afterEnd.contains { $0.httpMethod == "DELETE" })
        try await Task.sleep(for: .milliseconds(70))
        let finalRequests = await recorder.values()
        XCTAssertEqual(finalRequests.filter { $0.httpMethod == "PATCH" }.count, renewalsAfterEnd)
    }

    func testAccountRemovalStopsMediaAndBestEffortEndsVoiceSession() async throws {
        let model = try configuredVoiceModel()
        model.voiceSession = try XCTUnwrap(
            WatchVoiceSession(
                json: voiceSessionJSON(
                    deviceId: model.voiceDeviceId,
                    visibleChatId: chat)))
        let recorder = RequestRecorder()
        model.voiceRESTTransport = { request in
            await recorder.record(request)
            return (204, Data())
        }

        model.clearConversationForAccountRemoval()
        try await Task.sleep(for: .milliseconds(50))

        XCTAssertNil(model.voiceSession)
        XCTAssertEqual(model.voiceState, .off)
        let requests = await recorder.values()
        XCTAssertTrue(requests.contains { $0.httpMethod == "DELETE" })
    }

    func testWatchPrivacyDeclaresMicrophoneAndLinkedNonTrackingAudio() throws {
        XCTAssertNotNil(Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription"))
        let manifestURL = try XCTUnwrap(
            Bundle.main.url(forResource: "PrivacyInfo", withExtension: "xcprivacy"))
        let manifest = try plist(at: manifestURL)

        let collected = try XCTUnwrap(manifest["NSPrivacyCollectedDataTypes"] as? [[String: Any]])
        let audio = try XCTUnwrap(
            collected.first {
                $0["NSPrivacyCollectedDataType"] as? String
                    == "NSPrivacyCollectedDataTypeAudioData"
            })
        XCTAssertEqual(audio["NSPrivacyCollectedDataTypeLinked"] as? Bool, true)
        XCTAssertEqual(audio["NSPrivacyCollectedDataTypeTracking"] as? Bool, false)
    }

    private func composerJSON(
        revision: Int,
        connection: String? = nil,
        state: String = "off",
        foreground: Bool = false,
        actions: [String] = ["voice_session_start"],
        sessionId: String? = nil,
        generation: Int? = nil,
        mediaGrantRevision: Int? = nil
    ) throws -> String {
        var voice: [String: JSONValue] = [
            "available": .bool(true),
            "state": .string(state),
            "speech_muted": .bool(false),
            "microphone_enabled": .bool(false),
            "foreground_active": .bool(foreground),
            "reason": .string("ready"),
            "output_locale": .string("en-US"),
            "chat_context_revision": .null,
            "applied_chat_context_revision": .null,
            "chat_context_synced": .bool(false),
            "controls": .array(
                actions.enumerated().map { index, action in
                    .object([
                        "key": .string("voice-control-\(index)"),
                        "action": .string(action),
                        "label": .string("Voice action \(index + 1)"),
                        "icon": .string("microphone"),
                        "visible": .bool(true),
                        "enabled": .bool(true),
                        "pressed": .bool(false),
                        "busy": .bool(false),
                    ])
                }),
        ]
        if let sessionId { voice["session_id"] = .string(sessionId) }
        if let generation { voice["generation"] = .number(Double(generation)) }
        if let mediaGrantRevision {
            voice["media_grant_revision"] = .number(Double(mediaGrantRevision))
        }
        let value: JSONValue = .object([
            "type": .string("composer_state"),
            "schema_version": .string("1"),
            "revision": .number(Double(revision)),
            "connection_generation": .string(connection ?? self.connection),
            "voice": .object(voice),
        ])
        return String(data: try value.encoded(), encoding: .utf8)!
    }

    private func configuredVoiceModel(
        permission: WatchVoicePermission = .authorized
    ) throws -> WatchModel {
        let model = WatchModel()
        model.voiceBridge = MockWatchVoiceBridge(permission: permission)
        model.voiceTokenProvider = { "keycloak-token" }
        XCTAssertTrue(model.beginConversationConnection(connection))
        model.handleFrame(
            InboundFrame.parse(bindingJSON(deviceId: model.voiceDeviceId))!)
        return model
    }

    private func sensitiveTurnJSON() throws -> String {
        let value: JSONValue = .object([
            "type": .string("voice_turn_state"),
            "schema_version": .string("1"),
            "session_id": .string(session),
            "connection_generation": .string(connection),
            "generation": .number(1),
            "media_grant_revision": .number(2),
            "turn_id": .string(turn),
            "client_turn_id": .string(clientTurn),
            "submission_id": .string(submission),
            "request_generation": .string(request),
            "chat_id": .string(chat),
            "chat_context_revision": .number(1),
            "detected_language": .string("en-US"),
            "spoken_output_policy": .string("full_recap"),
            "output_reason": .string("ready"),
            "state": .string("succeeded"),
            "foreground": .bool(true),
            "sensitive_result_pending": .bool(true),
            "sequence": .number(1),
            "result_id": .string(result),
            "occurred_at": .string(timestamp(seconds: -1)),
        ])
        return String(data: try value.encoded(), encoding: .utf8)!
    }

    private func voiceTurnJSON(
        state: String,
        turnId: String? = nil,
        sequence: Int = 1,
        message: String? = nil,
        occurredAt: String? = nil,
        speechOutcome: String? = nil
    ) throws -> String {
        var value: [String: JSONValue] = [
            "type": .string("voice_turn_state"),
            "schema_version": .string("1"),
            "session_id": .string(session),
            "connection_generation": .string(connection),
            "generation": .number(1),
            "media_grant_revision": .number(2),
            "turn_id": .string(turnId ?? turn),
            "client_turn_id": .string(clientTurn),
            "submission_id": .string(submission),
            "request_generation": .string(request),
            "chat_id": .string(chat),
            "chat_context_revision": .number(1),
            "detected_language": .string("en-US"),
            "spoken_output_policy": .string("full_recap"),
            "output_reason": .string("ready"),
            "state": .string(state),
            "foreground": .bool(true),
            "sensitive_result_pending": .bool(false),
            "sequence": .number(Double(sequence)),
            "occurred_at": .string(occurredAt ?? timestamp(seconds: -1)),
        ]
        if let message { value["message"] = .string(message) }
        if let speechOutcome { value["speech_outcome"] = .string(speechOutcome) }
        return String(decoding: try JSONValue.object(value).encoded(), as: UTF8.self)
    }

    private func voiceSessionStateJSON(
        state: String,
        reason: String,
        message: String? = nil
    ) throws -> String {
        var value: [String: JSONValue] = [
            "type": .string("voice_session_state"),
            "schema_version": .string("1"),
            "session_id": .string(session),
            "connection_generation": .string(connection),
            "generation": .number(1),
            "media_grant_revision": .number(2),
            "visible_chat_id": .string(chat),
            "chat_context_revision": .number(1),
            "applied_chat_context_revision": .number(1),
            "chat_context_synced": .bool(true),
            "state": .string(state),
            "speech_muted": .bool(false),
            "microphone_enabled": .bool(true),
            "foreground_active": .bool(true),
            "reason": .string(reason),
            "occurred_at": .string(timestamp(seconds: -1)),
        ]
        if let message { value["message"] = .string(message) }
        return String(decoding: try JSONValue.object(value).encoded(), as: UTF8.self)
    }

    private func submissionRejectionJSON(
        message: String,
        retryPolicy: String
    ) throws -> String {
        let value: JSONValue = .object([
            "type": .string("voice_submission_rejected"),
            "schema_version": .string("1"),
            "session_id": .string(session),
            "connection_generation": .string(connection),
            "generation": .number(1),
            "media_grant_revision": .number(2),
            "turn_id": .string(turn),
            "client_turn_id": .string(clientTurn),
            "submission_id": .string(submission),
            "request_generation": .string(request),
            "chat_id": .string(chat),
            "reason": .string("capacity_exhausted"),
            "retry_policy": .string(retryPolicy),
            "message": .string(message),
            "occurred_at": .string(timestamp(seconds: -1)),
        ])
        return String(decoding: try value.encoded(), as: UTF8.self)
    }

    private func bridgeGrantJSON() -> JSONValue {
        .object([
            "grant_id": .string("watch-grant-01"),
            "transport": .string("watch_pcm_websocket"),
            "session_id": .string(session),
            "generation": .number(1),
            "media_grant_revision": .number(2),
            "expires_at": .string(timestamp(seconds: 600)),
            "url": .string("wss://astraldeep.example/api/voice/watch-bridge"),
            "ticket": .string(String(repeating: "t", count: 48)),
            "worker_identity": .string(worker),
            "capture": pcmProfile(rate: 16_000),
            "playback": pcmProfile(rate: 24_000),
        ])
    }

    private func readyJSON(worker: String) -> JSONValue {
        .object([
            "type": .string("bridge_ready"),
            "schema_version": .string("1"),
            "session_id": .string(session),
            "generation": .number(1),
            "media_grant_revision": .number(2),
            "worker_identity": .string(worker),
            "capture": pcmProfile(rate: 16_000),
            "playback": pcmProfile(rate: 24_000),
        ])
    }

    private func pcmProfile(rate: Int) -> JSONValue {
        .object([
            "encoding": .string("pcm_s16le"),
            "channels": .number(1),
            "sample_rate_hz": .number(Double(rate)),
            "frame_duration_ms": .number(20),
        ])
    }

    private func announcementJSON(lastSequence: Int) -> JSONValue {
        .object([
            "type": .string("voice_announcement_media"),
            "schema_version": .string("1"),
            "session_id": .string(session),
            "generation": .number(1),
            "media_grant_revision": .number(2),
            "announcement_id": .string("00000000-0000-4000-8000-00000000000c"),
            "announcement_sequence": .number(4),
            "turn_id": .string(turn),
            "kind": .string("acknowledgement"),
            "quantum_role": .string("single"),
            "quantum_index": .number(0),
            "transport": .string("watch_pcm_websocket"),
            "worker_identity": .string(worker),
            "sample_rate_hz": .number(24_000),
            "duration_samples": .number(24_000),
            "first_media_sequence": .number(100),
            "last_media_sequence": .number(Double(lastSequence)),
        ])
    }

    private func resultAnnouncementJSON(
        announcementSequence: Int,
        quantumRole: String,
        quantumIndex: Int,
        durationSamples: Int,
        firstMediaSequence: Int,
        reservedSamples: Int
    ) -> JSONValue {
        let frameCount = durationSamples / 480
        return .object([
            "type": .string("voice_announcement_media"),
            "schema_version": .string("1"),
            "session_id": .string(session),
            "generation": .number(1),
            "media_grant_revision": .number(2),
            "announcement_id": .string(
                String(format: "00000000-0000-4000-8000-%012x", announcementSequence)),
            "announcement_sequence": .number(Double(announcementSequence)),
            "turn_id": .string(turn),
            "kind": .string("result"),
            "quantum_role": .string(quantumRole),
            "quantum_index": .number(Double(quantumIndex)),
            "transport": .string("watch_pcm_websocket"),
            "worker_identity": .string(worker),
            "sample_rate_hz": .number(24_000),
            "duration_samples": .number(Double(durationSamples)),
            "result_reserved_samples_after": .number(Double(reservedSamples)),
            "first_media_sequence": .number(Double(firstMediaSequence)),
            "last_media_sequence": .number(Double(firstMediaSequence + frameCount - 1)),
        ])
    }

    private func finalTranscriptJSON() -> JSONValue {
        .object([
            "type": .string("voice_transcript"),
            "schema_version": .string("1"),
            "session_id": .string(session),
            "generation": .number(1),
            "turn_id": .string(turn),
            "client_turn_id": .string(clientTurn),
            "submission_id": .string(submission),
            "request_generation": .string(request),
            "chat_id": .string(chat),
            "chat_context_revision": .number(3),
            "media_grant_revision": .number(2),
            "sequence": .number(1),
            "final": .bool(true),
            "text": .string("Please review Café.\nKeep caveats."),
            "detected_language": .string("en-US"),
            "text_digest_sha256": .string(String(repeating: "a", count: 64)),
            "transcript_proof": .string(String(repeating: "b", count: 64)),
            "proof_expires_at": .string(timestamp(seconds: 120)),
            "source_participant_identity": .string(worker),
        ])
    }

    private func bindingJSON(deviceId: String) -> String {
        """
        {"type":"voice_control_binding","schema_version":"1","device_id":"\(deviceId)",
         "connection_generation":"\(connection)",
         "binding_id":"00000000-0000-4000-8000-00000000000a",
         "binding":"synthetic-binding-value-000000000000",
         "expires_at":"\(timestamp(seconds: 600))"}
        """
    }

    private func voiceSessionGrantJSON() -> JSONValue {
        .object([
            "session": voiceSessionJSON(deviceId: device, visibleChatId: chat),
            "grant": bridgeGrantJSON(),
        ])
    }

    private func voiceSessionJSON(
        deviceId: String,
        visibleChatId: String,
        state: String = "starting",
        foreground: Bool = true,
        foregroundReason: String = "foreground",
        microphone: Bool = true
    ) -> JSONValue {
        .object([
            "session_id": .string(session),
            "device_id": .string(deviceId),
            "device_kind": .string("watchos"),
            "transport": .string("watch_pcm_websocket"),
            "state": .string(state),
            "generation": .number(1),
            "media_grant_revision": .number(2),
            "owner_connection_generation": .string(connection),
            "visible_chat_id": .string(visibleChatId),
            "applied_visible_chat_id": .string(visibleChatId),
            "chat_context_revision": .number(1),
            "applied_chat_context_revision": .number(1),
            "chat_context_synced": .bool(true),
            "foreground_active": .bool(foreground),
            "foreground_reason": .string(foregroundReason),
            "foreground_changed_at": .string(timestamp(seconds: -1)),
            "speech_muted": .bool(false),
            "microphone_enabled": .bool(microphone),
            "lease_expires_at": .string(timestamp(seconds: 600)),
            "started_at": .string(timestamp(seconds: -1)),
            "idle_expires_at": .string(timestamp(seconds: 300)),
        ])
    }

    private func timestamp(seconds: TimeInterval) -> String {
        ISO8601DateFormatter().string(from: Date().addingTimeInterval(seconds))
    }

    private func plist(at url: URL) throws -> [String: Any] {
        let data = try Data(contentsOf: url)
        return try XCTUnwrap(
            PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
    }
}

@MainActor
private final class MockWatchVoiceBridge: WatchVoiceBridgeControlling {
    var state: WatchVoiceBridgeState = .idle
    let microphonePermission: WatchVoicePermission

    init(permission: WatchVoicePermission) {
        microphonePermission = permission
    }

    func requestMicrophonePermission() async -> WatchVoicePermission { microphonePermission }
    func setEventHandler(_ handler: @escaping @MainActor (WatchVoiceAudioEvent) -> Void) {}

    func connect(
        grant: WatchVoiceBridgeGrant,
        onState: @escaping @MainActor (WatchVoiceBridgeState) -> Void,
        onTranscript: @escaping @MainActor (WatchVoiceTranscript) -> Void,
        onPlayout: @escaping @MainActor (WatchVoicePlayoutObservation) -> Void
    ) async throws {
        state = .ready
        onState(.ready)
    }

    func setCaptureEnabled(_ enabled: Bool) {}
    func interruptPlayback() {}
    func disconnect(reason: String) { state = .ended }
}

private actor RequestRecorder {
    private var requests: [URLRequest] = []

    func record(_ request: URLRequest) { requests.append(request) }
    func value() -> URLRequest? { requests.last }
    func values() -> [URLRequest] { requests }
}
