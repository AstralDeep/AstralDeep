# Workspaces and files

Each turn gets a scratch directory its tool calls share, and tools can return files to the caller through an outbox
inside it. This page covers both, and the disk limits that apply.

## The turn's workspace

The first sandbox call of a turn creates a workspace, a fresh directory under the sandbox's `/tmp`. Every confined
call of that turn sees it as `$TMPDIR` and `$HOME`, and it is the only place a call can write. So one call can save
an intermediate result and a later call in the same turn can read it:

```bash
# first bash call
grep -l heron /data/species/*.md > "$TMPDIR/hits.txt"
# a later call in the same turn
wc -l < "$TMPDIR/hits.txt"
```

- The workspace is deleted when the turn ends, whether it finished, failed, hit its deadline or was cancelled. Calls
  still running for the turn are stopped first.
- A call cannot read another turn's workspace, even one for the same user.
- If the executor cannot tell the sandbox a turn ended, the sandbox stops tracking the turn a minute after the agent's
  `deadline_seconds` and removes its workspace on the next call it receives.
- A call that arrives for a turn that has already ended is refused.
- The sandbox's `/tmp` is mounted `noexec`, so files in the workspace cannot be run directly; run scripts with their
  interpreter (`sh script.sh`, `python3 script.py`).
- The total size of every workspace in a sandbox is capped by the agent's `scratch` setting in `deployment.toml`
  (default `256m`).

## Disk limits

!!! note "New in 0.5.0"
    Per-turn and per-user disk limits.

Inside `scratch`, the sandbox limits how much one turn and one user may hold, so one turn cannot fill the space
other users share:

```toml
[[agents]]
id = "field-guide"
package = "../examples/field-guide"
[agents.sandbox]
scratch = "256m"
turn_disk = "64m"    # default: a quarter of scratch
user_disk = "128m"   # default: half of scratch
```

Sizes use Docker's form: a number with an optional `b`, `k`, `m` or `g` (powers of 1,024). `deploy` refuses
anything but `0 < turn_disk <= user_disk <= scratch`.

| Rule | Detail |
|---|---|
| Per file | No single file a call writes may grow past `turn_disk`. |
| Per turn | The turn's workspace may hold at most `turn_disk`. |
| Per user | All of one user's live turns on this agent together may hold at most `user_disk`. |
| What counts | Allocated blocks (sparse files cannot hide), plus 1 KB for every file or folder, plus files deleted but still held open by the turn's processes. A tree too deep to measure counts as over. |
| When it is checked | About twice a second while a call runs, and when it ends. A fast writer can briefly overshoot; `scratch` stays the hard ceiling. |
| What happens | Every call of the turn is stopped, its workspace is emptied, and the call's output names the limit. The turn can go on with an empty workspace. |

The sandbox's `/tmp` also has one inode per KB of `scratch`.

## Files from tools

An agent that lists file types can return files to the caller:

```toml
[output]
files = ["png", "csv"]   # any of png, svg, csv, pdf
```

A confined call writes files into `$VESSEL_OUTBOX`, a folder inside its workspace:

```bash
printf 'name,length_cm\nGreat Blue Heron,115\n' > "$VESSEL_OUTBOX/lengths.csv"
```

After each call the sandbox takes every entry out of the outbox. It keeps a file only if all of these hold:

| Check | Rule |
|---|---|
| Type | A regular file (not a link, folder or pipe) |
| Name | Starts with a letter or digit, then letters, digits, `.`, `_` or `-`, up to 100 characters, ending in a listed extension |
| Content | PNG and PDF must start with their signatures; SVG and CSV must be UTF-8 without NUL bytes, and SVG must contain `<svg` near the start |
| Size | At most 4 MB each |
| Count | At most 4 per call |

The executor checks every file again when it arrives.

### How files reach the caller

`POST /run` on the executor returns the kept files in a `files` list. Each entry has `name`, `media_type` and base64 `data`. The executor does not keep them.

| Extension | Media type |
|---|---|
| `png` | `image/png` |
| `svg` | `image/svg+xml` |
| `csv` | `text/csv` |
| `pdf` | `application/pdf` |

!!! warning "SVG can carry script"
    Show an SVG as an `<img>` or offer it as a download. Never insert it inline into a page.
