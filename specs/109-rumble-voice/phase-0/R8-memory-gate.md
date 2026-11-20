# R8: the phone memory gate

**Question.** Is the 5.5 GB phone memory gate still right with a 1.85 GB peak and a possible extra speech model?

**Answer (provisional).** Keep 5.5 GB for now. The 1.85 GB figure is not the number the system's limit is judged on. It is a resident-set peak, and a separate lab run measured the same kind of stack at 0.49 to 0.69 GB of footprint on the same phone. The gate was sized for LFM3, which peaked at 2.66 GB of footprint. Once LFM3 is gone the gate is probably conservative, but the real choice is whether to admit 4 GB phones, and nothing measured so far covers a 4 GB phone. Another agent is measuring the voice candidates on the phone now (R12). Treat every conclusion here as provisional until those figures exist.

## The two measurements are different metrics

| Source | Stack | Peak | Metric | Tag |
|---|---|---|---|---|
| Pilot, iPhone 13 Pro, iOS 27.2.1 | Rumble, LFM2.5 and the iOS voice | 1,495,384,064 bytes (1.50 GB) | `ru_maxrss` | **measured** by the pilot run, `Results/ack-ios-20261119-141049.json` |
| Pilot, same phone | Rumble, LFM2.5 and Piper | 1,852,850,176 bytes (1.85 GB) | `ru_maxrss` | **measured**, `Results/ack-piper-20261119-141636.json` |
| Lab, iPhone 13 Pro, Metal | Rumble, 1.2B chat and Piper | 614 to 619 MB in four runs, 496 MB in a fifth | `phys_footprint`, 100 ms samples | **measured**, `VoiceTesting/Results/iPhone13Pro/*/rumble-1.2b-*/report.json`, key `peakFootprintBytes` |
| Lab, same phone | Rumble 350M chat and Piper | 691 to 693 MB | `phys_footprint` | **measured**, same folders, `rumble-*` |
| Lab, same phone | LFM3 Audio 2B 3-bit | 2.665 and 2.673 GB | `phys_footprint` | **measured**, same folders, `lfm-*` |
| Lab, same phone | Nemotron Bolt IQ3 | 1.22 and 1.27 GB | `phys_footprint` | **measured**, same folders, `bolt-*` |
| Lab, language runs on the same phone | Rumble, 1.2B chat | 492 to 495 MB | `phys_footprint` | **measured**, `Results/SixLanguages/iPhone13Pro`, `Results/Languages/iPhone13Pro` |

How each was taken (**read**):

- Pilot: `Sources/AcknowledgmentAudio.swift:53-54` calls `getrusage(RUSAGE_SELF)` and stores `ru_maxrss` as `peakResidentBytes` when the last acknowledgment clip is finished. `Scripts/measure_acknowledgments.py:178` copies it into the result as `peakResidentBytesAtFinalAcknowledgment`. It is the high-water resident size of the whole process since launch, so it includes the model load, any file-backed weight pages that were touched, and everything else in the pilot app. `INTEGRATION.md:175` repeats the two numbers as "peak resident memory at the tenth acknowledgment".
- Lab: `Sources/Inventory.swift:48-56` reads `phys_footprint` through `task_info`, and `Sources/Lab.swift:107-112` samples it every 100 ms and keeps the maximum. `README.md` in the lab says the same, and warns that "file-backed weights, allocator caches and system memory accounting can make process footprint differ from model bytes on disk."
- Both ran on a physical iPhone 13 Pro. The lab recorded `compute: "Metal (Piper uses CPU)"` and `cpuOnly: false`.

The system's per-app limit is judged on footprint, not resident size. Apple's `os_proc_available_memory()` returns "the current memory limit minus the memory footprint of your app" (**read**, <https://developer.apple.com/documentation/os/os_proc_available_memory>). So the lab's footprint column is the one to compare with a limit, and the pilot's 1.85 GB is not. The two should not be set side by side as if they disagreed. I cannot explain the whole gap from the files. File-backed weight pages that count toward resident size but not footprint are a likely part of it (**assumed**), and the pilot app is a larger program than the lab's. The R12 run should record `phys_footprint` so the number is comparable.

The lab's simulator figures (1.16 to 1.59 GB) come from the host CPU build and say nothing about the phone.

## The memory limit on each class of phone

Apple does not publish per-app memory limits. Every figure in this table is **assumed** unless it says otherwise, and the sources disagree.

| Class | Example | Foreground limit | Tag and source |
|---|---|---|---|
| 4 GB | iPhone 13, SE 3rd generation | About 2.1 GB on the iPhone 13 and 1.4 GB on the SE | **assumed**. A single blog reading of `os_proc_available_memory()` at launch plus resident memory: 2,099 MB and 1,384 MB. Two 4 GB phones differ by a third, so RAM alone does not predict the limit. <https://rorklab.net/en/articles/rork-dev/rork-jetsam-event-report-memory-limit-design> |
| 6 GB | iPhone 13 Pro | Above 2.67 GB | **measured** lower bound, from the lab: the LFM3 run peaked at 2.665 and 2.673 GB of footprint on this phone and finished with status "Replay finished" instead of being killed. The true limit is higher by an unknown amount. Roughly half of RAM, about 3 GB, is the usual community figure (**assumed**, no primary source) |
| 8 GB | iPhone 15 Pro and later | Not measured here | **assumed** higher than the 6 GB class. One third-party report loaded a model on an iPhone 17 Pro with the increased-memory-limit entitlement and saw a 4,651 MB resident peak. It is resident size, not footprint, and not a limit. <https://zenn.dev/mtfum/articles/ios_memory_entitlements?locale=en> |

Which phones have 4, 6 or 8 GB comes from Apple's public specifications and teardown reports, not from any file here (**assumed**). I did not check what `ProcessInfo.physicalMemory` returns on a 6 GB phone against the 5.5e9 constant (`OnDevice/VoiceDeviceSupport.swift:55`). That should be logged on the phone in the R12 run, because a 6 GB phone must report above 5.5e9 bytes for the gate to admit it.

## Is 5.5 GB still right?

The code: `VoiceDeviceSupport.swift:55-65` refuses a phone with less than 5,500,000,000 bytes of physical memory. It stops the 4 GB class and admits 6 GB and above. There is no step between those classes, so the real question is "admit 4 GB phones or not".

- The gate was built for LFM3, which needed 2.67 GB of footprint on the phone. That does not fit a roughly 2.1 GB limit, and it is a reason the gate exists.
- With LFM3 gone, the new stack measured 0.5 to 0.7 GB of footprint in the lab app. That would fit a 4 GB phone's limit with room to spare.
- The lab app is a bare test harness. The shipping app adds the mesh runtime, kernel, UI and chat history. The pilot app is closer to the real thing, and its resident peak of 1.85 GB is the figure that mixes file-backed pages in. The right comparison needs one run of the real app with the new runtime.
- A possible extra speech model adds to the total. Kokoro-82M peaked at 1.5 GB of process memory in one published Mac benchmark (spec section 7.4, no iPhone figure). Phonon 31m and Pocket TTS small are about 171 MB and 34 to 145 MB on disk (spec section 7.4).
- Cost of a wrong answer: a jetsam kill is silent during a conversation. Cost of too strict a gate: 4 GB phones keep the system voice. Until measured, keeping the gate costs less.

So: **keep 5.5 GB, do not raise it.** There is no evidence for raising it, since the new stack is lighter than LFM3 on the same 6 GB phone. Lowering it to admit 4 GB phones is a possible follow-up and needs the checks below.

## `os_proc_available_memory` and the increased-memory-limit entitlement

- `os_proc_available_memory()` (iOS 13 and later, **read**): advisory, cheap, returns the bytes the app may still allocate, and returns 0 if the process is over its limit. Apple says not to cache it and not to use it to maximize use. It is relevant here for two things: a runtime check before loading models, so a low reading makes the app fall back to the system voice instead of loading, and the measurements below.
- `com.apple.developer.kernel.increased-memory-limit` (iOS 15 and later, **read**, <https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.kernel.increased-memory-limit>): asks for a higher limit on supported devices only. Apple says to check with `os_proc_available_memory` and to behave correctly if more memory is not granted. The blog that cites it for "iPhone 11 and earlier" says Apple's documentation states the limit does not change on 4 GB phones. The page I fetched does not say that, so treat the claim as unconfirmed. The app does not declare the entitlement (`AstralApp-iOS.entitlements`), and neither does the pilot or the lab (no entitlements file in either project, and no mention in their `project.yml` or `Info.plist`). So every measurement above ran with the default limit.
- It is not needed to fit the new stack on 6 GB and above. It would matter only if the gate were lowered, and then it does not help the 4 GB class on Apple's own wording. Do not add it in this spec.

## Re-check when the voice candidates have been measured (R12)

Run on the iPhone 13 Pro, in the real app with Rumble, LFM2.5 and each candidate loaded, and record per run:

1. `phys_footprint` peak (not `ru_maxrss`), sampled at 100 ms through load, ten turns and an interrupted answer.
2. `os_proc_available_memory()` at launch, after load and at peak. Footprint plus that value gives the live limit, so the 6 GB class limit becomes a **measured** figure.
3. `ProcessInfo.physicalMemory` on the phone, against the 5.5e9 constant.
4. Memory warnings and the thermal state at the end.
5. The same run on a 4 GB phone if one can be borrowed. It is the only way to answer the lower-gate question.
6. Memory pressure: whether a warning arrives before a kill, and whether unloading 60 seconds after the overlay closes (spec section 7.3) returns the footprint to its starting value.

If the real-app peak with the heaviest candidate stays well below the 6 GB limit and the 4 GB limit is measured above it with margin, the gate can be revisited. If Kokoro-82M's footprint approaches the 6 GB figure on the phone, the candidate fails the spec's memory test rather than the gate being changed.

## Still unknown

- The foreground limit of every class on iOS 27.2. All non-lab figures above are assumed.
- Whether the lab's low footprint holds with Metal-backed weights in the real app.
- The 8 GB class's behavior. No 8 GB phone has been measured.
- Whether another agent's R12 figures change the picture. These conclusions were written before they exist.
