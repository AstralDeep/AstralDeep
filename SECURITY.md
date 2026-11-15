# Security policy

AstralVessel runs model-chosen tool calls inside a sandbox, so we treat isolation bugs as the most serious kind.

## Supported versions

Security fixes go into the latest release only. AstralVessel is 0.x, so upgrade to the newest version to receive
them.

## Reporting a vulnerability

Please do not open a public issue. Report it privately through GitHub: open the repository's **Security** tab and
choose **Report a vulnerability**. Include the version, your deployment settings with secrets removed, and the
steps that reproduce the problem.

We will acknowledge the report, keep you updated, and credit you in the changelog unless you ask us not to.

## In scope

- Escaping a tool call's sandbox, or reaching files, processes or the network a call should not see
- One turn, user or agent reaching another's workspace, history or files
- Bypassing the egress proxy's address checks, or reading the model key from outside the egress container
- Forging or bypassing a ticket, the executor's checks or the owner's identity, or widening an agent's tools beyond what a ticket grants
- Secrets or user data leaking into logs, errors or responses

Denial of service against your own deployment within its configured limits is out of scope, but reports of a limit
that does not hold are welcome.
