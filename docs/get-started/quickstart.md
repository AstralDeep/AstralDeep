# Quickstart

This guide builds a personal AstralDeep node, installs the AstralVessel command, and checks the running node.

## Requirements

- Python 3.11 or newer, Git, and Docker with Buildx and the Compose v2 plugin.
- A supported sandbox host: Linux x86_64 or arm64 with kernel 6.12 or newer and Landlock ABI 6; or an Apple silicon Mac with Docker Desktop whose Linux VM provides Landlock ABI 6. Docker Desktop 29.5 with a kernel 6.12 VM was checked; older versions may ship an older kernel. Windows with Docker Desktop is unverified. See [platform support](../security/platforms.md).
- An AstralDeep Keycloak issuer and client ID. Use the values configured for the AstralDeep app you use. See [realm setup](https://github.com/AstralDeep/AstralDeep/blob/main/docs/keycloak-realm-settings.md) and the relevant [Android and iOS client](https://github.com/AstralDeep/AstralDeep/blob/main/docs/keycloak-android-client-setup.md) or [Windows client](https://github.com/AstralDeep/AstralDeep/blob/main/docs/keycloak-windows-client-setup.md) guide.

## 1. Build the node image

The node image is built from the AstralDeep source and its pinned submodules. These commands use the [source commit qualified for this guide](https://github.com/AstralDeep/AstralDeep/commit/528467d1bd602ab3219a426ac9c82eda3b5bc4d5):

```bash
git clone https://github.com/AstralDeep/AstralDeep.git astraldeep
cd astraldeep
git checkout 528467d1bd602ab3219a426ac9c82eda3b5bc4d5
git submodule update --init --recursive
```

Build the pinned Kernel wheel and mailbox for the machine that will run Docker, then pass that directory as the named build context:

=== "Linux x86_64"

    ```bash
    python3 scripts/build_kernel_binding.py --root . --output ../kernel-artifacts --platform linux-x86_64
    docker build --platform linux/amd64 --build-context kernel-artifacts=../kernel-artifacts -t astralvessel-node:local .
    ```

=== "Linux arm64 or Apple silicon"

    ```bash
    python3 scripts/build_kernel_binding.py --root . --output ../kernel-artifacts --platform linux-aarch64
    docker build --platform linux/arm64 --build-context kernel-artifacts=../kernel-artifacts -t astralvessel-node:local .
    ```

The helper reads `config/astral-kernel-binding.json`, builds from its exact Kernel submodule commit, and checks the wheel and mailbox digests before the image build can use them. `astralvessel-node:local` is the default node image name.

## 2. Install AstralVessel

```bash
python3 -m pip install astralvessel
```

## 3. Start and check the node

Use your Keycloak realm URL as the issuer and the client ID configured for your AstralDeep app:

```bash
mkdir mynode && cd mynode
astralvessel up --issuer https://keycloak.example/realms/astral --client-id astral-mobile --example
```

`up` writes a private deployment file, starts the node and executor, waits for them to become ready, and runs the checks. The node listens on `127.0.0.1:8001` by default.

For commands that act as the owner, set a current access token issued for that owner by the configured issuer. The CLI reads it from the environment or a file and never prints it or accepts it as a command-line value:

```bash
export ASTRAL_OWNER_TOKEN="<current owner access token>"
astralvessel check
astralvessel ask "hello"
astralvessel conformance
```

See [CLI token handling](../reference/cli.md#the-owners-token) and [pairing](../run/pairing.md).

## Stop the node

```bash
astralvessel down
```

`down --volumes` also deletes the node's data, including its signing key.
