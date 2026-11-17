# Configuration tables

Every field of `vessel.toml`, declared tool files and `deployment.toml`, with its type, default and constraints.
These tables are generated from AstralVessel's pydantic models when the site is built. Unknown keys are refused in
every table. For what each setting does, see [vessel.toml](../build/vessel-toml.md) and
[deployment.toml](../run/deployment-toml.md).

## vessel.toml

### Manifest

The top level of `vessel.toml`.

<!-- config: astralvessel.manifest.Manifest -->

### AgentSection

`[agent]`

<!-- config: astralvessel.manifest.AgentSection -->


### ToolsSection

`[tools]`

<!-- config: astralvessel.manifest.ToolsSection -->

### SandboxSection

`[sandbox]`

<!-- config: astralvessel.manifest.SandboxSection -->

### Mount

An entry of `[sandbox] mounts`.

<!-- config: astralvessel.manifest.Mount -->

### OutputSection

`[output]`

<!-- config: astralvessel.manifest.OutputSection -->

## Declared tool files

### DeclaredTool

<!-- config: astralvessel.manifest.DeclaredTool -->

## deployment.toml

### DeploymentFile

The top level of `deployment.toml`.

<!-- config: astralvessel.deployment.DeploymentFile -->


### EgressSection

`[egress]`

<!-- config: astralvessel.deployment.EgressSection -->

### NodeSection

`[node]`

<!-- config: astralvessel.deployment.NodeSection -->

### OwnerSection

`[owner]`

<!-- config: astralvessel.deployment.OwnerSection -->

### ExecutorSection

`[executor]`

<!-- config: astralvessel.deployment.ExecutorSection -->


### AgentEntry

`[[agents]]`

<!-- config: astralvessel.deployment.AgentEntry -->

### SandboxProfile

`[agents.sandbox]`

<!-- config: astralvessel.deployment.SandboxProfile -->

### MountGrant

An entry of `[agents.sandbox] mounts`.

<!-- config: astralvessel.deployment.MountGrant -->



