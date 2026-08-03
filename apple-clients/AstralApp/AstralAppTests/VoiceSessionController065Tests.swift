import AstralCore
import Foundation
import XCTest

@testable import AstralDeep

@MainActor
final class VoiceSessionController065Tests: XCTestCase {
    private let device = "00000000-0000-4000-8000-000000000001"
    private let connection = "00000000-0000-4000-8000-000000000002"
    private let otherConnection = "00000000-0000-4000-8000-000000000012"
    private let voiceSession = "00000000-0000-4000-8000-000000000003"
    private let chat = "00000000-0000-4000-8000-000000000004"
    private let turn = "00000000-0000-4000-8000-000000000005"
    private let clientTurn = "00000000-0000-4000-8000-000000000006"
    private let submission = "00000000-0000-4000-8000-000000000007"
    private let request = "00000000-0000-4000-8000-000000000008"

    #if os(macOS)
        func testMacOSAudioHardwareProbeUsesDefaultRouteWithoutAVCaptureDiscovery() {
            XCTAssertFalse(AppleVoicePermission.hasUsableAudioInputDevice(nil))
            XCTAssertFalse(AppleVoicePermission.hasUsableAudioInputDevice(0))
            XCTAssertTrue(AppleVoicePermission.hasUsableAudioInputDevice(41))
            XCTAssertFalse(AppleVoicePermission.hasUsableAudioOutputDevice(nil))
            XCTAssertFalse(AppleVoicePermission.hasUsableAudioOutputDevice(0))
            XCTAssertTrue(AppleVoicePermission.hasUsableAudioOutputDevice(42))

            let available = AppleVoicePermission.macOSHardwareAvailability(
                AppleVoiceAudioRouteSnapshot(
                    inputDeviceID: 41, inputSampleRateHz: 48_000, inputChannelCount: 1,
                    outputDeviceID: 42, outputSampleRateHz: 48_000, outputChannelCount: 2))
            XCTAssertTrue(available.hasMicrophone)
            XCTAssertTrue(available.hasAudioOutput)

            let missingInput = AppleVoicePermission.macOSHardwareAvailability(
                AppleVoiceAudioRouteSnapshot(
                    inputDeviceID: nil, inputSampleRateHz: nil, inputChannelCount: nil,
                    outputDeviceID: 42, outputSampleRateHz: 48_000, outputChannelCount: 2))
            XCTAssertFalse(missingInput.hasMicrophone)
            XCTAssertTrue(missingInput.hasAudioOutput)
        }

        func testMacOSSelfGeneratedAudioEngineChangeWithSameHardwareKeepsMediaAndLease() async throws {
            let api = FakeVoiceAPI()
            api.startOutcome = .started(restSession(synced: true), grant())
            let media = FakeVoiceMedia()
            let route = AppleVoiceAudioRouteSnapshot(
                inputDeviceID: 11, inputSampleRateHz: 48_000, inputChannelCount: 1,
                outputDeviceID: 22, outputSampleRateHz: 48_000, outputChannelCount: 2)
            let controller = AppleVoiceSessionController(
                api: api, media: media,
                permissionProvider: {
                    .init(
                        hasMicrophone: true, hasAudioOutput: true,
                        microphonePermission: "authorized", fullDuplex: true)
                },
                audioRouteSnapshotProvider: { route },
                uuid: { "00000000-0000-4000-8000-00000000000f" },
                leaseRenewalNanoseconds: 15_000_000)
            install(controller)
            await controller.activate()
            let disconnectsAfterActivation = media.disconnectCount

            controller.audioEngineConfigurationChanged()
            controller.audioEngineConfigurationChanged()
            try await Task.sleep(nanoseconds: 55_000_000)

            XCTAssertEqual(media.disconnectCount, disconnectsAfterActivation)
            XCTAssertEqual(media.interruptionCount, 0)
            XCTAssertTrue(api.refreshIds.isEmpty)
            XCTAssertTrue(controller.mediaConnected)
            XCTAssertEqual(controller.phase, "greeting")
            XCTAssertFalse(
                api.updates.contains { $0["foreground_active"]?.boolValue == false })
            XCTAssertGreaterThanOrEqual(
                api.updates.filter { $0["foreground_active"]?.boolValue == true }.count, 2)
            controller.close()
        }

        func testMacOSHardwareRouteChangeSuspendsThenRecoversExactlyOnce() async throws {
            let api = FakeVoiceAPI()
            api.startOutcome = .started(restSession(synced: true), grant())
            api.refreshOutcomes = [
                .refreshed(restSession(synced: true, revision: 3), grant(revision: 3))
            ]
            let media = FakeVoiceMedia()
            let route = FakeAudioRouteSnapshotSource(
                AppleVoiceAudioRouteSnapshot(
                    inputDeviceID: 11, inputSampleRateHz: 48_000, inputChannelCount: 1,
                    outputDeviceID: 22, outputSampleRateHz: 48_000, outputChannelCount: 2))
            let controller = AppleVoiceSessionController(
                api: api, media: media,
                permissionProvider: {
                    .init(
                        hasMicrophone: true, hasAudioOutput: true,
                        microphonePermission: "authorized", fullDuplex: true)
                },
                audioRouteSnapshotProvider: { route.value },
                uuid: { "00000000-0000-4000-8000-00000000000f" })
            install(controller)
            await controller.activate()

            route.value = AppleVoiceAudioRouteSnapshot(
                inputDeviceID: 11, inputSampleRateHz: 48_000, inputChannelCount: 1,
                outputDeviceID: 23, outputSampleRateHz: 48_000, outputChannelCount: 2)
            controller.audioEngineConfigurationChanged()
            try await Task.sleep(nanoseconds: 40_000_000)

            XCTAssertEqual(
                api.updates.filter { $0["foreground_reason"]?.stringValue == "route_unavailable" }.count,
                1)
            XCTAssertEqual(api.refreshIds, ["00000000-0000-4000-8000-00000000000f"])
            XCTAssertEqual(media.connectCount, 2)
            XCTAssertTrue(controller.mediaConnected)
            XCTAssertEqual(controller.phase, "listening")

            let updatesAfterRecovery = api.updates.count
            let disconnectsAfterRecovery = media.disconnectCount
            let interruptionsAfterRecovery = media.interruptionCount
            controller.audioEngineConfigurationChanged()
            try await Task.sleep(nanoseconds: 20_000_000)
            XCTAssertEqual(api.updates.count, updatesAfterRecovery)
            XCTAssertEqual(api.refreshIds.count, 1)
            XCTAssertEqual(media.connectCount, 2)
            XCTAssertEqual(media.disconnectCount, disconnectsAfterRecovery)
            XCTAssertEqual(media.interruptionCount, interruptionsAfterRecovery)
            controller.close()
        }

        func testMacOSSameDeviceFormatChangeTriggersOneRecovery() async throws {
            let api = FakeVoiceAPI()
            api.startOutcome = .started(restSession(synced: true), grant())
            api.refreshOutcomes = [
                .refreshed(restSession(synced: true, revision: 3), grant(revision: 3))
            ]
            let media = FakeVoiceMedia()
            let route = FakeAudioRouteSnapshotSource(
                AppleVoiceAudioRouteSnapshot(
                    inputDeviceID: 11, inputSampleRateHz: 48_000, inputChannelCount: 1,
                    outputDeviceID: 22, outputSampleRateHz: 48_000, outputChannelCount: 2))
            let controller = AppleVoiceSessionController(
                api: api, media: media,
                permissionProvider: {
                    .init(
                        hasMicrophone: true, hasAudioOutput: true,
                        microphonePermission: "authorized", fullDuplex: true)
                },
                audioRouteSnapshotProvider: { route.value },
                uuid: { "00000000-0000-4000-8000-00000000000f" })
            install(controller)
            await controller.activate()

            route.value = AppleVoiceAudioRouteSnapshot(
                inputDeviceID: 11, inputSampleRateHz: 48_000, inputChannelCount: 1,
                outputDeviceID: 22, outputSampleRateHz: 44_100, outputChannelCount: 2)
            controller.audioEngineConfigurationChanged()
            try await Task.sleep(nanoseconds: 40_000_000)

            XCTAssertEqual(api.refreshIds.count, 1)
            XCTAssertEqual(media.connectCount, 2)
            XCTAssertEqual(controller.phase, "listening")
            controller.close()
        }

        func testMacOSOutputLossWaitsForHardwareRestoreBeforeRecovering() async throws {
            let api = FakeVoiceAPI()
            api.startOutcome = .started(restSession(synced: true), grant())
            api.refreshOutcomes = [
                .refreshed(restSession(synced: true, revision: 3), grant(revision: 3))
            ]
            let media = FakeVoiceMedia()
            let capability = FakeAudioCapabilitySource(
                .init(
                    hasMicrophone: true, hasAudioOutput: true,
                    microphonePermission: "authorized", fullDuplex: true))
            let route = FakeAudioRouteSnapshotSource(
                AppleVoiceAudioRouteSnapshot(
                    inputDeviceID: 11, inputSampleRateHz: 48_000, inputChannelCount: 1,
                    outputDeviceID: 22, outputSampleRateHz: 48_000, outputChannelCount: 2))
            let controller = AppleVoiceSessionController(
                api: api, media: media,
                permissionProvider: { capability.value },
                audioRouteSnapshotProvider: { route.value },
                uuid: { "00000000-0000-4000-8000-00000000000f" })
            install(controller)
            await controller.activate()

            route.value = AppleVoiceAudioRouteSnapshot(
                inputDeviceID: 11, inputSampleRateHz: 48_000, inputChannelCount: 1,
                outputDeviceID: nil, outputSampleRateHz: nil, outputChannelCount: nil)
            capability.value = .init(
                hasMicrophone: true, hasAudioOutput: false,
                microphonePermission: "authorized", fullDuplex: true)
            controller.audioEngineConfigurationChanged()
            try await Task.sleep(nanoseconds: 25_000_000)

            XCTAssertFalse(controller.mediaConnected)
            XCTAssertEqual(controller.reason, "route_unavailable")
            XCTAssertTrue(api.refreshIds.isEmpty)

            route.value = AppleVoiceAudioRouteSnapshot(
                inputDeviceID: 11, inputSampleRateHz: 48_000, inputChannelCount: 1,
                outputDeviceID: 23, outputSampleRateHz: 48_000, outputChannelCount: 2)
            capability.value = .init(
                hasMicrophone: true, hasAudioOutput: true,
                microphonePermission: "authorized", fullDuplex: true)
            controller.audioEngineConfigurationChanged()
            try await Task.sleep(nanoseconds: 45_000_000)

            XCTAssertEqual(api.refreshIds.count, 1)
            XCTAssertEqual(media.connectCount, 2)
            XCTAssertTrue(controller.mediaConnected)
            XCTAssertEqual(controller.phase, "listening")
            controller.close()
        }

        func testMacOSActivationRefreshesHardwareBaselineBeforeMediaConnect() async throws {
            let api = FakeVoiceAPI()
            api.startOutcome = .started(restSession(synced: true), grant())
            let media = FakeVoiceMedia()
            let route = FakeAudioRouteSnapshotSource(
                AppleVoiceAudioRouteSnapshot(
                    inputDeviceID: 11, inputSampleRateHz: 48_000, inputChannelCount: 1,
                    outputDeviceID: 22, outputSampleRateHz: 48_000, outputChannelCount: 2))
            let controller = AppleVoiceSessionController(
                api: api, media: media,
                permissionProvider: {
                    .init(
                        hasMicrophone: true, hasAudioOutput: true,
                        microphonePermission: "authorized", fullDuplex: true)
                },
                audioRouteSnapshotProvider: { route.value },
                uuid: { "00000000-0000-4000-8000-00000000000f" })
            install(controller)
            route.value = AppleVoiceAudioRouteSnapshot(
                inputDeviceID: 11, inputSampleRateHz: 48_000, inputChannelCount: 1,
                outputDeviceID: 23, outputSampleRateHz: 48_000, outputChannelCount: 2)

            await controller.activate()
            let disconnectsAfterActivation = media.disconnectCount
            controller.audioEngineConfigurationChanged()
            try await Task.sleep(nanoseconds: 20_000_000)

            XCTAssertEqual(media.disconnectCount, disconnectsAfterActivation)
            XCTAssertTrue(api.refreshIds.isEmpty)
            XCTAssertTrue(controller.mediaConnected)
            controller.close()
        }
    #endif

    func testPermissionDenialFailsBeforeRESTOrMediaAndLeavesTypedChatAvailable() async {
        let api = FakeVoiceAPI()
        let media = FakeVoiceMedia()
        let controller = makeController(
            api: api, media: media,
            capability: .init(
                hasMicrophone: true, hasAudioOutput: true,
                microphonePermission: "denied", fullDuplex: true))
        install(controller)

        await controller.activate()

        XCTAssertEqual(api.startCount, 0)
        XCTAssertEqual(media.connectCount, 0)
        XCTAssertEqual(controller.reason, "permission_denied")
        XCTAssertEqual(controller.phase, "error")
    }

    func testAppModelRoutesEveryVoiceWireOnCurrentConnectionWithoutQueueAlert() throws {
        let model = AppModel()
        model.signedIn = true
        model.connected = true
        XCTAssertTrue(model.beginConversationConnection(connection))
        var liveOnly: [String] = []
        model.currentConnectionVoiceSendOverride = { liveOnly.append($0) }

        let newChat = Outbound.correlatedVoiceNewChat(
            connectionGeneration: connection,
            submissionId: "00000000-0000-4000-8000-00000000000d",
            requestGeneration: "00000000-0000-4000-8000-00000000000e")
        XCTAssertTrue(model.sendVoiceWire(newChat))

        let transcript = try XCTUnwrap(
            VoiceTranscript(frame: try XCTUnwrap(InboundFrame.parse(finalTranscript()))))
        let voiceChat = Outbound.voiceChatMessage(
            transcript: transcript, connectionGeneration: connection)
        XCTAssertTrue(model.sendVoiceWire(voiceChat))
        XCTAssertTrue(model.sendVoiceWire(voiceChat), "an exact controller retry remains live-only")

        let playout =
            """
            {"type":"voice_playout_event","schema_version":"1","device_id":"\(device)","connection_generation":"\(connection)","session_id":"\(voiceSession)","generation":1,"media_grant_revision":2,"announcement_id":"00000000-0000-4000-8000-000000000009","announcement_sequence":1,"turn_id":"\(turn)","kind":"acknowledgement","quantum_role":"single","quantum_index":0,"phase":"started","client_sequence":0,"observed_at":"2099-07-31T12:00:00Z"}
            """
        XCTAssertTrue(model.sendVoiceWire(playout))

        XCTAssertEqual(liveOnly.count, 4)
        XCTAssertEqual(liveOnly.filter { $0 == voiceChat }.count, 2)
        XCTAssertEqual(model.localOperationSubmissions.count, 1)
        XCTAssertEqual(
            model.transientTurns.filter { $0.id == "pending-voice-\(submission)" }.count,
            1,
            "controller retries must not duplicate the optimistic user turn")
        XCTAssertNil(model.errorBanner)
    }

    func testActivationConnectsLiveKitAndDefersCaptureUntilContextSync() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: false), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)

        await controller.activate()
        XCTAssertEqual(api.startCount, 1)
        XCTAssertEqual(media.connectCount, 1)
        XCTAssertTrue(media.microphoneValues.isEmpty)
        XCTAssertEqual(controller.reason, "chat_context_unavailable")

        controller.consume(frame(sessionState(synced: true, microphone: true)))
        await Task.yield()
        XCTAssertEqual(media.microphoneValues.last, true)
    }

    func testFinalTranscriptRetriesExactWireUntilFullyCorrelatedAcknowledgement() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        var sent: [String] = []
        let controller = makeController(api: api, media: media, retryNanoseconds: 20_000_000)
        controller.setFrameSender {
            sent.append($0)
            return true
        }
        install(controller, replaceSender: false)
        await controller.activate()

        media.emit(
            .data(
                topic: voiceTranscriptTopic, participantIdentity: "voice-worker-01",
                payload: Data(finalTranscript().utf8)))
        try await Task.sleep(nanoseconds: 70_000_000)

        let messages = sent.filter { InboundFrame.parse($0)?.payload["action"]?.stringValue == "chat_message" }
        XCTAssertGreaterThanOrEqual(messages.count, 2)
        XCTAssertEqual(Set(messages).count, 1, "retries must reuse the exact serialized frame")
        XCTAssertEqual(controller.awaitingAcceptance, 1)

        controller.consume(frame(ack(connection: "00000000-0000-4000-8000-00000000000b")))
        XCTAssertEqual(controller.awaitingAcceptance, 1)
        controller.consume(frame(ack(connection: connection)))
        XCTAssertEqual(controller.awaitingAcceptance, 0)
        XCTAssertEqual(controller.phase, "processing")
        XCTAssertEqual(controller.message, "On it!")
    }

    func testTerminalRequestNoticeSurvivesChurnAndClearsForANewerActiveTurn() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        controller.consume(
            frame(
                try voiceTurn(
                    state: "failed", message: "The safe server explanation remains visible.",
                    occurredAt: "2099-07-31T12:05:00Z")))
        let failedNotice = try XCTUnwrap(controller.terminalNotice)
        XCTAssertEqual(failedNotice.title, "Request did not complete.")
        XCTAssertEqual(
            failedNotice.serverMessage, "The safe server explanation remains visible.")

        controller.consume(frame(sessionState(synced: true, microphone: true)))
        controller.consume(frame(activeComposer(revision: 20)))
        XCTAssertEqual(controller.terminalNotice, failedNotice)

        controller.consume(frame(try voiceTurn(state: "processing", sequence: 2)))
        XCTAssertEqual(
            controller.terminalNotice, failedNotice,
            "same-turn lifecycle churn must not erase its failure notice")

        let nextTurn = "00000000-0000-4000-8000-000000000015"
        controller.consume(
            frame(
                try voiceTurn(
                    state: "accepted", turnId: nextTurn, sequence: 1,
                    occurredAt: "2099-07-31T12:04:59Z")))
        XCTAssertEqual(
            controller.terminalNotice, failedNotice,
            "an older different-turn frame cannot erase a newer notice")

        controller.consume(
            frame(
                try voiceTurn(
                    state: "accepted", turnId: nextTurn, sequence: 2,
                    occurredAt: "2099-07-31T12:05:01Z")))
        XCTAssertNil(controller.terminalNotice)

        controller.consume(
            frame(
                try voiceTurn(
                    state: "refused", turnId: nextTurn, sequence: 3,
                    message: "This request needs a different permission.",
                    occurredAt: "2099-07-31T12:05:02Z")))
        XCTAssertEqual(controller.terminalNotice?.title, "Request did not start.")
        XCTAssertEqual(
            controller.terminalNotice?.serverMessage,
            "This request needs a different permission.")

        let successfulTurn = "00000000-0000-4000-8000-000000000025"
        controller.consume(
            frame(
                try voiceTurn(
                    state: "succeeded", turnId: successfulTurn, sequence: 1,
                    occurredAt: "2099-07-31T12:05:03Z")))
        XCTAssertNil(controller.terminalNotice)

        controller.consume(
            frame(
                try voiceTurn(
                    state: "cancelled", turnId: successfulTurn, sequence: 2,
                    message: "The request was cancelled.",
                    occurredAt: "2099-07-31T12:05:04Z")))
        XCTAssertNotNil(controller.terminalNotice)
        controller.close()
        XCTAssertNil(controller.terminalNotice, "an explicit reset clears the old notice")
    }

    func testCorrelatedSubmissionRejectionStopsRetryAndShowsExplicitRetryNotice() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        var sent: [String] = []
        let controller = makeController(
            api: api, media: media, retryNanoseconds: 5_000_000)
        controller.setFrameSender {
            sent.append($0)
            return true
        }
        install(controller, replaceSender: false)
        await controller.activate()

        media.emit(
            .data(
                topic: voiceTranscriptTopic, participantIdentity: "voice-worker-01",
                payload: Data(finalTranscript().utf8)))
        XCTAssertEqual(controller.awaitingAcceptance, 1)

        controller.consume(
            frame(
                submissionRejection(
                    message: "This stale socket must be ignored.",
                    retryPolicy: "explicit_user_retry"
                ).replacingOccurrences(
                    of: "\"connection_generation\":\"\(connection)\"",
                    with: "\"connection_generation\":\"\(otherConnection)\"")))
        XCTAssertEqual(controller.awaitingAcceptance, 1)
        XCTAssertNil(controller.terminalNotice)

        controller.consume(
            frame(
                submissionRejection(
                    message: "Capacity is temporarily full.",
                    retryPolicy: "explicit_user_retry")))

        XCTAssertEqual(controller.awaitingAcceptance, 0)
        XCTAssertEqual(controller.terminalNotice?.title, "Request did not start.")
        XCTAssertEqual(
            controller.terminalNotice?.serverMessage, "Capacity is temporarily full.")
        XCTAssertEqual(
            controller.terminalNotice?.guidance,
            "Please say it again when you are ready.")
        let settledSendCount = sent.count
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(
            sent.count, settledSendCount,
            "a terminal rejection cancels exact-wire retry and never auto-replays")
        controller.close()
    }

    func testPendingFinalReserializesForNewUIConnectionWithoutChangingProofIdentity() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        var sent: [String] = []
        let controller = makeController(
            api: api, media: media, retryNanoseconds: 20_000_000)
        controller.setFrameSender {
            sent.append($0)
            return true
        }
        install(controller, replaceSender: false)
        await controller.activate()

        media.emit(
            .data(
                topic: voiceTranscriptTopic, participantIdentity: "voice-worker-01",
                payload: Data(finalTranscript().utf8)))
        try await Task.sleep(nanoseconds: 30_000_000)

        let original = try XCTUnwrap(
            sent.compactMap(InboundFrame.parse).first {
                $0.payload["action"]?.stringValue == "chat_message"
                    && $0.payload["connection_generation"]?.stringValue == connection
            })
        let reconnectBoundary = sent.count
        controller.installUIConnection(
            token: "access-token", serverBase: URL(string: "https://example.test/")!,
            deviceId: device, deviceKind: "ios", connectionGeneration: otherConnection,
            visibleChatId: chat)
        try await Task.sleep(nanoseconds: 70_000_000)

        let reconnectMessages = sent.dropFirst(reconnectBoundary)
            .compactMap(InboundFrame.parse)
            .filter { $0.payload["action"]?.stringValue == "chat_message" }
        XCTAssertFalse(reconnectMessages.isEmpty)
        XCTAssertTrue(
            reconnectMessages.allSatisfy {
                $0.payload["connection_generation"]?.stringValue == otherConnection
                    && $0.payload["payload"]?["connection_generation"]?.stringValue
                        == otherConnection
            },
            "a pending final must never retry the stale UI connection generation")

        let retried = try XCTUnwrap(reconnectMessages.first)
        for key in ["submission_id", "request_generation", "session_id"] {
            XCTAssertEqual(retried.payload[key], original.payload[key])
        }
        let originalPayload = try XCTUnwrap(original.payload["payload"])
        let retriedPayload = try XCTUnwrap(retried.payload["payload"])
        XCTAssertEqual(retriedPayload["message"], originalPayload["message"])
        XCTAssertEqual(retriedPayload["chat_id"], originalPayload["chat_id"])
        XCTAssertEqual(retriedPayload["submission_id"], originalPayload["submission_id"])
        XCTAssertEqual(
            retriedPayload["request_generation"], originalPayload["request_generation"])
        XCTAssertEqual(retriedPayload["voice_origin"], originalPayload["voice_origin"])

        controller.consume(frame(ack(connection: connection)))
        XCTAssertEqual(
            controller.awaitingAcceptance, 1,
            "an acknowledgement from the replaced socket must not settle the reframed final")
        controller.consume(frame(ack(connection: otherConnection)))
        XCTAssertEqual(controller.awaitingAcceptance, 0)
    }

    func testAnnouncementIsAuthorizedBeforePlayoutAndReportsContentFreeEnvelope() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        var sent: [String] = []
        let controller = makeController(api: api, media: media)
        controller.setFrameSender {
            sent.append($0)
            return true
        }
        install(controller, replaceSender: false)
        await controller.activate()

        let manifest = announcement()
        media.emit(
            .data(
                topic: voiceAnnouncementTopic, participantIdentity: "voice-worker-01",
                payload: Data(manifest.utf8)))
        let authorized = try XCTUnwrap(media.authorized.first)
        XCTAssertEqual(authorized.trackSid, "TR_audio_001")
        media.emit(.playout(authorized, phase: "started"))

        let playout = try XCTUnwrap(sent.compactMap(InboundFrame.parse).first { $0.name == "voice_playout_event" })
        XCTAssertNil(playout.payload["text"])
        XCTAssertNil(playout.payload["message"])
        XCTAssertEqual(playout.payload["announcement_id"]?.stringValue, authorized.announcementId)
    }

    func testManifestMatcherSerializesExactPairsWhileSpeechIsActive() throws {
        var matcher = playoutMatcher()
        let first = try announcementValue(sequence: 1, sid: "TR_audio_001")
        let second = try announcementValue(sequence: 2, sid: "TR_audio_002")
        let firstTrack = publishedTrack(sid: "TR_audio_001")
        let secondTrack = publishedTrack(sid: "TR_audio_002")

        XCTAssertTrue(matcher.enqueue(first))
        XCTAssertTrue(matcher.remember(firstTrack))
        XCTAssertEqual(matcher.next(), .start(first, firstTrack))
        XCTAssertTrue(matcher.enqueue(second), "active speech must not reject the next manifest")
        XCTAssertTrue(matcher.remember(secondTrack))
        XCTAssertEqual(matcher.next(), .none, "only one bounded renderer may be active")

        XCTAssertEqual(matcher.finish(announcementId: first.announcementId)?.manifest, first)
        XCTAssertEqual(matcher.next(), .start(second, secondTrack))
    }

    func testManifestMatcherRequiresExactWorkerSidAndName() throws {
        var matcher = playoutMatcher()
        let manifest = try announcementValue(sequence: 1, sid: "TR_audio_001")
        XCTAssertTrue(matcher.enqueue(manifest))
        XCTAssertFalse(
            matcher.remember(
                .init(
                    sid: "TR_audio_001", name: "voice-result-opening",
                    workerIdentity: "unexpected-worker", isAudio: true)))

        let wrongName = AppleVoicePublishedTrack(
            sid: "TR_audio_001", name: "voice-other",
            workerIdentity: "voice-worker-01", isAudio: true)
        XCTAssertTrue(matcher.remember(wrongName))
        XCTAssertEqual(matcher.next(), .drop(manifest, wrongName))
    }

    func testManifestMatcherDoesNotSkipAnEarlierUnmatchedQuantum() throws {
        var matcher = playoutMatcher()
        let first = try announcementValue(sequence: 1, sid: "TR_audio_001")
        let second = try announcementValue(sequence: 2, sid: "TR_audio_002")
        let secondTrack = publishedTrack(sid: "TR_audio_002")

        XCTAssertTrue(matcher.enqueue(first))
        XCTAssertTrue(matcher.enqueue(second))
        XCTAssertTrue(matcher.remember(secondTrack))
        XCTAssertEqual(
            matcher.next(), .none,
            "a later exact pair cannot overtake an earlier ordered manifest")
        XCTAssertEqual(matcher.expireUnmatched(sid: "TR_audio_001")?.manifest, first)
        XCTAssertEqual(matcher.next(), .start(second, secondTrack))
    }

    func testManifestMatcherBoundsQueueExpiresHalfPairsAndNeverReplaysAfterClear() throws {
        var matcher = playoutMatcher()
        for sequence in 1...AppleVoicePlayoutMatcher.maximumPendingAnnouncements {
            XCTAssertTrue(
                matcher.enqueue(
                    try announcementValue(
                        sequence: sequence, sid: "TR_audio_\(sequence)")))
        }
        XCTAssertFalse(
            matcher.enqueue(
                try announcementValue(sequence: 9, sid: "TR_audio_9")))

        let cleared = matcher.clear()
        XCTAssertEqual(cleared.pending.count, AppleVoicePlayoutMatcher.maximumPendingAnnouncements)
        XCTAssertEqual(matcher.pendingCount, 0)
        XCTAssertFalse(
            matcher.enqueue(
                try announcementValue(sequence: 8, sid: "TR_audio_replay")),
            "clearing local media must retain the sequence fence")
        let next = try announcementValue(sequence: 9, sid: "TR_audio_9")
        XCTAssertTrue(matcher.enqueue(next))
        XCTAssertEqual(matcher.expireUnmatched(sid: "TR_audio_9")?.manifest, next)

        let paired = try announcementValue(sequence: 10, sid: "TR_audio_10")
        XCTAssertTrue(matcher.enqueue(paired))
        XCTAssertTrue(matcher.remember(publishedTrack(sid: "TR_audio_10")))
        XCTAssertNil(
            matcher.expireUnmatched(sid: "TR_audio_10"),
            "an exact pair waiting behind speech has already met the one-second match gate")
    }

    func testSampleBudgetIsExactAtTwentyFourKilohertzEquivalent() {
        var budget = AppleVoiceSampleBudget(targetSamples: 36_000)
        XCTAssertEqual(
            budget.accept(
                sampleRateHz: 48_000, channelCount: 2, inputFrames: 48_000,
                outputFrames: 24_000),
            24_000)
        XCTAssertEqual(
            budget.accept(
                sampleRateHz: 48_000, channelCount: 2, inputFrames: 48_000,
                outputFrames: 24_000),
            12_000)
        XCTAssertTrue(budget.complete)
        XCTAssertEqual(
            budget.accept(
                sampleRateHz: 24_000, channelCount: 1, inputFrames: 480,
                outputFrames: 480),
            0)

        var invalid = AppleVoiceSampleBudget(targetSamples: 480)
        XCTAssertNil(
            invalid.accept(
                sampleRateHz: 44_100, channelCount: 1, inputFrames: 480,
                outputFrames: 480))
        XCTAssertNil(
            invalid.accept(
                sampleRateHz: 48_000, channelCount: 1, inputFrames: 481,
                outputFrames: 240))
        XCTAssertNil(
            invalid.accept(
                sampleRateHz: 24_000, channelCount: 3, inputFrames: 480,
                outputFrames: 480))
    }

    func testRejectedOrDroppedManifestSurfacesSpeechErrorWithoutFakePlayout() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        media.authorizationResult = false
        var sent: [String] = []
        let controller = makeController(api: api, media: media)
        controller.setFrameSender {
            sent.append($0)
            return true
        }
        install(controller, replaceSender: false)
        await controller.activate()
        controller.consume(
            frame(
                try voiceTurn(
                    state: "processing", sequence: 1,
                    occurredAt: "2099-07-31T12:05:00Z")))

        media.emit(
            .data(
                topic: voiceAnnouncementTopic, participantIdentity: "voice-worker-01",
                payload: Data(announcement().utf8)))
        XCTAssertEqual(controller.reason, "speech_error")
        XCTAssertEqual(controller.terminalNotice?.kind, .speechFailure)
        XCTAssertEqual(controller.terminalNotice?.title, "Speech playback failed.")
        XCTAssertTrue(
            controller.terminalNotice?.displayText.contains(
                "text result is still available") == true)
        XCTAssertFalse(
            controller.terminalNotice?.displayText.contains("Request did not complete") == true)
        XCTAssertTrue(sent.compactMap(InboundFrame.parse).allSatisfy { $0.name != "voice_playout_event" })

        let dropped = try XCTUnwrap(media.authorized.first)
        media.emit(.announcementDropped(dropped))
        XCTAssertEqual(controller.reason, "speech_error")
        XCTAssertEqual(
            controller.terminalNotice?.serverMessage,
            "Assistant audio was unavailable. Typed chat is still available.")
        XCTAssertTrue(sent.compactMap(InboundFrame.parse).allSatisfy { $0.name != "voice_playout_event" })

        let sameTurnNotice = controller.terminalNotice
        controller.consume(
            frame(
                try voiceTurn(
                    state: "succeeded", sequence: 2,
                    message: "The text result is available.",
                    occurredAt: "2099-07-31T12:05:01Z",
                    speechOutcome: "source_finished")))
        XCTAssertEqual(controller.terminalNotice, sameTurnNotice)
        controller.close()
    }

    func testDelayedOlderResultDropCannotRelabelTheCurrentTurn() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        controller.consume(
            frame(
                try voiceTurn(
                    state: "processing", sequence: 1,
                    occurredAt: "2099-07-31T12:05:00Z")))
        media.emit(
            .data(
                topic: voiceAnnouncementTopic, participantIdentity: "voice-worker-01",
                payload: Data(announcement().utf8)))
        let oldAnnouncement = try XCTUnwrap(media.authorized.first)

        let newerTurn = "00000000-0000-4000-8000-000000000025"
        controller.consume(
            frame(
                try voiceTurn(
                    state: "succeeded", turnId: newerTurn, sequence: 1,
                    message: "The newer text result is available.",
                    occurredAt: "2099-07-31T12:05:01Z",
                    speechOutcome: "source_finished")))
        media.emit(.announcementDropped(oldAnnouncement))

        XCTAssertNil(controller.terminalNotice)
        XCTAssertNotEqual(controller.reason, "speech_error")
        controller.close()
    }

    func testGreetingMediaFailureDoesNotClaimAResultExists() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        media.authorizationResult = false
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        media.emit(
            .data(
                topic: voiceAnnouncementTopic, participantIdentity: "voice-worker-01",
                payload: Data(greetingAnnouncement().utf8)))
        XCTAssertEqual(controller.reason, "speech_error")
        XCTAssertNil(controller.terminalNotice)
        XCTAssertFalse(
            (controller.message ?? "").localizedCaseInsensitiveContains("text result"))

        let greeting = try XCTUnwrap(media.authorized.first)
        media.emit(.announcementDropped(greeting))
        XCTAssertEqual(controller.reason, "speech_error")
        XCTAssertNil(controller.terminalNotice)
        XCTAssertFalse(
            (controller.message ?? "").localizedCaseInsensitiveContains("text result"))
        controller.close()
    }

    func testServerSpeechErrorAfterTextSuccessKeepsTextAvailableNotice() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let controller = makeController(api: api, media: FakeVoiceMedia())
        install(controller)
        await controller.activate()

        controller.consume(
            frame(
                try voiceTurn(
                    state: "succeeded", sequence: 1,
                    occurredAt: "2099-07-31T12:05:00Z")))
        controller.consume(
            frame(
                terminalSessionState(
                    state: "error", reason: "speech_error",
                    message: "The result audio could not be delivered.")))

        let notice = try XCTUnwrap(controller.terminalNotice)
        XCTAssertEqual(notice.kind, .speechFailure)
        XCTAssertEqual(notice.turnId, turn)
        XCTAssertEqual(notice.occurredAt, "2099-07-31T12:05:00Z")
        XCTAssertEqual(notice.serverMessage, "The result audio could not be delivered.")
        XCTAssertTrue(notice.displayText.contains("text result may still be available"))
        XCTAssertFalse(notice.displayText.localizedCaseInsensitiveContains("request failed"))
        controller.close()
    }

    func testExactTurnSpeechFailureKeepsCommittedTextNoticeAndRejectsOlderFailure() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let controller = makeController(api: api, media: FakeVoiceMedia())
        install(controller)
        await controller.activate()

        controller.consume(
            frame(
                try voiceTurn(
                    state: "succeeded", sequence: 1,
                    message: "The result audio could not be delivered.",
                    occurredAt: "2099-07-31T12:05:00Z", speechOutcome: "failed")))

        let failedSpeech = try XCTUnwrap(controller.terminalNotice)
        XCTAssertEqual(failedSpeech.kind, .speechFailure)
        XCTAssertEqual(failedSpeech.turnId, turn)
        XCTAssertEqual(failedSpeech.occurredAt, "2099-07-31T12:05:00Z")
        XCTAssertTrue(failedSpeech.displayText.contains("text result is still available"))
        XCTAssertFalse(failedSpeech.displayText.localizedCaseInsensitiveContains("request failed"))
        XCTAssertEqual(controller.phase, "error")
        XCTAssertEqual(controller.reason, "speech_error")

        let newerTurn = "00000000-0000-4000-8000-000000000025"
        controller.consume(
            frame(
                try voiceTurn(
                    state: "succeeded", turnId: newerTurn, sequence: 1,
                    message: "Newer result audio was unavailable.",
                    occurredAt: "2099-07-31T12:05:01Z", speechOutcome: "failed")))
        let newerNotice = try XCTUnwrap(controller.terminalNotice)
        XCTAssertEqual(newerNotice.turnId, newerTurn)

        controller.consume(
            frame(
                try voiceTurn(
                    state: "succeeded", sequence: 2,
                    message: "This older audio failure must stay hidden.",
                    occurredAt: "2099-07-31T12:05:00Z", speechOutcome: "failed")))
        XCTAssertEqual(controller.terminalNotice, newerNotice)
        XCTAssertEqual(controller.message, newerNotice.displayText)
        controller.close()
    }

    func testNonFailureSpeechOutcomesRemainNormalSuccessfulResults() async throws {
        for outcome in [nil, "source_finished", "suppressed"] as [String?] {
            let api = FakeVoiceAPI()
            api.startOutcome = .started(restSession(synced: true), grant())
            let controller = makeController(api: api, media: FakeVoiceMedia())
            install(controller)
            await controller.activate()

            controller.consume(
                frame(
                    try voiceTurn(
                        state: "succeeded", sequence: 1,
                        message: "The result is ready.", speechOutcome: outcome)))

            XCTAssertNil(controller.terminalNotice)
            XCTAssertEqual(controller.phase, "speaking_result")
            XCTAssertEqual(controller.reason, "ready")
            controller.close()
        }
    }

    func testBackgroundSuspendsCaptureWithoutCancellingAcceptedWork() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        controller.sceneBecameInactive()
        await Task.yield()

        XCTAssertEqual(controller.phase, "suspended")
        XCTAssertEqual(media.microphoneValues.last, false)
        XCTAssertEqual(api.lastUpdate?["foreground_active"]?.boolValue, false)
        XCTAssertEqual(api.lastUpdate?["foreground_reason"]?.stringValue, "backgrounded")
        XCTAssertGreaterThanOrEqual(media.disconnectCount, 1)
        XCTAssertEqual(api.endCount, 0)
    }

    func testPermissionCompletionWhileInactiveDefersOneInitialStartWithoutGrantRotation() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)

        controller.sceneBecameInactive()
        await controller.activate()

        XCTAssertEqual(api.startCount, 0)
        XCTAssertTrue(api.refreshIds.isEmpty)
        XCTAssertEqual(media.connectCount, 0)

        controller.sceneBecameActive()
        await Task.yield()
        await Task.yield()

        XCTAssertEqual(api.startCount, 1)
        XCTAssertTrue(api.refreshIds.isEmpty)
        XCTAssertEqual(media.connectCount, 1)

        controller.sceneBecameActive()
        await Task.yield()
        XCTAssertEqual(api.startCount, 1)
        XCTAssertTrue(api.refreshIds.isEmpty)
        XCTAssertEqual(media.connectCount, 1)
    }

    func testActiveSceneWithoutSuspensionDoesNotRecoverConnectedMedia() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        controller.sceneBecameActive()
        await Task.yield()
        await Task.yield()

        XCTAssertTrue(api.refreshIds.isEmpty)
        XCTAssertEqual(media.connectCount, 1)
        XCTAssertEqual(controller.phase, "greeting")
    }

    func testRecoveryCannotOverlapInitialMediaConnect() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        media.holdNextConnect = true
        let controller = makeController(api: api, media: media)
        install(controller)

        let activation = Task { await controller.activate() }
        for _ in 0..<20 where media.connectCount == 0 { await Task.yield() }
        XCTAssertEqual(media.connectCount, 1)

        controller.sceneBecameActive()
        await Task.yield()
        await Task.yield()

        XCTAssertTrue(api.refreshIds.isEmpty)
        XCTAssertEqual(media.connectCount, 1)
        media.releaseConnect()
        await activation.value
        XCTAssertEqual(controller.phase, "greeting")
    }

    func testForegroundReturnRecoversOnlyAfterGenuineLiveSessionSuspension() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        api.refreshOutcomes = [
            .refreshed(restSession(synced: true, revision: 3), grant(revision: 3))
        ]
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        controller.sceneBecameInactive()
        await Task.yield()
        controller.sceneBecameActive()
        try await Task.sleep(nanoseconds: 30_000_000)

        XCTAssertEqual(api.refreshIds, ["00000000-0000-4000-8000-00000000000f"])
        XCTAssertEqual(media.connectCount, 2)
        XCTAssertEqual(controller.phase, "listening")
    }

    func testTerminalLeaseRefreshClearsRenewingAndStartsFreshWithoutSignIn() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        api.updateShouldFail = true
        api.refreshOutcomes = [.failed("session_ended", nil)]
        let media = FakeVoiceMedia()
        let controller = makeController(
            api: api, media: media,
            retryNanoseconds: 5_000_000,
            leaseRenewalNanoseconds: 5_000_000)
        install(controller)
        await controller.activate()

        for _ in 0..<80 where api.refreshIds.isEmpty || controller.phase == "reconnecting" {
            try await Task.sleep(nanoseconds: 2_000_000)
        }

        XCTAssertEqual(api.refreshIds.count, 1)
        XCTAssertEqual(controller.phase, "error")
        XCTAssertEqual(controller.reason, "media_error")
        XCTAssertEqual(
            controller.message,
            "Voice media ended. Start a new voice conversation or keep typing.")
        XCTAssertNotEqual(controller.message, "Renewing the voice connection…")
        XCTAssertFalse(controller.mediaConnected)

        api.updateShouldFail = false
        api.startOutcome = .started(restSession(synced: true), grant())
        await controller.activate()

        XCTAssertEqual(api.startCount, 2)
        XCTAssertEqual(media.connectCount, 2)
        XCTAssertEqual(controller.phase, "greeting")
        controller.close()
    }

    func testTransientRefreshRetriesAndRecoversInsteadOfEndingSession() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        api.refreshOutcomes = [
            .failed("network_interrupted", nil),
            .refreshed(restSession(synced: true, revision: 3), grant(revision: 3)),
        ]
        let media = FakeVoiceMedia()
        let controller = makeController(
            api: api, media: media, retryNanoseconds: 5_000_000)
        install(controller)
        await controller.activate()

        media.emit(.failed)
        for _ in 0..<80 where api.refreshIds.count < 2 || !controller.mediaConnected {
            try await Task.sleep(nanoseconds: 2_000_000)
        }

        XCTAssertEqual(api.refreshIds.count, 2)
        XCTAssertTrue(controller.mediaConnected)
        XCTAssertEqual(controller.phase, "listening")
        XCTAssertEqual(controller.reason, "ready")
        controller.close()
    }

    func testAudioInterruptionBlocksRecoveryUntilItEndsThenRefreshesBeforeRejoin() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        api.refreshOutcomes = [
            .refreshed(restSession(synced: true, revision: 3), grant(revision: 3))
        ]
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        controller.audioSessionInterrupted()
        media.emit(.failed)
        try await Task.sleep(nanoseconds: 20_000_000)

        XCTAssertTrue(api.refreshIds.isEmpty)
        XCTAssertEqual(controller.phase, "reconnecting")
        XCTAssertGreaterThanOrEqual(media.disconnectCount, 1)

        controller.audioSessionInterruptionEnded()
        try await Task.sleep(nanoseconds: 30_000_000)

        XCTAssertEqual(api.refreshIds, ["00000000-0000-4000-8000-00000000000f"])
        XCTAssertEqual(media.connectCount, 2)
        XCTAssertEqual(controller.phase, "listening")
        XCTAssertEqual(media.microphoneValues.last, true)
    }

    func testMediaFailureDuringInFlightRecoveryCannotBeClearedByOlderConnect() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        api.refreshOutcomes = [
            .refreshed(restSession(synced: true, revision: 3), grant(revision: 3)),
            .refreshed(restSession(synced: true, revision: 4), grant(revision: 4)),
        ]
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        controller.sceneBecameInactive()
        await Task.yield()
        media.holdNextConnect = true
        controller.sceneBecameActive()
        for _ in 0..<30 where media.connectCount < 2 { await Task.yield() }
        XCTAssertEqual(media.connectCount, 2)

        media.emit(.failed)
        media.releaseConnect()
        try await Task.sleep(nanoseconds: 70_000_000)

        XCTAssertEqual(api.refreshIds.count, 2)
        XCTAssertEqual(media.connectCount, 3)
        XCTAssertTrue(controller.mediaConnected)
        XCTAssertEqual(controller.phase, "listening")
        controller.close()
    }

    func testGreetingStopAndIdleExpiryRemainServerOwned() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()
        XCTAssertEqual(controller.phase, "greeting")

        await controller.perform(.stopSpeech)
        XCTAssertEqual(api.stopCount, 1)
        XCTAssertEqual(media.interruptionCount, 1)
        XCTAssertEqual(controller.phase, "listening")

        controller.consume(
            frame(
                """
                {"type":"voice_session_state","schema_version":"1","session_id":"\(voiceSession)","connection_generation":"\(connection)","generation":1,"media_grant_revision":2,"visible_chat_id":"\(chat)","chat_context_revision":3,"applied_chat_context_revision":3,"chat_context_synced":true,"state":"ended","speech_muted":false,"microphone_enabled":false,"foreground_active":false,"reason":"idle_expired","occurred_at":"2099-07-31T12:00:00Z"}
                """))
        XCTAssertEqual(controller.phase, "ended")
        XCTAssertEqual(controller.reason, "idle_expired")
        XCTAssertEqual(api.endCount, 0, "idle expiry is authoritative and must not issue a second end")
    }

    func testTakeoverRequiresExplicitSecondAction() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .takeoverRequired(
            .init(
                sessionId: voiceSession, deviceKind: "android", deviceLabel: "Pixel",
                generation: 1, mediaGrantRevision: 2), nil)
        api.takeoverOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)

        await controller.activate()
        XCTAssertTrue(controller.takeoverAvailable)
        XCTAssertEqual(api.takeoverCount, 0)
        await controller.takeover()
        XCTAssertEqual(api.takeoverCount, 1)
        XCTAssertEqual(media.connectCount, 1)
    }

    func testTakeoverPermissionCompletionWhileInactiveDefersExactlyOneTakeover() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .takeoverRequired(
            .init(
                sessionId: voiceSession, deviceKind: "android", deviceLabel: "Pixel",
                generation: 1, mediaGrantRevision: 2), nil)
        api.takeoverOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        controller.sceneBecameInactive()
        await controller.takeover()
        XCTAssertEqual(api.takeoverCount, 0)

        controller.sceneBecameActive()
        await Task.yield()
        await Task.yield()

        XCTAssertEqual(api.takeoverCount, 1)
        XCTAssertTrue(api.refreshIds.isEmpty)
        XCTAssertEqual(media.connectCount, 1)
    }

    func testForegroundLeaseRenewalDoesNotCountAsInteractionAndStopsInBackground() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(
            api: api, media: media, leaseRenewalNanoseconds: 15_000_000)
        install(controller)
        await controller.activate()

        try await Task.sleep(nanoseconds: 55_000_000)
        let foregroundRenewals = api.updates.filter {
            $0["foreground_active"]?.boolValue == true
        }
        XCTAssertGreaterThanOrEqual(foregroundRenewals.count, 2)
        XCTAssertTrue(foregroundRenewals.allSatisfy { $0["interaction"] == nil })

        controller.sceneBecameInactive()
        await Task.yield()
        let renewalCountAtSuspend = api.updates.filter {
            $0["foreground_active"]?.boolValue == true
        }.count
        try await Task.sleep(nanoseconds: 45_000_000)
        XCTAssertEqual(
            api.updates.filter { $0["foreground_active"]?.boolValue == true }.count,
            renewalCountAtSuspend,
            "backgrounded clients must not retain the session lease")
        XCTAssertEqual(api.updates.last?["foreground_active"]?.boolValue, false)
        controller.close()
    }

    func testRESTActivationUsesBoundHeadersAndRejectsAdditiveGrantFields() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubVoiceURLProtocol.self]
        let api = URLSessionAppleVoiceControlAPI(session: URLSession(configuration: configuration))
        let control = try XCTUnwrap(VoiceControlBinding(frame: frame(binding())))
        let bound = AppleVoiceUIBinding(
            token: "access-token", serverBase: URL(string: "https://example.test/")!,
            deviceId: device, deviceKind: "ios", connectionGeneration: connection,
            control: control, visibleChatId: chat)
        StubVoiceURLProtocol.install(status: 201, data: Data(restStartResponse().utf8))

        let first = await api.start(
            binding: bound, activationId: "00000000-0000-4000-8000-00000000000f",
            capability: .init(
                hasMicrophone: true, hasAudioOutput: true,
                microphonePermission: "authorized", fullDuplex: true))
        guard case .started(let session, let mediaGrant) = first else {
            return XCTFail("strict canonical REST response should start")
        }
        XCTAssertEqual(session.sessionId, voiceSession)
        XCTAssertEqual(mediaGrant.workerIdentity, "voice-worker-01")
        let request = try XCTUnwrap(StubVoiceURLProtocol.lastRequest())
        XCTAssertEqual(request.url?.absoluteString, "https://example.test/api/voice/sessions")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Astral-Device-Id"), device)
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "X-Astral-Connection-Generation"), connection)
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "X-Astral-Voice-Control-Binding"),
            "synthetic-binding-value-000000000000")
        XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalAndRemoteCacheData)

        StubVoiceURLProtocol.install(
            status: 201, data: Data(restStartResponse(extraGrantField: true).utf8))
        let additive = await api.start(
            binding: bound, activationId: "00000000-0000-4000-8000-00000000000f",
            capability: .init(
                hasMicrophone: true, hasAudioOutput: true,
                microphonePermission: "authorized", fullDuplex: true))
        guard case .failed = additive else {
            return XCTFail("authority-sensitive REST grants must reject unknown fields")
        }
    }

    func testMediaGrantRefreshReadsCurrentStateAndRetriesIdenticalCASAfterLostResponse() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubVoiceURLProtocol.self]
        let api = URLSessionAppleVoiceControlAPI(session: URLSession(configuration: configuration))
        let control = try XCTUnwrap(VoiceControlBinding(frame: frame(binding())))
        let bound = AppleVoiceUIBinding(
            token: "access-token", serverBase: URL(string: "https://example.test/")!,
            deviceId: device, deviceKind: "ios", connectionGeneration: connection,
            control: control, visibleChatId: chat)
        let refreshId = "00000000-0000-4000-8000-00000000000f"
        StubVoiceURLProtocol.install([
            .http(status: 200, data: Data(credentialFreeState(revision: 2).utf8)),
            .failure,
            .http(
                status: 200,
                data: Data(refreshResponse(refreshId: refreshId, revision: 3).utf8)),
        ])

        let outcome = await api.refresh(
            binding: bound, session: restSession(synced: true), refreshId: refreshId)
        guard case .refreshed(let session, let mediaGrant) = outcome else {
            return XCTFail("a replayed idempotent refresh should return the committed grant")
        }
        XCTAssertEqual(session.mediaGrantRevision, 3)
        XCTAssertEqual(mediaGrant.mediaGrantRevision, 3)

        let requests = StubVoiceURLProtocol.requests()
        XCTAssertEqual(requests.map(\.httpMethod), ["GET", "POST", "POST"])
        XCTAssertEqual(requests[0].url?.path, "/api/voice/sessions/\(voiceSession)/media-grants")
        let requestBodies = StubVoiceURLProtocol.requestBodies()
        XCTAssertNil(requestBodies[0])
        XCTAssertEqual(requestBodies[1], requestBodies[2])
        let postBody = try JSONValue.parse(XCTUnwrap(requestBodies[1]))
        XCTAssertEqual(postBody["refresh_id"]?.stringValue, refreshId)
        XCTAssertEqual(postBody["expected_media_grant_revision"]?.numberValue, 2)
        XCTAssertEqual(
            requests[2].value(forHTTPHeaderField: "X-Astral-Voice-Control-Binding"),
            "synthetic-binding-value-000000000000")
    }

    func testMediaGrantRefreshPreservesTerminalSessionEndedCode() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubVoiceURLProtocol.self]
        let api = URLSessionAppleVoiceControlAPI(session: URLSession(configuration: configuration))
        let control = try XCTUnwrap(VoiceControlBinding(frame: frame(binding())))
        let bound = AppleVoiceUIBinding(
            token: "access-token", serverBase: URL(string: "https://example.test/")!,
            deviceId: device, deviceKind: "ios", connectionGeneration: connection,
            control: control, visibleChatId: chat)
        StubVoiceURLProtocol.install(
            status: 503,
            data: Data(
                """
                {"type":"urn:astraldeep:voice:session_ended","title":"Voice request could not be completed","status":503,"code":"session_ended"}
                """.utf8))

        let outcome = await api.refresh(
            binding: bound, session: restSession(synced: true),
            refreshId: "00000000-0000-4000-8000-00000000000f")

        guard case .failed(let reason, let message) = outcome else {
            return XCTFail("a terminal media-grant response must remain terminal")
        }
        XCTAssertEqual(reason, "session_ended")
        XCTAssertNil(message)
        XCTAssertEqual(StubVoiceURLProtocol.requests().count, 1)
        XCTAssertEqual(StubVoiceURLProtocol.lastRequest()?.httpMethod, "GET")
    }

    func testEndUsesOnlyTheBoundAuthoritativeSessionFence() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubVoiceURLProtocol.self]
        let api = URLSessionAppleVoiceControlAPI(session: URLSession(configuration: configuration))
        let control = try XCTUnwrap(VoiceControlBinding(frame: frame(binding())))
        let bound = AppleVoiceUIBinding(
            token: "access-token", serverBase: URL(string: "https://example.test/")!,
            deviceId: device, deviceKind: "ios", connectionGeneration: connection,
            control: control, visibleChatId: chat)
        StubVoiceURLProtocol.install(status: 204, data: Data())

        let ended = await api.end(
            binding: bound,
            fence: AppleVoiceSessionFence(
                sessionId: voiceSession, generation: 4, mediaGrantRevision: 7))

        XCTAssertTrue(ended)
        let request = try XCTUnwrap(StubVoiceURLProtocol.lastRequest())
        XCTAssertEqual(request.httpMethod, "DELETE")
        XCTAssertEqual(request.url?.path, "/api/voice/sessions/\(voiceSession)")
        let query = try XCTUnwrap(URLComponents(url: XCTUnwrap(request.url), resolvingAgainstBaseURL: false))
            .queryItems
        XCTAssertEqual(query?.first { $0.name == "expected_generation" }?.value, "4")
        XCTAssertEqual(query?.first { $0.name == "expected_media_grant_revision" }?.value, "7")
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "X-Astral-Voice-Control-Binding"),
            "synthetic-binding-value-000000000000")
    }

    func testCloseHardStopsLocalMediaAndBestEffortEndsBoundSession() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        controller.close()
        await Task.yield()

        XCTAssertEqual(controller.phase, "off")
        XCTAssertFalse(controller.mediaConnected)
        XCTAssertGreaterThanOrEqual(media.disconnectCount, 1)
        XCTAssertEqual(api.endCount, 1)
    }

    func testMicrophoneControlReportsMicrophoneOffNotAssistantSpeechMuted() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        api.updatedSession = restSession(synced: true, microphone: false)
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        await controller.perform(.microphone)

        XCTAssertEqual(api.lastUpdate?["microphone_enabled"]?.boolValue, false)
        XCTAssertEqual(media.microphoneValues.last, false)
        XCTAssertEqual(controller.phase, "listening")
        XCTAssertEqual(controller.message, "Microphone is off.")
        controller.close()
    }

    func testIndependentMediaControlsReportEachMuteCombination() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        api.updatedSession = restSession(
            synced: true, microphone: true, speechMuted: true)
        await controller.perform(.muteSpeech)
        XCTAssertEqual(controller.phase, "muted")
        XCTAssertEqual(controller.message, "Assistant speech is muted.")

        api.updatedSession = restSession(
            synced: true, microphone: false, speechMuted: true)
        await controller.perform(.microphone)
        XCTAssertEqual(controller.phase, "muted")
        XCTAssertEqual(
            controller.message, "Microphone and assistant speech are muted.")

        api.updatedSession = restSession(
            synced: true, microphone: false, speechMuted: false)
        await controller.perform(.muteSpeech)
        XCTAssertEqual(controller.phase, "listening")
        XCTAssertEqual(controller.message, "Microphone is off.")

        api.updatedSession = restSession(
            synced: true, microphone: true, speechMuted: false)
        await controller.perform(.microphone)
        XCTAssertEqual(controller.phase, "listening")
        XCTAssertEqual(controller.message, "Listening…")
        controller.close()
    }

    func testAuthenticationExpiryHardTearsDownAndCannotAutoRecover() async throws {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        api.refreshOutcomes = [
            .refreshed(restSession(synced: true, revision: 3), grant(revision: 3))
        ]
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)
        await controller.activate()

        controller.consume(frame("{\"type\":\"auth_required\",\"reason\":\"expired\"}"))
        controller.sceneBecameActive()
        controller.consume(frame(binding()))
        try await Task.sleep(nanoseconds: 20_000_000)

        XCTAssertEqual(controller.phase, "unavailable")
        XCTAssertEqual(controller.reason, "authentication_required")
        XCTAssertFalse(controller.active)
        XCTAssertFalse(controller.mediaConnected)
        XCTAssertTrue(api.refreshIds.isEmpty)
        XCTAssertGreaterThanOrEqual(media.disconnectCount, 1)
    }

    func testComposerOwnedSessionWithoutLocalMediaStaysActionableAndCanEndByFence() async {
        let api = FakeVoiceAPI()
        let media = FakeVoiceMedia()
        let controller = makeController(api: api, media: media)
        install(controller)

        controller.consume(frame(activeComposer()))

        XCTAssertEqual(controller.phase, "reconnecting")
        XCTAssertEqual(controller.reason, "network_interrupted")
        XCTAssertTrue(
            controller.composer?.controls.contains { $0.action == .end && $0.visible } == true)

        await controller.perform(.microphone)
        await controller.perform(.muteSpeech)
        XCTAssertEqual(controller.phase, "reconnecting")
        XCTAssertTrue(api.updates.isEmpty)
        XCTAssertNotNil(controller.composer)

        await controller.perform(.end)
        XCTAssertEqual(api.endCount, 1)
        XCTAssertEqual(
            api.lastEndFence,
            AppleVoiceSessionFence(
                sessionId: voiceSession, generation: 1, mediaGrantRevision: 2))
        XCTAssertNotNil(controller.composer, "ending must retain server-owned controls until refresh")

        controller.consume(frame(offComposer()))
        XCTAssertEqual(controller.phase, "off")
        XCTAssertTrue(
            controller.composer?.controls.contains { $0.action == .start && $0.visible } == true)
    }

    func testComposerRevisionResetsForNewConnectionGenerationAndRejectsDuplicates() {
        let controller = makeController(api: FakeVoiceAPI(), media: FakeVoiceMedia())
        install(controller)
        controller.consume(frame(activeComposer(revision: 7)))
        XCTAssertTrue(
            controller.composer?.controls.contains { $0.action == .end && $0.visible } == true)

        controller.installUIConnection(
            token: "access-token", serverBase: URL(string: "https://example.test/")!,
            deviceId: device, deviceKind: "ios", connectionGeneration: otherConnection,
            visibleChatId: chat)
        controller.consume(
            frame(offComposer(revision: 0, connectionGeneration: otherConnection)))
        XCTAssertEqual(controller.phase, "off")
        XCTAssertTrue(
            controller.composer?.controls.contains { $0.action == .start && $0.visible } == true)

        controller.consume(
            frame(activeComposer(revision: 0, connectionGeneration: otherConnection)))
        XCTAssertEqual(controller.phase, "off", "equal-revision duplicate must be ignored")
        XCTAssertTrue(
            controller.composer?.controls.contains { $0.action == .start && $0.visible } == true)
    }

    func testForegroundResumeRevalidatesMicrophonePermissionBeforeRenewing() async {
        let api = FakeVoiceAPI()
        api.startOutcome = .started(restSession(synced: true), grant())
        let media = FakeVoiceMedia()
        var capabilityRead = 0
        let controller = AppleVoiceSessionController(
            api: api, media: media,
            permissionProvider: {
                capabilityRead += 1
                return .init(
                    hasMicrophone: true, hasAudioOutput: true,
                    microphonePermission: capabilityRead == 1 ? "authorized" : "denied",
                    fullDuplex: true)
            },
            uuid: { "00000000-0000-4000-8000-00000000000f" })
        install(controller)
        await controller.activate()
        controller.sceneBecameInactive()
        await Task.yield()
        let updateCount = api.updates.count

        controller.sceneBecameActive()
        await Task.yield()
        await Task.yield()

        XCTAssertEqual(controller.reason, "permission_denied")
        XCTAssertEqual(api.updates.count, updateCount)
        XCTAssertEqual(media.microphoneValues.last, false)
        controller.close()
    }

    private func makeController(
        api: FakeVoiceAPI, media: FakeVoiceMedia,
        capability: AppleVoiceMediaCapability = .init(
            hasMicrophone: true, hasAudioOutput: true,
            microphonePermission: "authorized", fullDuplex: true),
        retryNanoseconds: UInt64 = 2_500_000_000,
        leaseRenewalNanoseconds: UInt64 = 20_000_000_000
    ) -> AppleVoiceSessionController {
        AppleVoiceSessionController(
            api: api, media: media, permissionProvider: { capability },
            uuid: { "00000000-0000-4000-8000-00000000000f" },
            retryNanoseconds: retryNanoseconds,
            leaseRenewalNanoseconds: leaseRenewalNanoseconds)
    }

    private func install(_ controller: AppleVoiceSessionController, replaceSender: Bool = true) {
        if replaceSender { controller.setFrameSender { _ in true } }
        controller.installUIConnection(
            token: "access-token", serverBase: URL(string: "https://example.test/")!,
            deviceId: device, deviceKind: "ios", connectionGeneration: connection,
            visibleChatId: chat)
        controller.consume(frame(binding()))
    }

    private func restSession(
        synced: Bool, revision: Int = 2, microphone: Bool? = nil,
        speechMuted: Bool = false
    ) -> AppleVoiceRestSession {
        AppleVoiceRestSession(
            sessionId: voiceSession, deviceId: device, deviceKind: "ios",
            transport: "livekit",
            ownerConnectionGeneration: connection, visibleChatId: chat,
            appliedVisibleChatId: synced ? chat : nil, generation: 1,
            mediaGrantRevision: revision, chatContextRevision: 3,
            appliedChatContextRevision: synced ? 3 : nil, chatContextSynced: synced,
            state: "active", foregroundActive: true, foregroundReason: "foreground",
            speechMuted: speechMuted, microphoneEnabled: microphone ?? synced,
            leaseExpiresAt: "2099-07-31T12:01:00Z")
    }

    private func grant(revision: Int = 2) -> AppleLiveKitGrant {
        AppleLiveKitGrant(
            grantId: "grant-01", sessionId: voiceSession, generation: 1,
            mediaGrantRevision: revision, expiresAt: "2099-07-31T12:02:00Z",
            url: "wss://voice.example.test", joinToken: String(repeating: "a", count: 64),
            roomName: "voice-room", participantIdentity: "ios-client-01",
            workerIdentity: "voice-worker-01")
    }

    private func binding() -> String {
        """
        {"type":"voice_control_binding","schema_version":"1","device_id":"\(device)","connection_generation":"\(connection)","binding_id":"00000000-0000-4000-8000-00000000000a","binding":"synthetic-binding-value-000000000000","expires_at":"2099-07-31T12:10:00Z"}
        """
    }

    private func activeComposer(
        revision: Int = 1, connectionGeneration: String? = nil
    ) -> String {
        """
        {"type":"composer_state","schema_version":"1","revision":\(revision),"connection_generation":"\(connectionGeneration ?? connection)","voice":{"available":true,"state":"listening","speech_muted":false,"microphone_enabled":true,"foreground_active":true,"reason":"ready","output_locale":"en-US","chat_context_revision":3,"applied_chat_context_revision":3,"chat_context_synced":true,"session_id":"\(voiceSession)","generation":1,"media_grant_revision":2,"visible_chat_id":"\(chat)","owner_device":{"device_id":"\(device)","device_kind":"ios","generation":1},"controls":[{"key":"voice-microphone","action":"voice_microphone_set","label":"Microphone","icon":"microphone","visible":true,"enabled":true,"pressed":true,"busy":false},{"key":"voice-mute","action":"voice_speech_mute_set","label":"Mute voice","icon":"speaker-muted","visible":true,"enabled":true,"pressed":false,"busy":false},{"key":"voice-end","action":"voice_session_end","label":"End","icon":"stop","visible":true,"enabled":true,"pressed":false,"busy":false}]}}
        """
    }

    private func offComposer(
        revision: Int = 2, connectionGeneration: String? = nil
    ) -> String {
        """
        {"type":"composer_state","schema_version":"1","revision":\(revision),"connection_generation":"\(connectionGeneration ?? connection)","voice":{"available":true,"state":"off","speech_muted":false,"microphone_enabled":false,"foreground_active":false,"reason":"ready","output_locale":"en-US","chat_context_revision":null,"applied_chat_context_revision":null,"chat_context_synced":false,"controls":[{"key":"voice-start","action":"voice_session_start","label":"Start voice","icon":"microphone","visible":true,"enabled":true,"pressed":false,"busy":false}]}}
        """
    }

    private func finalTranscript() -> String {
        """
        {"type":"voice_transcript","schema_version":"1","session_id":"\(voiceSession)","generation":1,"turn_id":"\(turn)","client_turn_id":"\(clientTurn)","submission_id":"\(submission)","request_generation":"\(request)","chat_id":"\(chat)","chat_context_revision":3,"media_grant_revision":2,"sequence":1,"final":true,"text":"Review the result","detected_language":"en-US","text_digest_sha256":"5b6c9147d242ef629cc5731bd8844b86f5ef8bcf4e6d4d741bb80d2bc446ab04","transcript_proof":"af81ff8058e12f5622e53f8c1dc3ed460b753c13a3df46f9e09e40ca8f96a7f9","proof_expires_at":"2099-07-31T12:02:00Z","source_participant_identity":"voice-worker-01"}
        """
    }

    private func sessionState(synced: Bool, microphone: Bool) -> String {
        """
        {"type":"voice_session_state","schema_version":"1","session_id":"\(voiceSession)","connection_generation":"\(connection)","generation":1,"media_grant_revision":2,"visible_chat_id":"\(chat)","chat_context_revision":3,"applied_chat_context_revision":\(synced ? "3" : "null"),"chat_context_synced":\(synced),"state":"listening","speech_muted":false,"microphone_enabled":\(microphone),"foreground_active":true,"reason":"ready","occurred_at":"2099-07-31T12:00:00Z"}
        """
    }

    private func terminalSessionState(
        state: String, reason: String, message: String
    ) -> String {
        """
        {"type":"voice_session_state","schema_version":"1","session_id":"\(voiceSession)","connection_generation":"\(connection)","generation":1,"media_grant_revision":2,"visible_chat_id":"\(chat)","chat_context_revision":3,"applied_chat_context_revision":3,"chat_context_synced":true,"state":"\(state)","speech_muted":false,"microphone_enabled":true,"foreground_active":true,"reason":"\(reason)","message":"\(message)","occurred_at":"2099-07-31T12:00:00Z"}
        """
    }

    private func voiceTurn(
        state: String,
        turnId: String? = nil,
        sequence: Int = 1,
        message: String? = nil,
        occurredAt: String = "2099-07-31T12:00:00Z",
        speechOutcome: String? = nil
    ) throws -> String {
        var value: [String: JSONValue] = [
            "type": .string("voice_turn_state"),
            "schema_version": .string("1"),
            "session_id": .string(voiceSession),
            "connection_generation": .string(connection),
            "generation": .number(1),
            "media_grant_revision": .number(2),
            "turn_id": .string(turnId ?? turn),
            "client_turn_id": .string(clientTurn),
            "submission_id": .string(submission),
            "request_generation": .string(request),
            "chat_id": .string(chat),
            "chat_context_revision": .number(3),
            "detected_language": .string("en-US"),
            "spoken_output_policy": .string("full_recap"),
            "output_reason": .string("ready"),
            "state": .string(state),
            "foreground": .bool(true),
            "sensitive_result_pending": .bool(false),
            "sequence": .number(Double(sequence)),
            "occurred_at": .string(occurredAt),
        ]
        if let message { value["message"] = .string(message) }
        if let speechOutcome { value["speech_outcome"] = .string(speechOutcome) }
        return String(decoding: try JSONValue.object(value).encoded(), as: UTF8.self)
    }

    private func submissionRejection(
        message: String, retryPolicy: String
    ) -> String {
        """
        {"type":"voice_submission_rejected","schema_version":"1","session_id":"\(voiceSession)","connection_generation":"\(connection)","generation":1,"media_grant_revision":2,"turn_id":"\(turn)","client_turn_id":"\(clientTurn)","submission_id":"\(submission)","request_generation":"\(request)","chat_id":"\(chat)","reason":"capacity_exhausted","retry_policy":"\(retryPolicy)","message":"\(message)","occurred_at":"2099-07-31T12:00:00Z"}
        """
    }

    private func ack(connection: String) -> String {
        """
        {"type":"user_message_acked","schema_version":"1","chat_id":"\(chat)","message_id":41,"submission_id":"\(submission)","request_generation":"\(request)","connection_generation":"\(connection)","voice_turn_id":"\(turn)"}
        """
    }

    private func playoutMatcher() -> AppleVoicePlayoutMatcher {
        AppleVoicePlayoutMatcher(
            sessionId: voiceSession, generation: 1, mediaGrantRevision: 2,
            workerIdentity: "voice-worker-01")
    }

    private func publishedTrack(sid: String) -> AppleVoicePublishedTrack {
        AppleVoicePublishedTrack(
            sid: sid, name: "voice-result-opening",
            workerIdentity: "voice-worker-01", isAudio: true)
    }

    private func announcementValue(sequence: Int, sid: String) throws -> VoiceAnnouncementMedia {
        try XCTUnwrap(
            VoiceAnnouncementMedia(
                frame: frame(announcement(sequence: sequence, sid: sid))))
    }

    private func announcement(
        sequence: Int = 3, sid: String = "TR_audio_001",
        trackName: String = "voice-result-opening"
    ) -> String {
        let announcementId = String(
            format: "00000000-0000-4000-8000-%012llx", Int64(sequence))
        return """
            {"type":"voice_announcement_media","schema_version":"1","session_id":"\(voiceSession)","generation":1,"media_grant_revision":2,"announcement_id":"\(announcementId)","announcement_sequence":\(sequence),"turn_id":"\(turn)","kind":"result","quantum_role":"result_opening","quantum_index":0,"transport":"livekit","worker_identity":"voice-worker-01","sample_rate_hz":24000,"duration_samples":36000,"result_reserved_samples_after":36000,"track_sid":"\(sid)","track_name":"\(trackName)"}
            """
    }

    private func greetingAnnouncement(
        sequence: Int = 4, sid: String = "TR_audio_greeting"
    ) -> String {
        let announcementId = String(
            format: "00000000-0000-4000-8000-%012llx", Int64(sequence))
        return """
            {"type":"voice_announcement_media","schema_version":"1","session_id":"\(voiceSession)","generation":1,"media_grant_revision":2,"announcement_id":"\(announcementId)","announcement_sequence":\(sequence),"turn_id":null,"kind":"greeting","quantum_role":"single","quantum_index":0,"transport":"livekit","worker_identity":"voice-worker-01","sample_rate_hz":24000,"duration_samples":24000,"track_sid":"\(sid)","track_name":"voice-greeting"}
            """
    }

    private func restStartResponse(extraGrantField: Bool = false) -> String {
        let extra = extraGrantField ? ",\"unexpected\":true" : ""
        return """
            {"session":{"session_id":"\(voiceSession)","device_id":"\(device)","device_kind":"ios","transport":"livekit","state":"active","generation":1,"media_grant_revision":2,"owner_connection_generation":"\(connection)","visible_chat_id":"\(chat)","applied_visible_chat_id":"\(chat)","chat_context_revision":3,"applied_chat_context_revision":3,"chat_context_synced":true,"foreground_active":true,"foreground_reason":"foreground","foreground_changed_at":"2099-07-31T12:00:00Z","speech_muted":false,"microphone_enabled":true,"lease_expires_at":"2099-07-31T12:01:00Z","started_at":"2099-07-31T12:00:00Z","idle_expires_at":null},"grant":{"grant_id":"grant-01","transport":"livekit","session_id":"\(voiceSession)","generation":1,"media_grant_revision":2,"expires_at":"2099-07-31T12:02:00Z","url":"wss://voice.example.test","join_token":"\(String(repeating: "a", count: 64))","room_name":"voice-room","participant_identity":"ios-client-01","worker_identity":"voice-worker-01"\(extra)}}
            """
    }

    private func credentialFreeState(revision: Int) -> String {
        """
        {"session":\(restSessionJSON(revision: revision)),"grant_state":{"transport":"livekit","media_grant_revision":\(revision),"status":"active","expires_at":"2099-07-31T12:02:00Z"}}
        """
    }

    private func refreshResponse(refreshId: String, revision: Int) -> String {
        """
        {"refresh_id":"\(refreshId)","replayed":true,"replay_expires_at":"2099-07-31T12:03:00Z","session":\(restSessionJSON(revision: revision)),"grant":\(grantJSON(revision: revision))}
        """
    }

    private func restSessionJSON(revision: Int) -> String {
        """
        {"session_id":"\(voiceSession)","device_id":"\(device)","device_kind":"ios","transport":"livekit","state":"active","generation":1,"media_grant_revision":\(revision),"owner_connection_generation":"\(connection)","visible_chat_id":"\(chat)","applied_visible_chat_id":"\(chat)","chat_context_revision":3,"applied_chat_context_revision":3,"chat_context_synced":true,"foreground_active":true,"foreground_reason":"foreground","foreground_changed_at":"2099-07-31T12:00:00Z","speech_muted":false,"microphone_enabled":true,"lease_expires_at":"2099-07-31T12:01:00Z","started_at":"2099-07-31T12:00:00Z","idle_expires_at":null}
        """
    }

    private func grantJSON(revision: Int) -> String {
        """
        {"grant_id":"grant-\(revision)","transport":"livekit","session_id":"\(voiceSession)","generation":1,"media_grant_revision":\(revision),"expires_at":"2099-07-31T12:02:00Z","url":"wss://voice.example.test","join_token":"\(String(repeating: "a", count: 64))","room_name":"voice-room","participant_identity":"ios-client-01","worker_identity":"voice-worker-01"}
        """
    }

    private func frame(_ raw: String) -> InboundFrame { InboundFrame.parse(raw)! }
}

private final class StubVoiceURLProtocol: URLProtocol, @unchecked Sendable {
    enum Response {
        case http(status: Int, data: Data)
        case failure
    }

    private static let lock = NSLock()
    private nonisolated(unsafe) static var responses: [Response] = []
    private nonisolated(unsafe) static var capturedRequests: [URLRequest] = []
    private nonisolated(unsafe) static var capturedBodies: [Data?] = []

    static func install(status: Int, data: Data) {
        install([.http(status: status, data: data)])
    }

    static func install(_ values: [Response]) {
        lock.lock()
        responses = values
        capturedRequests = []
        capturedBodies = []
        lock.unlock()
    }

    static func lastRequest() -> URLRequest? {
        lock.lock()
        defer { lock.unlock() }
        return capturedRequests.last
    }

    static func requests() -> [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return capturedRequests
    }

    static func requestBodies() -> [Data?] {
        lock.lock()
        defer { lock.unlock() }
        return capturedBodies
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let body = request.httpBody ?? Self.read(request.httpBodyStream)
        Self.lock.lock()
        Self.capturedRequests.append(request)
        Self.capturedBodies.append(body)
        let response =
            Self.responses.isEmpty
            ? Response.http(status: 500, data: Data())
            : Self.responses.removeFirst()
        Self.lock.unlock()
        switch response {
        case .failure:
            client?.urlProtocol(
                self,
                didFailWithError: URLError(.networkConnectionLost))
        case .http(let status, let data):
            let response = HTTPURLResponse(
                url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                headerFields: ["Cache-Control": "no-store", "Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    override func stopLoading() {}

    private static func read(_ stream: InputStream?) -> Data? {
        guard let stream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4_096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count >= 0 else { return nil }
            if count == 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}

@MainActor
private final class FakeAudioRouteSnapshotSource {
    var value: AppleVoiceAudioRouteSnapshot

    init(_ value: AppleVoiceAudioRouteSnapshot) {
        self.value = value
    }
}

@MainActor
private final class FakeAudioCapabilitySource {
    var value: AppleVoiceMediaCapability

    init(_ value: AppleVoiceMediaCapability) {
        self.value = value
    }
}

@MainActor
private final class FakeVoiceAPI: AppleVoiceControlAPI {
    var startOutcome: AppleVoiceStartOutcome = .failed("voice_unavailable", nil)
    var takeoverOutcome: AppleVoiceStartOutcome = .failed("voice_unavailable", nil)
    var refreshOutcomes: [AppleVoiceRefreshOutcome] = []
    var startCount = 0
    var takeoverCount = 0
    var refreshIds: [String] = []
    var endCount = 0
    var lastEndFence: AppleVoiceSessionFence?
    var stopCount = 0
    var lastUpdate: [String: JSONValue]?
    var updates: [[String: JSONValue]] = []
    var updatedSession: AppleVoiceRestSession?
    var updateShouldFail = false

    func start(
        binding: AppleVoiceUIBinding, activationId: String,
        capability: AppleVoiceMediaCapability
    ) async -> AppleVoiceStartOutcome {
        startCount += 1
        return startOutcome
    }

    func takeover(
        binding: AppleVoiceUIBinding, activationId: String,
        target: AppleVoiceTakeoverTarget, capability: AppleVoiceMediaCapability
    ) async -> AppleVoiceStartOutcome {
        takeoverCount += 1
        return takeoverOutcome
    }

    func update(
        binding: AppleVoiceUIBinding, session: AppleVoiceRestSession,
        fields: [String: JSONValue]
    ) async -> AppleVoiceRestSession? {
        lastUpdate = fields
        updates.append(fields)
        if updateShouldFail { return nil }
        return updatedSession ?? session
    }

    func refresh(
        binding: AppleVoiceUIBinding, session: AppleVoiceRestSession,
        refreshId: String
    ) async -> AppleVoiceRefreshOutcome {
        refreshIds.append(refreshId)
        guard !refreshOutcomes.isEmpty else {
            return .failed("network_interrupted", nil)
        }
        return refreshOutcomes.removeFirst()
    }

    func stopSpeech(binding: AppleVoiceUIBinding, session: AppleVoiceRestSession) async -> Bool {
        stopCount += 1
        return true
    }
    func consent(
        binding: AppleVoiceUIBinding, session: AppleVoiceRestSession,
        resultId: String, turnId: String
    ) async -> Bool { true }
    func end(binding: AppleVoiceUIBinding, fence: AppleVoiceSessionFence) async -> Bool {
        endCount += 1
        lastEndFence = fence
        return true
    }
}

@MainActor
private final class FakeVoiceMedia: AppleVoiceMediaClient {
    var eventHandler: ((AppleVoiceMediaEvent) -> Void)?
    var connectCount = 0
    var microphoneValues: [Bool] = []
    var authorized: [VoiceAnnouncementMedia] = []
    var authorizationResult = true
    var interruptionCount = 0
    var disconnectCount = 0
    var holdNextConnect = false
    private var connectContinuation: CheckedContinuation<Void, Never>?

    func connect(_ grant: AppleLiveKitGrant) async throws {
        connectCount += 1
        if holdNextConnect {
            holdNextConnect = false
            await withCheckedContinuation { connectContinuation = $0 }
        }
    }
    func setMicrophoneEnabled(_ enabled: Bool) async throws { microphoneValues.append(enabled) }
    func authorize(_ announcement: VoiceAnnouncementMedia) -> Bool {
        authorized.append(announcement)
        return authorizationResult
    }
    func interruptPlayout() { interruptionCount += 1 }
    func disconnect() { disconnectCount += 1 }
    func emit(_ event: AppleVoiceMediaEvent) { eventHandler?(event) }
    func releaseConnect() {
        connectContinuation?.resume()
        connectContinuation = nil
    }
}
