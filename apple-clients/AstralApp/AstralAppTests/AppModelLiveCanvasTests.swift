import AstralCore
import SwiftUI
import XCTest

@testable import AstralDeep

// Live-op rule (retires the 044 origin/co-viewer divergence): while a turn is
// armed (`pendingReplace`), identity-keyed `ui_upsert`/stream ops apply
// IMMEDIATELY to the visible canvas — morph-in-place, exactly as when no turn
// is armed — so the originating device renders partial output just like
// co-viewing devices. Only full `ui_render` replaces still buffer into
// `pendingCanvas` (the actual mid-turn clobber hazard); a buffered render
// wins at commit, with mid-turn ops mirrored into it so nothing applied live
// is lost. The first live op clears the query-start skeleton (web parity:
// first canvas content hides it) without ending the turn-active state.
@MainActor
final class AppModelLiveCanvasTests: XCTestCase {

    private var workspaceCard: AstralComponent {
        AstralComponent(
            json: .object([
                "type": .string("card"), "component_id": .string("wc_abc123"),
                "title": .string("Budget"),
            ]))!
    }

    private let doneStatus = #"{"type":"chat_status","status":"done"}"#
    private let resultUpsert =
        #"{"type":"ui_upsert","ops":[{"op":"upsert","component_id":"wc_result","component":{"type":"card","component_id":"wc_result","title":"Result"}}]}"#
    private let designedRender =
        #"{"type":"ui_render","target":"canvas","components":[{"type":"card","component_id":"wc_designed","title":"Designed"}]}"#

    private func reduce(_ model: AppModel, _ json: String) {
        model.handleFrame(InboundFrame.parse(json)!)
    }

    // MARK: upsert ops go live mid-turn

    func testUpsertAppliesToVisibleCanvasMidTurn() {
        let model = AppModel()
        model.canvas = [workspaceCard]
        model.sendChat("go")
        reduce(model, resultUpsert)
        XCTAssertEqual(model.canvas.map(\.componentId), ["wc_abc123", "wc_result"])
        XCTAssertTrue(model.pendingCanvas.isEmpty)  // no render — nothing buffered
        XCTAssertTrue(model.turnActive)  // the turn is still running
    }

    func testUpsertOnlyTurnCommitsTheLiveCanvas() {
        let model = AppModel()
        model.canvas = [workspaceCard]
        model.sendChat("go")
        reduce(model, resultUpsert)
        reduce(model, doneStatus)
        // No replace happened: the live canvas IS the committed state —
        // no double-apply, no loss, and nothing archived to the timeline.
        XCTAssertEqual(model.canvas.map(\.componentId), ["wc_abc123", "wc_result"])
        XCTAssertTrue(model.canvasHistory.isEmpty)
        XCTAssertFalse(model.turnActive)
    }

    // MARK: full renders still buffer; the buffered render wins at commit

    func testRenderStaysBufferedUntilCommit() {
        let model = AppModel()
        model.canvas = [workspaceCard]
        model.sendChat("go")
        reduce(model, designedRender)
        XCTAssertEqual(model.canvas.map(\.componentId), ["wc_abc123"])  // visible canvas untouched
        XCTAssertEqual(model.pendingCanvas.map(\.componentId), ["wc_designed"])
        reduce(model, doneStatus)
        XCTAssertEqual(model.canvas.map(\.componentId), ["wc_designed"])
        XCTAssertEqual(model.canvasHistory.count, 1)
    }

    func testOpsAfterBufferedRenderMirrorIntoCommit() {
        let model = AppModel()
        model.sendChat("go")
        reduce(model, designedRender)
        reduce(model, resultUpsert)  // live AND mirrored into the buffer
        XCTAssertEqual(model.canvas.map(\.componentId), ["wc_result"])
        XCTAssertEqual(model.pendingCanvas.map(\.componentId), ["wc_designed", "wc_result"])
        reduce(model, doneStatus)
        XCTAssertEqual(model.canvas.map(\.componentId), ["wc_designed", "wc_result"])
    }

    // MARK: skeleton clears on the first live op, turn stays active

    func testFirstLiveOpClearsSkeletonWithoutEndingTurn() {
        let model = AppModel()
        model.sendChat("go")
        XCTAssertTrue(model.showSkeleton)
        reduce(model, resultUpsert)
        XCTAssertFalse(model.showSkeleton)
        XCTAssertTrue(model.turnActive)
    }

    func testBufferedRenderKeepsSkeleton() {
        let model = AppModel()
        model.sendChat("go")
        reduce(model, designedRender)  // invisible until commit — keep the shimmer
        XCTAssertTrue(model.showSkeleton)
    }

    func testStreamOpsGoLiveMidTurnAndClearSkeleton() {
        let model = AppModel()
        model.sendChat("go")
        reduce(
            model,
            #"{"type":"ui_stream_data","stream_id":"s1","seq":1,"components":[{"type":"text","content":"partial"}]}"#)
        XCTAssertEqual(model.canvas.map(\.componentId), ["stream-s1"])
        XCTAssertFalse(model.showSkeleton)
        XCTAssertTrue(model.pendingCanvas.isEmpty)
    }

    func testNextTurnReArmsSkeleton() {
        let model = AppModel()
        model.sendChat("one")
        reduce(model, resultUpsert)
        reduce(model, doneStatus)
        model.sendChat("two")
        XCTAssertTrue(model.showSkeleton)  // liveOpsThisTurn resets on arm
    }

    // MARK: 063 stuck-canvas regression — continuity-mode turn terminal frames

    // Frame shapes taken from a live frame trace of the 2026-07-27 stuck-
    // skeleton reproduction (chat 209bed7e…): the server delivered every
    // terminal frame (`conversation_snapshot`, `chat_status done`, and the
    // post-done designed `ui_render`) and the reducer must resolve the
    // skeleton and commit the canvas from exactly this sequence. The live
    // failure was NOT a reducer defect — a shimmer-driven layout livelock
    // starved the MainActor so these frames were never reduced — but this
    // pins the wire contract the fix restored end-to-end.
    private static let chat = "209bed7e-2057-4c88-b707-57406e81c52d"
    private static let connection = "09733175-9b07-4625-9247-f25a643350b5"
    private static let request = "8a21ba74-fa14-4e02-8461-f65e75fedc82"

    private func fence(_ sequence: Int, base: Int = 0) -> String {
        #""chat_id":"\#(Self.chat)","connection_generation":"\#(Self.connection)","#
            + #""request_generation":"\#(Self.request)","#
            + #""base_render_revision":\#(base),"frame_sequence":\#(sequence)"#
    }

    private func armContinuityTurn(_ model: AppModel) {
        XCTAssertTrue(model.beginConversationConnection(Self.connection))
        XCTAssertTrue(
            model.openConversationRequest(
                chatId: Self.chat, requestGeneration: Self.request, purpose: .commit))
        model.activeChatId = Self.chat
        model.turnActive = true
        model.pendingReplace = true
        model.pendingCanvas = []
        model.liveOpsThisTurn = false
        model.stepTrail = ["✓ roll_dice"]
    }

    private var committedSnapshot: String {
        #"{"type":"conversation_snapshot","schema_version":1,"#
            + #""snapshot_id":"0980a848-4021-457a-83d2-7884d9028be7","#
            + #""chat_id":"\#(Self.chat)","#
            + #""connection_generation":"\#(Self.connection)","#
            + #""request_generation":"\#(Self.request)","#
            + #""snapshot_purpose":"commit","render_revision":6,"#
            + #""committed_at":"2026-07-27T18:02:08Z","#
            + #""transcript":[{"message_id":"m-1","role":"user","#
            + #""created_at":"2026-07-27T18:01:45Z","#
            + #""parts":[{"type":"text","text":"Roll two dice and show live system metrics"}],"#
            + #""attachments":[]},"#
            + #"{"message_id":"m-2","role":"assistant","#
            + #""created_at":"2026-07-27T18:02:07Z","#
            + #""parts":[{"type":"text","text":"Rolled 3 and 2."}],"attachments":[]}],"#
            + #""canvas":{"target":"canvas","components":["#
            + #"{"type":"card","component_id":"wc_dice","title":"Dice Roll Results"},"#
            + #"{"type":"card","component_id":"wc_metrics","title":"Live System Metrics"}]}}"#
    }

    func testContinuityTerminalFramesClearSkeletonAndCommitCanvas() {
        let model = AppModel()
        armContinuityTurn(model)
        XCTAssertTrue(model.showSkeleton)

        // Mid-turn fenced transient upsert — overlay only, skeleton stays.
        reduce(
            model,
            #"{"type":"ui_upsert",\#(fence(1)),"ops":[{"op":"upsert","#
                + #""component_id":"wc_dice","component":{"type":"card","#
                + #""component_id":"wc_dice","title":"Dice Roll Results"}}]}"#)
        XCTAssertEqual(model.transientCanvas?.map(\.componentId), ["wc_dice"])
        XCTAssertTrue(model.showSkeleton)

        // Rail narrative preview (fenced, chat target).
        reduce(
            model,
            #"{"type":"ui_render","target":"chat",\#(fence(2)),"#
                + #""components":[{"type":"text","content":"Rolled 3 and 2.","#
                + #""variant":"markdown"}]}"#)
        XCTAssertTrue(model.transientTurns.contains { $0.role == "assistant" })

        // The committed snapshot resolves the skeleton and replaces the canvas.
        reduce(model, committedSnapshot)
        XCTAssertFalse(model.showSkeleton)
        XCTAssertEqual(model.canvas.map(\.componentId), ["wc_dice", "wc_metrics"])
        XCTAssertNil(model.transientCanvas)
        XCTAssertEqual(model.turns.map(\.role), ["user", "assistant"])

        // Terminal status clears the turn chrome.
        reduce(model, doneStatus)
        XCTAssertFalse(model.turnActive)
        XCTAssertNil(model.statusText)
        XCTAssertTrue(model.stepTrail.isEmpty)

        // The post-done designed refinement rides the NEXT fence, but the
        // committed snapshot already carries the designed canvas — on a
        // continuity client the overlay is a deliberate no-op (a completed
        // scope accepts no further transients).
        reduce(
            model,
            #"{"type":"ui_render","target":"canvas",\#(fence(1, base: 6)),"#
                + #""components":[{"type":"card","component_id":"wc_designed","#
                + #""title":"Designed"}]}"#)
        XCTAssertEqual(model.canvas.map(\.componentId), ["wc_dice", "wc_metrics"])
        XCTAssertFalse(model.showSkeleton)
    }

    // A done that arrives with NO snapshot (error-path turns) must still
    // release the skeleton in continuity mode — the canvas it uncovers is the
    // prior committed state, which is honest.
    func testContinuityDoneWithoutSnapshotReleasesSkeleton() {
        let model = AppModel()
        armContinuityTurn(model)
        XCTAssertTrue(model.showSkeleton)
        reduce(model, doneStatus)
        XCTAssertFalse(model.showSkeleton)
        XCTAssertFalse(model.turnActive)
    }

    // MARK: 063 shimmer sweep — pure curve

    // The shimmer highlight is driven by a TimelineView so its per-frame
    // invalidation stays scoped to the overlay (the animated-@State version
    // forced a full-screen layout pass per animation frame and livelocked the
    // main thread — the stuck-skeleton defect). The sweep itself is pinned
    // here: one 1.3 s cycle crosses the card from x = -1·w to x = 1.6·w.
    func testShimmerPhaseSweepMatchesLegacyAnimation() {
        XCTAssertEqual(ShimmerModifier.phase(cycle: 0), -1.0, accuracy: 0.0001)
        XCTAssertEqual(ShimmerModifier.phase(cycle: 0.5), 0.3, accuracy: 0.0001)
        XCTAssertEqual(ShimmerModifier.phase(cycle: 0.9999), 1.6, accuracy: 0.001)
    }

    func testContinuousActivityPresentationMatchesPlatformLayoutSafetyPolicy() {
        #if os(macOS)
            XCTAssertFalse(ContinuousActivityPresentation.allowsAnimatedIndicators)
        #else
            XCTAssertTrue(ContinuousActivityPresentation.allowsAnimatedIndicators)
        #endif
    }

    func testTranscriptRowsMatchPlatformLayoutSafetyPolicy() {
        #if os(macOS)
            XCTAssertFalse(TranscriptLayoutPresentation.usesLazyRows)
        #else
            XCTAssertTrue(TranscriptLayoutPresentation.usesLazyRows)
        #endif
    }

    #if os(macOS)
        func testMacTranscriptLayoutSettlesAcrossVoiceCommitChurn() {
            let model = AppModel()
            model.turns = (0..<10).map { index in
                .init(
                    id: "committed-\(index)",
                    role: index.isMultiple(of: 2) ? "user" : "assistant",
                    text: String(repeating: "A bounded transcript row. ", count: 12))
            }
            let controller = NSHostingController(
                rootView: ChatShell()
                    .environment(model)
                    .environment(model.themeStore))
            controller.view.frame = CGRect(x: 0, y: 0, width: 960, height: 640)
            controller.view.layoutSubtreeIfNeeded()

            let started = CFAbsoluteTimeGetCurrent()
            model.transientTurns = [
                .init(id: "pending-voice", role: "user", text: "Voice question"),
                .init(
                    id: "preview-1", role: "assistant",
                    text: String(repeating: "Streaming answer. ", count: 24)),
            ]
            model.turnActive = true
            model.statusText = "Working…"
            controller.view.needsLayout = true
            controller.view.layoutSubtreeIfNeeded()

            model.turns.append(contentsOf: model.transientTurns)
            model.transientTurns = []
            model.turnActive = false
            model.statusText = nil
            controller.view.needsLayout = true
            controller.view.layoutSubtreeIfNeeded()

            XCTAssertLessThan(
                CFAbsoluteTimeGetCurrent() - started, 2,
                "voice transcript replacement must not trap AppKit in lazy row placement")
        }
    #endif
}
