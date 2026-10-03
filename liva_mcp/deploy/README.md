# Remote deployment notes

The committed unit is a template for the separate `liva-mcp-remote.service`. It does not replace or restart `liva.service` and does not modify the existing port-443 Funnel.

Deployment order:

1. Install `requirements-remote.lock` and this package into the dedicated remote Venv.
2. Generate the external configuration and credentials with `liva-mcp-generate-remote-config`.
3. Install and verify the dedicated systemd unit.

For live parity with the canonical LIVA actions facade, install
`systemd/actions-bridge.conf` as a drop-in for both `liva-mcp-remote.service`
and `liva.service`. Generate one private 0600 bridge token at
`/var/lib/liva/.config/liva-mcp-remote/actions-bridge-token`. The token is
accepted only from loopback, with the MCP bridge header, and only on the two
`/api/v2/actions/liva/*` facade routes.
4. Complete local OAuth and MCP smoke tests on `127.0.0.1:8765`.
5. Only then add an isolated Tailscale Funnel listener on the selected unused HTTPS port.

Rollback affects only the new Funnel listener and `liva-mcp-remote.service`. The existing port-443 Funnel and `liva.service` must remain untouched.

The unit bind-mounts each allowlisted production SQLite file read-only into the dedicated runtime database view. SQLite may create WAL/SHM sidecars beside that view; the original database files and repository remain read-only inside the service sandbox.
