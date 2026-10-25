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

## Connector lifecycle and optional automatic startup

Tunnel configuration and DNS records persist, but public access requires a running `cloudflared` connector. The connector maintains the connection from your machine to Cloudflare; creating a tunnel or configuring ingress does not keep that process running. The machine must remain powered on and connected to the network.

**`make up` starts Postiz, not `cloudflared`. `make down` does not stop the tunnel.** The configuration helper above does not manage either lifecycle. Automatic connector startup is optional, user-level machine configuration—not a requirement for developers using only localhost and not a service installed by this repository.

Cloudflare error **1033** means Cloudflare cannot find a healthy connector for the requested tunnel. Check the connector and the domain's tunnel association before changing Postiz or recreating DNS. A connected tunnel alone does not prove that Postiz is running or healthy. See [Cloudflare's error 1033 guidance](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1033/).

### Align Postiz with the public URLs

For a two-hostname setup, route the frontend hostname to local port `4200` and the API hostname to the configured backend port (default `3000`). Set the corresponding literal values in ignored `.dev.env.local`, for example:

```dotenv
FRONTEND_URL=https://postiz.example.com
MAIN_URL=https://postiz.example.com
NEXT_PUBLIC_BACKEND_URL=https://postiz-api.example.com
NEXT_PUBLIC_OVERRIDE_BACKEND_URL=https://postiz-api.example.com
BACKEND_INTERNAL_URL=http://localhost:3000
```

Replace the example hostnames with your configured domains. Keep the internal URL local and consistent with your backend port. Retain the other private settings, including datastore destinations and the JWT secret. Effective Make/environment settings override this file, including explicitly empty values. Review the frontend's `allowedDevOrigins` in `apps/frontend/next.config.js` for your development hostname rather than assuming another developer's hostname applies to you.

Changing live application URLs requires an explicit `make down` followed by `make up`; coordinate that interruption first. Do not change databases or prepare schema merely to enable a tunnel. See the [development guide](development.md) for configuration precedence and startup requirements.

### Run the existing tunnel as a systemd user service

The following example is for Linux with a systemd user manager and an existing **locally managed named tunnel**. It assumes the current user can read the existing configuration and its referenced credential file. Do not copy credentials into the repository or service unit, and do not use this example unchanged for a token-managed tunnel.

Before enabling the service:

- Review every ingress route. A shared tunnel can expose other projects as well as Postiz; enabling it is not limited to the application currently being developed. Confirm the intended access controls before publishing development servers.
- Check for an existing user or system service and any manually launched connector. Do not overwrite an existing unit or stop an unidentified process. Coordinate a handover; after the new connector is verified, stop only the identified old connector that you intend to replace.
- Run `command -v cloudflared` to find the installed binary. The service does not inherit your interactive shell's PATH or version-manager setup.
- Check the existing linger policy with `loginctl show-user "$USER" -p Linger`; do not change it blindly because it affects other user services too.

Create `~/.config/systemd/user/cloudflared.service` only if it does not already exist. Replace `/absolute/path/to/cloudflared` and `EXISTING_TUNNEL_NAME_OR_UUID` below, and adjust the config path if needed. These are placeholders, not runnable defaults:

```ini
[Unit]
Description=Cloudflare Tunnel for development

[Service]
Type=simple
ExecStart=/absolute/path/to/cloudflared tunnel --config %h/.cloudflared/config.yml run EXISTING_TUNNEL_NAME_OR_UUID
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
UMask=0077

[Install]
WantedBy=default.target
```

`%h` expands to the user's home directory. Keep the unit and tunnel files private and ensure paths with spaces are properly quoted for systemd. Validate the edited unit, then enable it only when ready to activate all configured routes:

```sh
systemd-analyze --user verify "$HOME/.config/systemd/user/cloudflared.service"
systemctl --user daemon-reload
systemctl --user enable --now cloudflared.service
systemctl --user is-enabled cloudflared.service
systemctl --user is-active cloudflared.service
journalctl --user -u cloudflared.service -n 50 --no-pager
```

Treat journal output as private; redact sensitive information before sharing it. Verify registered tunnel connections and the intended public frontend/API responses. `active` only confirms process state, not connectivity or application health. Also check the development UI, browser console, and network requests; loading the auth page alone does not verify login or authenticated flows.

### Login startup versus boot startup: linger

Without linger, the user manager normally starts with the user's first login and stops after the last session ends, subject to system policy. The enabled service above follows that user manager; it is not tied specifically to opening a terminal or starting a graphical session.

With **linger enabled**, the user manager starts at boot and can remain running without a logged-in session. Consequently, this enabled tunnel service can start at boot and continue after logout. It still does not start Postiz: run `make up` separately when needed. `Restart=on-failure` restarts a failed process; it is not an application health check or a guarantee of uninterrupted public access.

If boot-time operation is desired and linger is not already enabled, deliberately enable it with:

```sh
loginctl enable-linger "$USER"
```

This may require administrator authorization. Obtain approval for the broader user-service lifecycle change; do not disable an existing linger setting just to control this tunnel. A user manager running at boot may also depend on home-directory availability for the binary, configuration, and credentials. Verify behavior after a subsequent reboot rather than treating `enabled` as proof of a completed boot test.

Useful lifecycle commands:

```sh
systemctl --user status cloudflared.service
systemctl --user restart cloudflared.service
systemctl --user stop cloudflared.service          # stop now; keep future automatic startup
systemctl --user disable --now cloudflared.service # stop now and disable automatic startup
```

Stopping or disabling a shared connector affects every route served by that connector, not only Postiz. These commands do not delete the tunnel, DNS records, configuration, or application data. Other connectors for the same tunnel may still serve traffic.

## Local fixture tests

Run only the focused synthetic test module:

```sh
python3 -m unittest scripts.dev.tests.test_cloudflare_config
```

The tests use a temporary config and fake `cloudflared` executable. They do not access the user's Cloudflare configuration, credentials, live services, or DNS.
