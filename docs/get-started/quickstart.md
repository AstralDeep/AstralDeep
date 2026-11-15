# Quickstart

This guide builds a personal AstralDeep node, installs the AstralVessel command, and checks the running node.

## Requirements

- Python 3.11 or newer, Git, and Docker with Buildx and the Compose v2 plugin.
- A supported sandbox host: Linux x86_64 or arm64 with kernel 6.12 or newer and Landlock ABI 6; or an Apple silicon Mac with Docker Desktop whose Linux VM provides Landlock ABI 6. Docker Desktop 29.5 with a kernel 6.12 VM was checked; older versions may ship an older kernel. Windows with Docker Desktop is unverified. See [platform support](../security/platforms.md).
- An AstralDeep Keycloak issuer and client ID. Use the values configured for the AstralDeep app you use. See [realm setup](https://github.com/AstralDeep/AstralDeep/blob/main/docs/keycloak-realm-settings.md) and the relevant [Android and iOS client](https://github.com/AstralDeep/AstralDeep/blob/main/docs/keycloak-android-client-setup.md) or [Windows client](https://github.com/AstralDeep/AstralDeep/blob/main/docs/keycloak-windows-client-setup.md) guide.

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

Use your Keycloak realm URL as the issuer and the client ID configured for your AstralDeep app. The example uses `astral-mobile`:

```bash
cd ..
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
