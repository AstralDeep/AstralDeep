# Several deployments on one host

Give each deployment its own folder, project name, front subnet and published port.

```toml
[node]
project = "second"
port = 8002
front_subnet = "172.31.248.0/24"
front_address = "172.31.248.10"
```

`up` picks a free project name, a free `/24` and writes them when it creates the file. A stopped deployment keeps its volumes, so its name is not reused. Two deployments cannot share a folder because the secrets folder sits beside the Compose file.
