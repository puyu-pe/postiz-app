# Chrome DevTools MCP

From the repository root, create your local configuration:

```sh
cp .pi/mcp.json.dist .pi/mcp.json
```

Edit `.pi/mcp.json` and set `--userDataDir` to the Chrome user data directory you want to use. The template path is an example for Chrome Flatpak on Linux (`~/.var/app/com.google.Chrome/config/google-chrome`); adjust it for a native installation or another machine. Pi expands `~` only at the beginning of an argument. Your local `.pi/mcp.json` is ignored by Git.

Requirements: Node.js and pnpm available on the host, and Chrome 144 or later. In Chrome, open `chrome://inspect/#remote-debugging`, enable remote debugging, and approve the connection when prompted. Then reload the MCP configuration (`/reload` in Pi), check `/mcp`, and test with a blank tab.

**Privacy:** the server can access the selected Chrome profile, including its sessions and data; use a dedicated, empty profile. Pi does not automatically load `.env` files or interpolate environment variables in server arguments.
