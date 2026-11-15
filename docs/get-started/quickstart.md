# Quickstart

This installs a personal node with the example agent. It takes one image build and one command.

## 1. Get a node image

The node is an AstralDeep image. Build it from the AstralDeep repository with the kernel artifacts that Deep pins in `config/astral-kernel-binding.json`:

```bash
docker build --build-context kernel-artifacts=<folder with the kernel wheel and mailbox> -t astralvessel-node:local .
```

`astralvessel-node:local` is the default image name. To use another image, pass `--image` or set `image` in the `[node]` section.

## 2. Install the command

```bash
pip install astralvessel
```

From a checkout, use `pip install -e .`. A checkout builds the executor and sandbox images locally. The build needs the pinned kernel wheel and fails without it. Point the deployment at the folder that holds it:

```toml
[executor]
kernel_artifacts = "/path/to/kernel-artifacts"
```

## 3. Start the node

```bash
mkdir mynode && cd mynode
astralvessel up --issuer https://keycloak.example.com/realms/astral --client-id astral-app --example
```

`up` writes `deployment.toml` and a private `.env`, writes the Compose file, starts the containers, waits for the node and the sandboxes, and runs the checks. It prints `ready` when they pass.

The node listens on `127.0.0.1:8001`. Pass `--port` to change that.

## 4. Check it and talk to it

```bash
astralvessel check
export ASTRAL_OWNER_TOKEN=<an access token for the owner>
astralvessel ask "hello"
astralvessel conformance
```

`ASTRAL_OWNER_TOKEN` must be a current token from the issuer you named. The command never prints it and never takes it as an argument. Use `--token-file` to read it from a file.

## 5. Add the node from the app

```bash
astralvessel pair
```

See [Pairing](../run/pairing.md).

## Stop it

```bash
astralvessel down
```

`down --volumes` also deletes the node's data, including its signing key.
