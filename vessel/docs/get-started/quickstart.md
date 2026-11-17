# Quickstart

This guide installs the AstralVessel command, starts a personal AstralDeep node from a node pack, and checks it. No container engine is involved.

## Requirements

- Python 3.11 or newer.
- macOS on Apple silicon, or Linux with kernel 6.12 or newer and unprivileged user namespaces. See [platform support](../security/platforms.md) for what each host gives you.
- The AstralDeep app on a device you already use, to approve the node into your mesh.

## 1. Install AstralVessel

```bash
python3 -m pip install astralvessel
```

## 2. Start and check the node

```bash
mkdir mynode && cd mynode
astralvessel up --example
```

`up` installs the release's pinned node pack, writes a private deployment file, starts the node, the executor and the example agent's sandbox, waits until they answer, and runs the checks. The node listens on `127.0.0.1:8001` by default.

A release that pins no pack asks you to name one. Pass the pack's descriptor and its digest:

```bash
astralvessel up --example --bundle https://example.test/node-pack.json --bundle-sha256 <digest>
```

A pack on your own disk needs no digest. Give `--bundle` the folder that holds `node-pack.json`.

`up` keeps the processes running with systemd user units on Linux and launchd agents on macOS. Add `--foreground` to run them in the terminal instead, which suits a container or a shell over SSH. Press Ctrl-C to stop them.

On Linux, user services stop when you log out. `up` prints the one command an administrator runs to change that.

## 3. Pair the node

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

`down` keeps your data. `down --volumes` deletes the node's data under `.runtime/node-data`, including its signing key.

## Container mode

The steps below build and run the Docker deployment that earlier releases used. Skip them for a user install. They apply when your deployment file says `mode = "container"` under `[runtime]`.

Container mode needs Docker with Buildx and the Compose v2 plugin, and a Linux kernel with Landlock ABI 6. See [platform support](../security/platforms.md#container-mode).

### 1. Prepare the node build inputs

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

### 2. Build the node image

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

### 3. Install AstralVessel

```bash
python3 -m pip install astralvessel==0.7.0
```

### 4. Start and check the node

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
