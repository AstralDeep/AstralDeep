# Local-state continuity inventory

Inventory captured on 2026-08-13 before repository replacement work began. Only path metadata was inspected: existence, ignored disposition, type, file count, and byte size. No file contents were read.

## Preserved ignored state

| Repository | Path | Type | Files | Bytes | Ignore source | Continuity rule |
|---|---|---:|---:|---:|---|---|
| LETS | `paper/submission/` | directory | 91 | 24,372,962 | `.gitignore:67` | Preserve in place; never clean, stage, copy as product source, or overwrite during the migration. |
| LETS | `results/generated/` | directory | 313 | 102,183,811 | `.gitignore:61` | Preserve in place; use only through the later evidence protocol. |
| AstralPlane | `app/app.db` | file | 1 | 24,576 | `.gitignore:8` | Preserve in the original checkout; never copy into the replacement worktree. |
| AstralPlane | `app/logs/app.log` | file | 1 | 2,610,969 | `.gitignore:6` | Preserve in the original checkout; never stage or copy. |
| AstralPlane | `app/logs/app.log.1` | file | 1 | 10,485,732 | `.gitignore:6` | Preserve in the original checkout; never stage or copy. |
| AstralPlane | `app/logs/error.log` | file | 1 | 2,250,360 | `.gitignore:6` | Preserve in the original checkout; never stage or copy. |
| AstralPlane | `app/logs/websocket_detail.log` | file | 1 | 0 | `.gitignore:6` | Preserve in the original checkout; never stage or copy. |
| AstralPlane | `app/logs/websocket.log` | file | 1 | 283,041 | `.gitignore:6` | Preserve in the original checkout; never stage or copy. |
| AstralPlane | `logs/app.log` | file | 1 | 24,474 | `.gitignore:5` | Preserve in the original checkout; never stage or copy. |
| AstralPlane | `logs/error.log` | file | 1 | 16,398 | `.gitignore:5` | Preserve in the original checkout; never stage or copy. |
| AstralPlane | `logs/websocket_detail.log` | file | 1 | 0 | `.gitignore:5` | Preserve in the original checkout; never stage or copy. |
| AstralPlane | `logs/websocket.log` | file | 1 | 0 | `.gitignore:5` | Preserve in the original checkout; never stage or copy. |
| AstralProjection | `node_modules/` | directory | 12,335 | 104,571,025 | `.gitignore:10` | Preserve in the original checkout; create the replacement in a fresh worktree. |
| AstralDeep | `android-client/keystore.properties` | file | 1 | 158 | `android-client/.gitignore:15` | Keep the original in place. Copy without reading or logging only after the Projection Android ignore rules exist. |
| AstralDeep | `android-client/local.properties` | file | 1 | 349 | `android-client/.gitignore:3` | Keep the original in place. Copy without reading or logging only after the Projection Android ignore rules exist. |

The two Android property files are present, untracked, and ignored. Removing the original `android-client/` source directory is prohibited until both files have been copied to the Projection worktree, independently confirmed ignored there, and the signed-build continuity check has passed. The source files remain the recovery copy throughout extraction.

## Post-restart availability

On 2026-08-14, after the workstation restart, the recorded `Y:` volume was unavailable and the current LETS checkout did not contain `paper/submission/`, `results/generated/`, or `results/astraldeep-case-study/`. No manuscript or result directory was reconstructed, synthesized, or overwritten. Tasks that require those ignored artifacts remain blocked until the original storage is restored or the owner explicitly authorizes a separate reconstruction procedure.

## Safety boundary

- Replacement work occurs only in fresh Plane and Projection worktrees.
- No cleanup command may run in the original LETS, Plane, Projection, or Deep checkout.
- Migration commits stage explicit tracked paths and must pass the staged-path denylist.
- File counts and sizes are a continuity aid, not evidence that ignored data belongs in Git.
