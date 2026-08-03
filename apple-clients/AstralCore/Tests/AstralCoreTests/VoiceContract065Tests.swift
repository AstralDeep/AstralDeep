import CryptoKit
import Foundation
import XCTest

@testable import AstralCore

final class VoiceContract065Tests: XCTestCase {
    private struct Fixture {
        let root: JSONValue
        let positives: [String: JSONValue]

        init() throws {
            guard
                let url = Bundle.module.url(
                    forResource: "client_conformance", withExtension: "json")
            else {
                throw NSError(
                    domain: "VoiceContract065Tests", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "bundled canonical fixture is missing"])
            }
            let data = try Data(contentsOf: url)
            let digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
            guard
                digest == "bc98077594fa8d51dd664fadefaa48cf596a94e7fb2a961a972dbabca4f02143"
            else {
                throw NSError(
                    domain: "VoiceContract065Tests", code: 2,
                    userInfo: [NSLocalizedDescriptionKey: "bundled canonical fixture digest differs"])
            }
            root = try JSONValue.parse(data)
            var values: [String: JSONValue] = [:]
            for testCase in root["cases"]?.arrayValue ?? [] {
                for vector in testCase["positive"]?.arrayValue ?? [] {
                    if let id = vector["id"]?.stringValue, let payload = vector["payload"] {
                        values[id] = payload
                    }
                }
            }
            positives = values
        }
    }

    func testSharedC0ThroughC6PositiveAndNegativeVectors() throws {
        let fixture = try Fixture()
        let cases = fixture.root["cases"]?.arrayValue ?? []
        var resolved = fixture.positives
        XCTAssertEqual(cases.compactMap { $0["id"]?.stringValue }, ["C0", "C1", "C2", "C3", "C4", "C5", "C6"])

        for testCase in cases {
            for vector in testCase["positive"]?.arrayValue ?? [] {
                let id = try XCTUnwrap(vector["id"]?.stringValue)
                var payload: JSONValue
                if let direct = vector["payload"] {
                    payload = direct
                } else {
                    let base = try XCTUnwrap(vector["base_vector"]?.stringValue)
                    payload = try XCTUnwrap(resolved[base])
                    for mutation in vector["mutations"]?.arrayValue ?? [] {
                        payload = mutate(payload, mutation: mutation)
                    }
                }
                resolved[id] = payload
                XCTAssertTrue(
                    accepts(payload, context: vector["context"]),
                    "expected positive vector \(id) to be accepted")
            }
            for vector in testCase["negative"]?.arrayValue ?? [] {
                let id = try XCTUnwrap(vector["id"]?.stringValue)
                let base = try XCTUnwrap(vector["base_vector"]?.stringValue)
                var payload = try XCTUnwrap(resolved[base])
                for mutation in vector["mutations"]?.arrayValue ?? [] {
                    payload = mutate(payload, mutation: mutation)
                }
                XCTAssertFalse(
                    accepts(payload, context: vector["context"]),
                    "expected negative vector \(id) to be rejected")
            }
        }
    }

    func testVoiceRegistrationIsStableDeviceBoundAndAdvertisesRuntimeFacts() throws {
        let deviceId = "00000000-0000-4000-8000-000000000001"
        let connection = "00000000-0000-4000-8000-000000000002"
        var device = DeviceDescriptor.ios(viewportWidth: 390, viewportHeight: 844)
        device.deviceId = deviceId
        device.microphonePermission = "authorized"
        let raw = Outbound.registerUI(
            token: "secret", sessionId: nil, device: device, resumed: false,
            connectionGeneration: connection)
        let value = try JSONValue.parse(Data(raw.utf8))
        XCTAssertEqual(value["device_id"]?.stringValue, deviceId)
        XCTAssertEqual(value["capabilities"]?.arrayValue?.compactMap(\.stringValue), ["render", "stream", "voice"])
        XCTAssertEqual(value["device"]?["microphone_permission"]?.stringValue, "authorized")
        XCTAssertEqual(value["device"]?["voice_transport"]?.stringValue, "livekit")
    }

    func testFinalTranscriptUsesOnlyOrdinaryChatMessageAndImmutableIdentifiers() throws {
        let fixture = try Fixture()
        let payload = try XCTUnwrap(fixture.positives["C2-P2-final"])
        let data = try payload.encoded()
        let frame = try XCTUnwrap(InboundFrame.parse(String(decoding: data, as: UTF8.self)))
        let transcript = try XCTUnwrap(VoiceTranscript(frame: frame, packetBytes: data.count))
        let connection = "00000000-0000-4000-8000-000000000002"
        let first = Outbound.voiceChatMessage(transcript: transcript, connectionGeneration: connection)
        let retry = Outbound.voiceChatMessage(transcript: transcript, connectionGeneration: connection)
        let outbound = try JSONValue.parse(Data(first.utf8))
        XCTAssertEqual(outbound, try JSONValue.parse(Data(retry.utf8)))
        XCTAssertEqual(outbound["action"]?.stringValue, "chat_message")
        XCTAssertEqual(outbound["submission_id"]?.stringValue, transcript.submissionId)
        XCTAssertEqual(outbound["payload"]?["voice_origin"]?["turn_id"]?.stringValue, transcript.turnId)
        XCTAssertNil(outbound["payload"]?["audio"])
    }

    func testCurrentConnectionVoiceFramesAreStrictlyTypedAndNeverReplayable() throws {
        let fixture = try Fixture()
        let connection = "00000000-0000-4000-8000-000000000002"

        let transcriptPayload = try XCTUnwrap(fixture.positives["C2-P2-final"])
        let transcriptData = try transcriptPayload.encoded()
        let transcript = try XCTUnwrap(
            VoiceTranscript(
                frame: try XCTUnwrap(
                    InboundFrame.parse(String(decoding: transcriptData, as: UTF8.self))),
                packetBytes: transcriptData.count))
        let transcriptText = Outbound.voiceChatMessage(
            transcript: transcript, connectionGeneration: connection)
        let transcriptFrame = try XCTUnwrap(
            VoiceCurrentConnectionFrame(frameText: transcriptText))
        XCTAssertEqual(transcriptFrame.kind, .finalTranscript)
        XCTAssertNil(QueuedOperationReplay(frameText: transcriptText))

        let newChatText = Outbound.correlatedVoiceNewChat(
            connectionGeneration: connection,
            submissionId: "00000000-0000-4000-8000-000000000003",
            requestGeneration: "00000000-0000-4000-8000-000000000004")
        let newChatFrame = try XCTUnwrap(
            VoiceCurrentConnectionFrame(frameText: newChatText))
        XCTAssertEqual(newChatFrame.kind, .correlatedNewChat)
        XCTAssertNil(QueuedOperationReplay(frameText: newChatText))

        let announcementPayload = try XCTUnwrap(
            fixture.positives["C3-P1-livekit-opening"])
        let announcement = try XCTUnwrap(
            VoiceAnnouncementMedia(frame: try XCTUnwrap(frame(announcementPayload))))
        let playout = try XCTUnwrap(
            VoicePlayoutEvent(
                deviceId: "00000000-0000-4000-8000-000000000001",
                connectionGeneration: connection,
                announcement: announcement,
                phase: "started",
                clientSequence: 0,
                observedAt: "2026-07-31T12:00:00Z"))
        let playoutText = Outbound.voicePlayoutEvent(playout)
        let playoutFrame = try XCTUnwrap(
            VoiceCurrentConnectionFrame(frameText: playoutText))
        XCTAssertEqual(playoutFrame.kind, .playoutEvent)
        XCTAssertNil(QueuedOperationReplay(frameText: playoutText))

        let ordinaryText = Outbound.chatMessage(
            "typed message",
            sessionId: transcript.chatId,
            submissionId: "00000000-0000-4000-8000-000000000005",
            requestGeneration: "00000000-0000-4000-8000-000000000006")
        XCTAssertNil(VoiceCurrentConnectionFrame(frameText: ordinaryText))
        XCTAssertNotNil(QueuedOperationReplay(frameText: ordinaryText))
    }

    func testMalformedClaimedVoiceFramesAreRejectedInsteadOfFallingBackToReplay() throws {
        let fixture = try Fixture()
        let connection = "00000000-0000-4000-8000-000000000002"
        let transcriptPayload = try XCTUnwrap(fixture.positives["C2-P2-final"])
        let transcriptData = try transcriptPayload.encoded()
        let transcript = try XCTUnwrap(
            VoiceTranscript(
                frame: try XCTUnwrap(
                    InboundFrame.parse(String(decoding: transcriptData, as: UTF8.self))),
                packetBytes: transcriptData.count))

        var malformedTranscript = try XCTUnwrap(
            try JSONValue.parse(
                Data(
                    Outbound.voiceChatMessage(
                        transcript: transcript, connectionGeneration: connection
                    ).utf8
                )
            ).objectValue)
        var malformedPayload = try XCTUnwrap(malformedTranscript["payload"]?.objectValue)
        var malformedOrigin = try XCTUnwrap(malformedPayload["voice_origin"]?.objectValue)
        malformedOrigin["transcript_proof"] = .string("not-a-proof")
        malformedPayload["voice_origin"] = .object(malformedOrigin)
        malformedTranscript["payload"] = .object(malformedPayload)
        let malformedTranscriptText = String(
            decoding: try JSONValue.object(malformedTranscript).encoded(), as: UTF8.self)
        XCTAssertNil(VoiceCurrentConnectionFrame(frameText: malformedTranscriptText))
        XCTAssertNil(QueuedOperationReplay(frameText: malformedTranscriptText))

        var malformedNewChat = try XCTUnwrap(
            try JSONValue.parse(
                Data(
                    Outbound.correlatedVoiceNewChat(
                        connectionGeneration: connection,
                        submissionId: "00000000-0000-4000-8000-000000000003",
                        requestGeneration: "00000000-0000-4000-8000-000000000004"
                    ).utf8
                )
            ).objectValue)
        var newChatPayload = try XCTUnwrap(malformedNewChat["payload"]?.objectValue)
        newChatPayload["connection_generation"] = .string(
            "00000000-0000-4000-8000-000000000009")
        malformedNewChat["payload"] = .object(newChatPayload)
        let malformedNewChatText = String(
            decoding: try JSONValue.object(malformedNewChat).encoded(), as: UTF8.self)
        XCTAssertNil(VoiceCurrentConnectionFrame(frameText: malformedNewChatText))
        XCTAssertNil(QueuedOperationReplay(frameText: malformedNewChatText))
    }

    func testDisconnectedCurrentConnectionVoiceSendIsDroppedWithoutARejectionEvent() async throws {
        let client = WSClient(url: URL(string: "ws://127.0.0.1:9/ws")!)
        let events = await client.events()
        let text = Outbound.correlatedVoiceNewChat(
            connectionGeneration: "00000000-0000-4000-8000-000000000002",
            submissionId: "00000000-0000-4000-8000-000000000003",
            requestGeneration: "00000000-0000-4000-8000-000000000004")

        await client.send(text)
        let unexpectedEvent = await nextEvent(from: events, timeoutNanoseconds: 50_000_000)
        XCTAssertNil(unexpectedEvent)

        let typedClient = WSClient(url: URL(string: "ws://127.0.0.1:9/ws")!)
        let sentWhileDisconnected = await typedClient.sendCurrentConnectionVoice(text)
        XCTAssertFalse(sentWhileDisconnected)

        let malformedClient = WSClient(url: URL(string: "ws://127.0.0.1:9/ws")!)
        let malformedEvents = await malformedClient.events()
        let malformedAccepted = await malformedClient.sendCurrentConnectionVoice(
            #"{"type":"voice_playout_event"}"#)
        XCTAssertFalse(malformedAccepted)
        guard
            let event = await nextEvent(
                from: malformedEvents, timeoutNanoseconds: 50_000_000)
        else { return XCTFail("malformed current-connection voice frame was not rejected") }
        guard case .sendRejected = event else {
            return XCTFail("expected sendRejected for malformed voice frame")
        }
    }

    func testAnnouncementLedgerRejectsReplayOutOfOrderAndAggregateOverflow() throws {
        let fixture = try Fixture()
        let payload = try XCTUnwrap(fixture.positives["C3-P1-livekit-opening"])
        let openingFrame = try XCTUnwrap(frame(payload))
        let opening = try XCTUnwrap(VoiceAnnouncementMedia(frame: openingFrame))
        var ledger = VoiceAnnouncementLedger()
        XCTAssertTrue(ledger.accept(opening))
        XCTAssertFalse(ledger.accept(opening), "announcement replay must not reopen a stream")

        var continuationRoot = try XCTUnwrap(payload.objectValue)
        continuationRoot["announcement_id"] = .string("00000000-0000-4000-8000-00000000000a")
        continuationRoot["announcement_sequence"] = .number(4)
        continuationRoot["quantum_role"] = .string("result_continuation")
        continuationRoot["quantum_index"] = .number(1)
        continuationRoot["duration_samples"] = .number(1_000)
        continuationRoot["result_reserved_samples_after"] = .number(1_000)
        let underReserved = try XCTUnwrap(
            VoiceAnnouncementMedia(frame: try XCTUnwrap(frame(.object(continuationRoot)))))
        XCTAssertFalse(
            ledger.accept(underReserved),
            "a continuation cannot lower the durable reservation below cumulative audio")

        continuationRoot["result_reserved_samples_after"] = .number(37_000)
        let validContinuation = try XCTUnwrap(
            VoiceAnnouncementMedia(frame: try XCTUnwrap(frame(.object(continuationRoot)))))
        XCTAssertTrue(ledger.accept(validContinuation))
    }

    func testForegroundReconnectAndErrorStatesMatchTheSharedSchema() throws {
        let fixture = try Fixture()
        for state in ["reconnecting", "error"] {
            var composerRoot = try XCTUnwrap(fixture.positives["C0-P1-composer"]?.objectValue)
            var composerVoice = try XCTUnwrap(composerRoot["voice"]?.objectValue)
            composerVoice["state"] = .string(state)
            composerRoot["voice"] = .object(composerVoice)
            XCTAssertNotNil(
                VoiceComposerState(frame: try XCTUnwrap(frame(.object(composerRoot)))),
                "foreground composer state \(state) is explicitly allowed")

            var sessionRoot = try XCTUnwrap(fixture.positives["C5-P1-active"]?.objectValue)
            sessionRoot["state"] = .string(state)
            XCTAssertNotNil(
                VoiceSessionState(frame: try XCTUnwrap(frame(.object(sessionRoot)))),
                "foreground session state \(state) is explicitly allowed")
        }
    }

    func testOffComposerMayOmitItsOptionalSessionIdentifier() throws {
        let fixture = try Fixture()
        var root = try XCTUnwrap(fixture.positives["C0-P1-composer"]?.objectValue)
        var voice = try XCTUnwrap(root["voice"]?.objectValue)
        voice.removeValue(forKey: "session_id")
        root["voice"] = .object(voice)
        XCTAssertNotNil(
            VoiceComposerState(frame: try XCTUnwrap(frame(.object(root)))))
    }

    func testThreeLetterEngCodeDoesNotMatchTheEnglishOutputPolicy() throws {
        let fixture = try Fixture()
        var root = try XCTUnwrap(fixture.positives["C4-P1-en"]?.objectValue)
        root["detected_language"] = .string("eng")
        XCTAssertNil(VoiceTurnState(frame: try XCTUnwrap(frame(.object(root)))))

        root["spoken_output_policy"] = .string("english_lifecycle_only")
        root["output_reason"] = .string("output_language_unsupported")
        XCTAssertNotNil(VoiceTurnState(frame: try XCTUnwrap(frame(.object(root)))))
    }

    func testVoiceTurnSpeechOutcomeIsOptionalBoundedAndSuccessOnly() throws {
        let fixture = try Fixture()

        for outcome in VoiceSpeechOutcome.allCases {
            let turn = try voiceTurn(
                fixture: fixture, state: "succeeded",
                speechOutcome: outcome.rawValue)
            XCTAssertEqual(turn.speechOutcome, outcome)
        }

        let legacy = try voiceTurn(fixture: fixture, state: "succeeded")
        XCTAssertNil(legacy.speechOutcome)

        var root = try XCTUnwrap(fixture.positives["C4-P1-en"]?.objectValue)
        root["state"] = .string("succeeded")
        root["speech_outcome"] = .string("provider_detail")
        XCTAssertNil(VoiceTurnState(frame: try XCTUnwrap(frame(.object(root)))))

        root["state"] = .string("processing")
        root["speech_outcome"] = .string("failed")
        XCTAssertNil(VoiceTurnState(frame: try XCTUnwrap(frame(.object(root)))))
    }

    func testTerminalTurnNoticeUsesExplicitNonColorWordingAndPreservesServerMessage() throws {
        let fixture = try Fixture()
        let expectedTitles = [
            "failed": "Request did not complete.",
            "cancelled": "Request did not complete.",
            "abandoned": "Request did not complete.",
            "refused": "Request did not start.",
        ]

        for (state, expectedTitle) in expectedTitles {
            let turn = try voiceTurn(
                fixture: fixture, state: state,
                message: "The server kept this safe explanation.")
            let notice = try XCTUnwrap(
                VoiceTerminalNoticeReducer.reduce(current: nil, turn: turn))

            XCTAssertEqual(notice.kind, .requestFailure)
            XCTAssertEqual(notice.title, expectedTitle)
            XCTAssertEqual(notice.serverMessage, "The server kept this safe explanation.")
            XCTAssertTrue(notice.displayText.hasPrefix("Warning. \(expectedTitle)"))
            XCTAssertTrue(notice.displayText.contains(notice.serverMessage))
            XCTAssertEqual(notice.accessibilityLabel, notice.displayText)
        }
    }

    func testTerminalNoticeClearsOnlyForDifferentAcceptedProcessingOrSucceededTurn() throws {
        let fixture = try Fixture()
        let failed = try voiceTurn(
            fixture: fixture, state: "failed", message: "The first request failed.",
            occurredAt: "2026-07-31T12:05:00Z")
        let notice = try XCTUnwrap(
            VoiceTerminalNoticeReducer.reduce(current: nil, turn: failed))

        for state in ["accepted", "processing", "succeeded"] {
            let sameTurn = try voiceTurn(
                fixture: fixture, state: state, turnId: failed.turnId, sequence: 2)
            XCTAssertEqual(
                VoiceTerminalNoticeReducer.reduce(current: notice, turn: sameTurn), notice,
                "same-turn lifecycle churn must not erase its terminal notice")
        }

        let waitingOnAnotherTurn = try voiceTurn(
            fixture: fixture, state: "waiting_on_user",
            turnId: "00000000-0000-4000-8000-000000000205", sequence: 1)
        XCTAssertEqual(
            VoiceTerminalNoticeReducer.reduce(current: notice, turn: waitingOnAnotherTurn),
            notice,
            "unrelated session/turn churn does not clear the notice")

        for state in ["accepted", "processing", "succeeded"] {
            let olderTurn = try voiceTurn(
                fixture: fixture, state: state,
                turnId: "00000000-0000-4000-8000-000000000205", sequence: 2,
                occurredAt: "2026-07-31T12:04:59Z")
            XCTAssertEqual(
                VoiceTerminalNoticeReducer.reduce(current: notice, turn: olderTurn),
                notice,
                "an older different-turn \(state) frame must not supersede the notice")

            let nextTurn = try voiceTurn(
                fixture: fixture, state: state,
                turnId: "00000000-0000-4000-8000-000000000205", sequence: 3,
                occurredAt: "2026-07-31T12:05:01Z")
            XCTAssertNil(
                VoiceTerminalNoticeReducer.reduce(current: notice, turn: nextTurn),
                "a different active or successful turn supersedes the old notice")
        }
    }

    func testSpeechFailureNeverClaimsTheRequestFailedAndKeepsTextResultGuidance() {
        let notice = VoiceTerminalNoticeReducer.speechFailure(
            message: "Assistant audio could not be played.",
            turnId: "00000000-0000-4000-8000-000000000105",
            occurredAt: "2026-07-31T12:05:00Z")

        XCTAssertEqual(notice.kind, .speechFailure)
        XCTAssertEqual(notice.title, "Speech playback failed.")
        XCTAssertEqual(notice.serverMessage, "Assistant audio could not be played.")
        XCTAssertEqual(notice.occurredAt, "2026-07-31T12:05:00Z")
        XCTAssertTrue(notice.displayText.contains("text result may still be available"))
        XCTAssertFalse(notice.displayText.localizedCaseInsensitiveContains("request failed"))
        XCTAssertFalse(notice.displayText.contains("Request did not complete"))
    }

    func testSucceededSpeechFailureUsesExactTurnAndCommittedTextGuidance() throws {
        let fixture = try Fixture()
        let failedTurn = try voiceTurn(
            fixture: fixture, state: "succeeded",
            message: "The result audio could not be delivered.",
            occurredAt: "2026-07-31T12:05:00Z", speechOutcome: "failed")
        let notice = try XCTUnwrap(
            VoiceTerminalNoticeReducer.reduce(current: nil, turn: failedTurn))

        XCTAssertEqual(notice.kind, .speechFailure)
        XCTAssertEqual(notice.turnId, failedTurn.turnId)
        XCTAssertEqual(notice.occurredAt, failedTurn.occurredAt)
        XCTAssertEqual(notice.serverMessage, "The result audio could not be delivered.")
        XCTAssertTrue(notice.displayText.contains("text result is still available"))
        XCTAssertFalse(notice.displayText.localizedCaseInsensitiveContains("request failed"))

        for outcome in [nil, "source_finished", "suppressed"] as [String?] {
            let successfulTurn = try voiceTurn(
                fixture: fixture, state: "succeeded", speechOutcome: outcome)
            XCTAssertNil(
                VoiceTerminalNoticeReducer.reduce(current: nil, turn: successfulTurn),
                "\(outcome ?? "absent") must remain a normal successful result")
        }

        let newerFailedTurn = try voiceTurn(
            fixture: fixture, state: "succeeded",
            turnId: "00000000-0000-4000-8000-000000000205",
            occurredAt: "2026-07-31T12:05:01Z", speechOutcome: "failed")
        let newerNotice = try XCTUnwrap(
            VoiceTerminalNoticeReducer.reduce(current: notice, turn: newerFailedTurn))
        XCTAssertEqual(newerNotice.turnId, newerFailedTurn.turnId)
        XCTAssertEqual(
            VoiceTerminalNoticeReducer.reduce(current: newerNotice, turn: failedTurn),
            newerNotice,
            "an older different-turn speech error cannot replace the newer notice")
    }

    func testSubmissionRejectionNoticePreservesMessageAndRequiresExplicitRetry() throws {
        let fixture = try Fixture()
        var root = try XCTUnwrap(fixture.positives["C2-P4-rejected"]?.objectValue)
        root["message"] = .string("Capacity is temporarily full.")
        root["retry_policy"] = .string("explicit_user_retry")
        let rejection = try XCTUnwrap(
            VoiceSubmissionRejected(frame: try XCTUnwrap(frame(.object(root)))))

        let notice = VoiceTerminalNoticeReducer.submissionRejected(rejection)

        XCTAssertEqual(notice.kind, .requestFailure)
        XCTAssertEqual(notice.title, "Request did not start.")
        XCTAssertEqual(notice.serverMessage, "Capacity is temporarily full.")
        XCTAssertEqual(notice.guidance, "Please say it again when you are ready.")
        XCTAssertTrue(notice.displayText.contains("Capacity is temporarily full."))
        XCTAssertTrue(notice.displayText.contains("Please say it again when you are ready."))

        let failed = try voiceTurn(
            fixture: fixture, state: "failed", message: "Keep this newer failure.",
            occurredAt: "2026-07-31T12:05:00Z")
        let current = try XCTUnwrap(
            VoiceTerminalNoticeReducer.reduce(current: nil, turn: failed))
        root["turn_id"] = .string("00000000-0000-4000-8000-000000000205")
        root["occurred_at"] = .string("2026-07-31T12:04:59Z")
        let olderRejection = try XCTUnwrap(
            VoiceSubmissionRejected(frame: try XCTUnwrap(frame(.object(root)))))
        XCTAssertEqual(
            VoiceTerminalNoticeReducer.reduce(current: current, rejection: olderRejection),
            current,
            "an older different-turn rejection cannot replace the newer notice")

        root["occurred_at"] = .string("2026-07-31T12:05:01Z")
        let newerRejection = try XCTUnwrap(
            VoiceSubmissionRejected(frame: try XCTUnwrap(frame(.object(root)))))
        XCTAssertEqual(
            VoiceTerminalNoticeReducer.reduce(current: current, rejection: newerRejection)?
                .turnId,
            "00000000-0000-4000-8000-000000000205")
    }

    private func accepts(_ payload: JSONValue, context: JSONValue?) -> Bool {
        guard let data = try? payload.encoded(), let frame = frame(payload),
            let type = payload["type"]?.stringValue
        else { return false }
        let mediaContext = VoiceMediaContext(
            expectedWorkerIdentity: context?["expected_worker_identity"]?.stringValue,
            expectedDeviceId: context?["expected_device_id"]?.stringValue,
            expectedConnectionGeneration: context?["expected_connection_generation"]?.stringValue,
            expectedSessionId: context?["expected_session_id"]?.stringValue,
            expectedGeneration: context?["expected_generation"]?.numberValue.map(Int.init),
            expectedMediaGrantRevision: context?["expected_media_grant_revision"]?.numberValue.map(Int.init))

        switch type {
        case "composer_state": return VoiceComposerState(frame: frame) != nil
        case "voice_control_binding": return VoiceControlBinding(frame: frame) != nil
        case "ui_event": return CorrelatedVoiceNewChat(frame: frame) != nil
        case "chat_created": return CorrelatedVoiceChatCreated(frame: frame) != nil
        case "voice_session_state":
            guard let value = VoiceSessionState(frame: frame) else { return false }
            if let expected = mediaContext.expectedConnectionGeneration,
                value.connectionGeneration != expected
            {
                return false
            }
            return true
        case "voice_turn_state": return VoiceTurnState(frame: frame) != nil
        case "voice_transcript":
            guard let value = VoiceTranscript(frame: frame, packetBytes: data.count) else { return false }
            return mediaContext.accepts(value, participantIdentity: nil)
        case "user_message_acked": return VoiceMessageAcknowledgement(frame: frame) != nil
        case "voice_submission_rejected": return VoiceSubmissionRejected(frame: frame) != nil
        case "voice_announcement_media":
            guard let value = VoiceAnnouncementMedia(frame: frame, packetBytes: data.count) else { return false }
            return mediaContext.accepts(value, participantIdentity: nil)
        case "voice_playout_event":
            guard let value = VoicePlayoutEvent(frame: frame, packetBytes: data.count) else { return false }
            if let expected = mediaContext.expectedDeviceId, value.deviceId != expected { return false }
            if let expected = mediaContext.expectedConnectionGeneration,
                value.connectionGeneration != expected
            {
                return false
            }
            if let expected = mediaContext.expectedSessionId, value.sessionId != expected { return false }
            if let expected = mediaContext.expectedGeneration, value.generation != expected { return false }
            if let expected = mediaContext.expectedMediaGrantRevision,
                value.mediaGrantRevision != expected
            {
                return false
            }
            return true
        default: return false
        }
    }

    private func frame(_ payload: JSONValue) -> InboundFrame? {
        guard let data = try? payload.encoded() else { return nil }
        return InboundFrame.parse(String(decoding: data, as: UTF8.self))
    }

    private func voiceTurn(
        fixture: Fixture,
        state: String,
        turnId: String = "00000000-0000-4000-8000-000000000105",
        sequence: Int = 1,
        message: String? = nil,
        occurredAt: String? = nil,
        speechOutcome: String? = nil
    ) throws -> VoiceTurnState {
        var root = try XCTUnwrap(fixture.positives["C4-P1-en"]?.objectValue)
        root["state"] = .string(state)
        root["turn_id"] = .string(turnId)
        root["sequence"] = .number(Double(sequence))
        if let message {
            root["message"] = .string(message)
        } else {
            root.removeValue(forKey: "message")
        }
        if let occurredAt { root["occurred_at"] = .string(occurredAt) }
        if let speechOutcome {
            root["speech_outcome"] = .string(speechOutcome)
        } else {
            root.removeValue(forKey: "speech_outcome")
        }
        return try XCTUnwrap(
            VoiceTurnState(frame: try XCTUnwrap(frame(.object(root)))))
    }

    private func nextEvent(
        from stream: AsyncStream<WSEvent>, timeoutNanoseconds: UInt64
    ) async -> WSEvent? {
        await withTaskGroup(of: WSEvent?.self) { group in
            group.addTask {
                var iterator = stream.makeAsyncIterator()
                return await iterator.next()
            }
            group.addTask {
                try? await Task.sleep(nanoseconds: timeoutNanoseconds)
                return nil
            }
            let result = await group.next() ?? nil
            group.cancelAll()
            return result
        }
    }

    private func mutate(_ root: JSONValue, mutation: JSONValue) -> JSONValue {
        guard let op = mutation["op"]?.stringValue,
            let rawPath = mutation["path"]?.stringValue
        else { return root }
        let path = rawPath.split(separator: "/").map(String.init)
        return mutate(
            root, path: path, op: op, value: mutation["value"], count: mutation["count"]?.numberValue.map(Int.init))
    }

    private func mutate(
        _ root: JSONValue, path: [String], op: String, value: JSONValue?, count: Int?
    ) -> JSONValue {
        guard let key = path.first, var object = root.objectValue else { return root }
        if path.count > 1 {
            guard let child = object[key] else { return root }
            object[key] = mutate(child, path: Array(path.dropFirst()), op: op, value: value, count: count)
            return .object(object)
        }
        switch op {
        case "remove": object.removeValue(forKey: key)
        case "add", "replace": if let value { object[key] = value }
        case "repeat":
            if let scalar = value?.stringValue, let count {
                object[key] = .string(String(repeating: scalar, count: count))
            }
        default: break
        }
        return .object(object)
    }
}
