# MCP de Chrome DevTools

Desde la raíz del repositorio, crea tu configuración local:

```sh
cp .pi/mcp.json.dist .pi/mcp.json
```

Edita `.pi/mcp.json` y ajusta `--userDataDir` al directorio de datos del perfil de Chrome que usarás. La ruta de la plantilla es un ejemplo para Chrome Flatpak en Linux (`~/.var/app/com.google.Chrome/config/google-chrome`); cámbiala por la ruta del perfil nativo u otra máquina. Pi expande `~` solo cuando está al inicio del argumento.

Requisitos: Node.js y pnpm disponibles, Chrome 144 o posterior. En Chrome abre `chrome://inspect/#remote-debugging`, activa Remote Debugging y acepta la conexión solicitada. Después recarga la configuración MCP (`/reload` en Pi), comprueba `/mcp` y prueba en una pestaña vacía.

**Privacidad:** el servidor podrá acceder al perfil de Chrome indicado, incluidas sus sesiones y datos; usa un perfil dedicado y vacío. No pongas secretos en `.env`: Pi no carga ni interpola variables `.env` en los argumentos del servidor.
