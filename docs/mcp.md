# MCP setup with Pi

This guide configures Pi to connect to the Postiz development MCP endpoint. It does not authorize production access or write operations.

## Configure the development server

Configure the ignored, project-local `.pi/mcp.json` from `.pi/mcp.json.dist` if needed, preserving existing entries such as `chrome-devtools`. The Postiz entry uses the literal local backend URL `http://127.0.0.1:3000/mcp` and a Pi `!command` Authorization header that reads `~/.config/postiz/dev-api-key` only when the configuration resolves. Both local config and tracked template contain the same command; the template contains no credential. Pi 1.4.0 evaluates command-valued headers whenever headers are resolved, so the same command can read an updated credential file without a process restart. `/reload` loads changed MCP configuration and reconnects the servers. It does not interpolate environment variables in URLs or server arguments.

## Store an existing development key securely

Use only the existing API key belonging to the `puyu` organization in the isolated development database. Retrieve it via your approved local, read-only development workflow; verify the organization name before use. Do not rotate/generate a key, query another organization, copy production credentials, or send this key to a remote host. If the key is not available from the approved local DB, stop rather than substituting another credential.

Store the key as plaintext in a private file outside the repository. The configured path is `~/.config/postiz/dev-api-key`; the containing directory must be mode `700`, and the file mode `600`. Treat it as a credential: never paste it into chat, shell command arguments, shell history, logs, screenshots, or repository files. Example interactive setup after retrieving the key through the approved secure method:

```sh
install -d -m 700 ~/.config/postiz
umask 077
read -rsp 'Existing puyu development API key: ' key; printf '\n'
printf '%s\n' "$key" > ~/.config/postiz/dev-api-key
unset key
chmod 600 ~/.config/postiz/dev-api-key
```

The example writes via shell redirection; ensure the target does not already exist and is not a symlink before running it. Prefer an exclusive-create secure file writer if available. Do not overwrite an existing unexpected credential. This file is unencrypted plaintext; host account security and backups remain your responsibility. `.pi/mcp.json` is ignored, and `.pi/mcp.json.dist` must never contain secrets or machine-specific values.

The Authorization command fails closed when the file is missing, unreadable, or empty; it emits only `Bearer` plus the key on success. Correct permissions/path or retrieve the approved key; the next header resolution reads the current file contents. Run `/reload` in an existing Pi session to load any changed MCP configuration and reconnect, then inspect `/mcp` for connection state. Command output is not cached for the process lifetime; changing only the credential file does not itself require `/reload`. The command does not depend on `POSTIZ_API_KEY`, inherited parent-shell environment, or dotenv loading.

Pair the credential only with its matching endpoint/environment. This setup targets loopback development `http://127.0.0.1:3000/mcp`; do not change it to a remote frontend or production URL. The remote development frontend is `postiz.socamaru.site` and its backend is `postiz-api.socamaru.site`; neither is this local endpoint. If you intentionally configure another environment, obtain and confirm that environment's own credential separately; never reuse the local development key across environments. The Chrome DevTools MCP entry and setup remain independent and unchanged.

## Validate conservatively

Ensure the private credential file is available, then run `/reload` in the existing Pi session to load the MCP configuration and reconnect. No API-key environment variable is needed. Inspect `/mcp` for the Postiz server and its available tools, and perform only non-mutating checks such as listing available accounts or reading existing data when authorized. Do not publish, schedule, delete, or otherwise mutate content as a connection test. A server appearing in `/mcp` alone does not establish that permissions, organizational isolation, or every operation are correct. Stop if the endpoint, identity, or returned organization is unexpected; report errors without disclosing credentials.

Available tools and permissions depend on the deployed backend and the API key's grants. Pi Apps are separate from MCP tools: this setup does not establish that Apps are installed, available, or supported. Verify capabilities before relying on them.
