# Quickstart

This guide builds a personal AstralDeep node, installs the AstralVessel command, and checks the running node.

## Requirements

- Python 3.11 or newer, Git, and Docker with Buildx and the Compose v2 plugin.
- A supported sandbox host: Linux x86_64 or arm64 with kernel 6.12 or newer and Landlock ABI 6; or an Apple silicon Mac with Docker Desktop whose Linux VM provides Landlock ABI 6. Docker Desktop 29.5 with a kernel 6.12 VM was checked; older versions may ship an older kernel. Windows with Docker Desktop is unverified. See [platform support](../security/platforms.md).
- The AstralDeep app on a device you already use, to approve the node into your mesh.

## 1. Prepare the node build inputs

The AstralVessel 0.7.0 executor image contains the exact, verified Kernel artifacts used to build the matching Deep node image. Use it only to copy those files; it is not the node image and must not be passed to `astralvessel up --image`.

```bash
mkdir astral-install && cd astral-install
docker pull ghcr.io/astraldeep/astralvessel:0.7.0
artifact_container="$(docker create ghcr.io/astraldeep/astralvessel:0.7.0)"
mkdir kernel-artifacts
docker cp "${artifact_container}:/opt/vessel-build/kernel-artifacts/." ./kernel-artifacts/
docker rm "$artifact_container"
```

`docker create` does not start the executor. The release image is public; this step needs no registry login.

## 2. Build the node image

Clone the qualified AstralDeep source and initialize its pinned submodules:

```bash
git clone https://github.com/AstralDeep/AstralDeep.git astraldeep
cd astraldeep
git checkout 67519cba8964cb77335642bd7fdb5871f996547a
git submodule update --init --recursive
```

Build for the machine that will run Docker. The named build context points to the artifacts copied in the previous step:

=== "Linux x86_64"

    ```bash
    docker build --platform linux/amd64 --build-context kernel-artifacts=../kernel-artifacts -t astralvessel-node:local .
    ```

=== "Linux arm64 or Apple silicon"

    ```bash
    docker build --platform linux/arm64 --build-context kernel-artifacts=../kernel-artifacts -t astralvessel-node:local .
    ```

Deep's image build checks the wheel and mailbox against its pinned manifest and fails if either is missing or differs. `astralvessel-node:local` is the default node image name.

## 3. Install AstralVessel

```bash
python3 -m pip install astralvessel==0.7.0
```

## 4. Start and check the node

```bash
cd ..
mkdir mynode && cd mynode
astralvessel up --example
```

`up` writes a private deployment file, starts the node and executor, waits for them to become ready, and runs the checks. The node listens on `127.0.0.1:8001` by default.

Pair the node into your mesh with no sign-in, then approve it on a device you already use:

```bash
astralvessel pair
```

To ask the node a question from a script, create a holder key, give its public form to the owner, and save the caller ticket they mint for this node to a file. The private key stays in a 0600 `caller-key` file and is never printed:

```bash
astralvessel caller key
astralvessel check
astralvessel ask "hello" --ticket-file ticket.txt
astralvessel conformance --ticket-file ticket.txt
```

See [the caller ticket](../reference/cli.md#the-caller-ticket) and [pairing](../run/pairing.md).

## Stop the node

```bash
astralvessel down
```

`down --volumes` also deletes the node's data, including its signing key.
