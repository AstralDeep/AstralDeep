# The egress

The egress is optional. Add it when you want the model key to stay out of the node.

```toml
[egress]
upstream_url = "https://api.example.com/v1"
upstream_key = "env:VESSEL_UPSTREAM_MODEL_KEY"
```

The egress is the only container that holds the key. The node joins the `egress` network and can call `http://egress:8080/v1` with the egress token, which is derived from `VESSEL_SANDBOX_TOKEN` and written to `.generated/egress.env`. The egress refuses any caller without it.

The node's own provider settings stay in the product, where they are encrypted. The egress does not change that.

The egress also runs a filtered web proxy on port 3128. It allows only globally routable addresses on ports 80 and 443.

Without `[egress]` no egress container runs.
