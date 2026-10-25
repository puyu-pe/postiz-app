# MCP configuration maintenance

Every MCP server added to the local `.pi/mcp.json` must also be represented in the tracked `.pi/mcp.json.dist` template. Use placeholders only; never include credentials, secrets, or personal machine-specific values in the template.

# Test

Use Chrome DevTools MCP to test affected browser flows and check the UI, console errors, and network requests. Use a dedicated tab in the development environment; do not inspect personal tabs or production. Report any blockers and test results.
