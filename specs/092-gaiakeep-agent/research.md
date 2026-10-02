# Research: GaiaKeep integration (2026-10-02)

## Sources

- GaiaKeep/docs eafd200ec8cda9ac791b82612deb8704f2d8a987; public release docs reference older gfs 8461e1a.
- GaiaKeep/gfs 02f7d83cc92db50968d4076da8afad2cc7a811b7, branch1.3: [capabilities](https://github.com/GaiaKeep/gfs/blob/02f7d83cc92db50968d4076da8afad2cc7a811b7/src/main/java/io/cresco/gfs/ExecutorImpl.java), [client](https://github.com/GaiaKeep/gfs/blob/02f7d83cc92db50968d4076da8afad2cc7a811b7/clients/README.md), [transport](https://github.com/GaiaKeep/gfs/blob/02f7d83cc92db50968d4076da8afad2cc7a811b7/clients/python/gaiakeep/transport.py), [dashboard expectation](https://github.com/GaiaKeep/gfs/blob/02f7d83cc92db50968d4076da8afad2cc7a811b7/eval/gfs_dashboard_check.py).

## Decisions

- Decision: use the existing SSH tunnel; do not install fabric or create a Gaia instance. Rationale: explicit owner steering. Alternatives: original Cresco infrastructure setup is superseded.
- Decision: wrap the exact optional private SDK with an Astral-owned verified transport. Rationale: supported signing/GKT/integrity/leader semantics while refusing SDK's unverified dataplane option. Alternatives: copying private source conflicts with its distribution boundary; an independent protocol implementation would duplicate security-critical transfer logic.
- Decision: separate SSH auth, fabric service auth, and signed Gaia principal auth. Rationale: SSH credentials only provide host access; system-admin does not imply tenant data access.
- Decision: explicit per-action tools, conservative mutations, operator-disabled legacy compatibility. Rationale: the 111 declarations include 90 core and21 prototype/common actions; operation classes are timing categories, not safety categories. Unannotated node/consensus actions remain excluded.
- Decision: preserve exact upstream limitations. Rationale: legalorder action=shorten collides with the native action slot; never silently convert it to erase. The Linux SDK uses pread/pwrite, requiring Linux file qualification.
- Decision: bind core.put/get/haveopen/have to complete SDK workflows, retaining 111 declarations with 109 tool surfaces. Direct RPCs cannot complete the encrypted session. Have publication supports expected_head but not base_vid, verified in client.py and have.py at the pin.
- Decision: refuse legacy fetch before connection. The packaged SDK has no receiver; the evaluation harness uses an older format with unsafe fallbacks. Future qualification needs admitted resolve-origin routing, activation before RPC, exact 8-character transfer and 6-character sequence framing, contiguous bounded bytes, per-chunk and whole-file SHA256, and verified TLS. Sources: [receiver](https://github.com/GaiaKeep/gfs/blob/02f7d83cc92db50968d4076da8afad2cc7a811b7/eval/gfs_client.py#L298), [publisher](https://github.com/GaiaKeep/gfs/blob/02f7d83cc92db50968d4076da8afad2cc7a811b7/src/main/java/io/cresco/gfs/PublisherEngine.java).

## Existing deployment observations

Authenticated sandbox SSH and host-key-verified DGX SSH through sandbox localhost:40000 reached slogin-02. Astral health/readiness passed. The existing dashboard on DGX localhost:8900 returned a mesh snapshot; /api/storage returned404 and the snapshot had no storage model. Authenticated verified-TLS wsapi inventory reported only the global region/agent and no gfs plugin; DGX11–13 appeared in link telemetry but their queried global-side plugin inventories were empty. This does not prove absence on every regional agent. Direct compute-node inspection was refused by strict host-key checking. No server/service/instance was installed, started, stopped or changed. Owner believes the interface is pending and requests agent work now.
