# Optional existing Cloudflare Tunnel configuration

`scripts/dev/configure_cloudflare_tunnel.py` is an independent, opt-in helper for adding one public hostname to an **already-created** named Cloudflare Tunnel configuration. It edits local ingress configuration; it does not start or stop `cloudflared`, start/check Postiz or containers, create tunnels, install/login to Cloudflare, manage credentials, or modify Make startup. The helper reads only the config path you explicitly pass; it does not open `.env` or credential files itself. The optional DNS command delegates to `cloudflared`, which can use its already-configured authentication. The repository tests use only synthetic fixtures and fake commands.

## Preconditions and supported format

Use a current, authenticated `cloudflared` CLI and an existing config containing exactly one plain top-level `tunnel: <name-or-UUID>` setting and exactly one top-level `ingress:` list. The helper's deliberately narrow editable ingress subset is:

```yaml
ingress:
  - hostname: existing.example.com
    service: http://127.0.0.1:4200
  - service: http_status:404
```

Each non-fallback rule must contain exactly a plain `hostname` and `service`; the final and only hostless rule must be `service: http_status:404`. The helper preserves all text outside its inserted rule, but rejects unsupported ingress constructs, fields, duplicate/complex YAML, quoted values, hostless shadowing rules, missing/duplicate fallback, and conflicting hostname rules rather than attempting a lossy rewrite. The cloudflared validator is also run against the candidate. Review your configuration yourself; this is not a general-purpose YAML editor. Existing backup files are never overwritten.

## Review, then apply

The first invocation validates a candidate without writing the config or DNS:

```sh
python3 scripts/dev/configure_cloudflare_tunnel.py \
  --config /path/to/config.yml \
  --tunnel EXISTING_TUNNEL_NAME_OR_UUID \
  --hostname postiz.example.com \
  --port 4200
```

Review the requested hostname, local port, current config and proposed route. If correct, add `--apply` to write the validated candidate. The helper first creates `<config>.bak` exclusively with mode `0600`, then atomically replaces the config while retaining its existing permission bits. If the backup already exists, it stops without replacing it; choose and review a different config copy/path rather than deleting a backup blindly. A config symlink is rejected. A repeated identical request is idempotent; a conflicting existing hostname fails closed. Only `127.0.0.1:<port>` origins are generated.

The helper only validates Cloudflare ingress syntax; it does not check whether anything is listening on the port or whether the app is healthy or publicly reachable. You must separately ensure the intended service, authorization, and exposure policy are appropriate. The command makes no firewall, access-policy, or application-authentication changes.

## Optional DNS route

DNS is a separate explicit action and requires both confirmation flags, in addition to `--apply`:

```sh
python3 scripts/dev/configure_cloudflare_tunnel.py \
  --config /path/to/config.yml \
  --tunnel EXISTING_TUNNEL_NAME_OR_UUID \
  --hostname postiz.example.com \
  --port 4200 \
  --apply --create-dns --confirm-dns
```

The helper invokes only `cloudflared tunnel route dns <tunnel> <hostname>`; it never passes the overwrite option. The upstream Cloudflare CLI declares `--overwrite-dns` as the flag that “Overwrites existing DNS records with this hostname” and passes its false-by-default value into the DNS route request (`overwrite_existing: false`). See the [upstream CLI source](https://github.com/cloudflare/cloudflared/blob/master/cmd/cloudflared/tunnel/subcommands.go) and [DNS route request source](https://github.com/cloudflare/cloudflared/blob/master/cfapi/hostname.go). Use a cloudflared version whose behavior you have checked; the helper does not independently inspect DNS, and a successful CLI request is not proof of propagation or reachability. Cloudflare can reject an existing conflicting record; inspect and resolve that conflict deliberately in Cloudflare rather than asking this script to overwrite it.

DNS is attempted only after config apply. If DNS routing fails, the validated config remains applied with its backup; the helper reports that partial result and does not silently roll it back or delete DNS. Inspect the config and DNS manually. Do not treat fixture tests as verification of real credentials, Cloudflare permissions, existing DNS state, propagation, or service availability.

## Local fixture tests

Run only the focused synthetic test module:

```sh
python3 -m unittest scripts.dev.tests.test_cloudflare_config
```

The tests use a temporary config and fake `cloudflared` executable. They do not access the user's Cloudflare configuration, credentials, live services, or DNS.
