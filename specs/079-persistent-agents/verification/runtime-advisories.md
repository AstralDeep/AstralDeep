# Runtime Python advisory assessment

The scanner found **six advisory records, representing four unique advisories in two packages**. These findings remain open for dependency triage. This report grants no exception, alert dismissal, or release approval.

The complete 145-package version, METADATA hash and Requires-Dist inventory and
Python 3.11.16 identity were subsequently verified identical in runtime images
from `9913610f` and `7c8cd1b7`. The final `7c8cd1b7` image is
`sha256:12d4cf52c4bebfd8160ce5514c3f46381dafb35b9172303218d13822e2b089f6`.
Its reuse report is `build/079/verification/runtime-advisories/reuse-7c8cd1b7.json`,
SHA-256 `747d29efdc81e0dfae382b027d96d521c7c2f056339c7891113ad0052725b3d2`.
This binds the same package findings to that exact image; it does not expand
the original scan's coverage or clear any advisory.

The assessed image is `astraldeep:079-runtime-0dfc768f`, immutable ID `sha256:27d16f52dd6ed6c021714c8aee07f749ecff06fc0e38517d4284edaf6eddd0c8`, created `2026-09-05T22:22:44.222990963Z`. It contains Python 3.11.16 and 145 installed distributions. Four first-party distributions were excluded before external queries. Of 141 public inputs, 140 were audited; PyPI could not audit the public spaCy model `en-core-web-lg==3.8.0`.

| Installed dependency | Primary advisory | Applicability and observed application use |
| --- | --- | --- |
| cryptography 48.0.1 | [CVE-2026-69247](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5), moderate; fixed in 50.0.0 | Affected version. Requires applications to decrypt attacker-supplied PKCS#7 EnvelopedData and expose an adaptive oracle. No affected decryption calls found in image application Python or other installed package Python. Linked OpenSSL is 4.0.1; upstream describes implicit rejection on OpenSSL 3.2+, but that is not treated as clearing this advisory. |
| cryptography 48.0.1 | [CVE-2026-69248](https://github.com/pyca/cryptography/security/advisories/GHSA-m2h6-j472-rp4c), low; fixed in 49.0.0 | Certificate verifier can accept an overly broad wildcard. Scanner flags 48.0.1, but upstream lists affected versions <=48.0.0 and patched >=49.0.0. Applicability needs upstream clarification; no affected verifier calls found in checked application or other installed package Python. |
| cryptography 48.0.1 | [CVE-2026-69249](https://github.com/pyca/cryptography/security/advisories/GHSA-jwv3-5hgf-82ww), low; fixed in 49.0.0 | Duplicate certificate intermediates can amplify verification work. Same version-range discrepancy and source-search result as above. |
| ecdsa 0.19.2 | [CVE-2024-23342](https://github.com/tlsfuzzer/python-ecdsa/security/advisories/GHSA-wj6h-64fc-37mp), high; no upstream fix | Affects this library's signing, key generation, and ECDH timing; verification is unaffected. Installed `python-jose` selects `jose.backends.cryptography_backend` for EC and RSA. Checked application JWT callers restrict RS256, and application ECDH uses cryptography. No direct application import of ecdsa was found. |

Both affected packages are production dependencies, not merely test tools. The installed `presidio-anonymizer==2.2.364` requires `cryptography>=48.0.1,<49.0.0`, blocking a simple upgrade to the versions listed as patched. `python-jose==3.5.0` unconditionally requires ecdsa. Removing a required package or forcing an incompatible crypto version would need a separately qualified dependency change. The [cryptography changelog](https://cryptography.io/en/latest/changelog/) also records compatibility changes in 49.0.0; no broad upgrade was attempted.

Installed tooling was included in the audit: pytest 9.1.1, pytest-asyncio 1.4.0, setuptools 84.0.0, wheel 0.48.0, and pip 26.2.1 had no matches in this scan. The image's installed setuptools version is 84.0.0; the separately pinned component build requirement is not evidence of the final runtime version.

The source inspection supports **no demonstrated affected application path**, not a proof of unreachability. Dynamic imports, native extension paths, generated code, operating-system packages, native-library advisories, and model-file advisories were not exhaustively analyzed. No OS scanner was run. No runtime, credentials, environment file, database, package manifest, or installed image contents were changed.

## Reproduction and evidence

Inventory and API inspection ran in ephemeral containers using `--network none --read-only`, no host mounts, and a bounded tmpfs. The retained `read_only_reachability.py` emits package/API facts and source hashes only. It can be supplied on stdin to:

```text
docker run -i --rm --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m --entrypoint python sha256:27d16f52dd6ed6c021714c8aee07f749ecff06fc0e38517d4284edaf6eddd0c8 -
```

The scanner was installed and run only through isolated uv tooling in a disposable test container, against exact public installed pins, with no dependency resolution or target installation:

```text
docker exec astraldeep-079-tests timeout 240 uv tool run --isolated pip-audit==2.10.1 -r /workspace/build/079/verification/runtime-advisories/public-installed-requirements.txt --no-deps --disable-pip --progress-spinner off --format json --desc on --output /workspace/build/079/verification/runtime-advisories/pip-audit.json
```

Exit 1 is the expected vulnerability-found result. `pip-audit.json` retains unmodified scanner records, including duplicate aliases; `report.json` deduplicates by CVE without suppressing records. `image-python-inventory.json` preserves every installed distribution version, dependency metadata, and metadata hash. `image-reachability.json` preserves exact image API selections, OpenSSL versions, source witnesses, and public composition hashes. `scanner-identity.json` records scanner/tool metadata identities. `report-digests.json` binds retained artifacts by SHA-256. Findings were checked against the linked primary maintainer advisories on 2026-09-05.
