# LIVA Emergency Admin Runbook

Use this only locally or over the normal SSH path.  It is independent of the
LIVA web process and exposes fixed commands only.

```bash
sudo liva-admin status
sudo liva-admin restart liva
sudo liva-admin restart mcp
sudo liva-admin self-healing off
sudo liva-admin release status
```

## Website unavailable

Read `sudo liva-admin status`, then use `sudo liva-admin restart liva`
once when LIVA is not healthy.  Re-read status; do not repeatedly
restart it.

## MCP unavailable

Read status, then use `sudo liva-admin restart mcp` once.  Verify with status.

## Self-Healing unexpected

Use `sudo liva-admin self-healing off`.  This is the same persistent global
kill switch used by Control Center.  Investigate before re-enabling it.

## Deployment stuck

Use `sudo liva-admin release status`.  Do not remove locks manually; use the
existing Release/rollback path only when it reports it is available.

## Recovery or USB problem

Use `sudo liva-admin status` to read recovery state.  Do **not** format or
repair a recovery medium.  Follow the encrypted Full-Recovery runbook and the
Windows secondary copy procedure.

## Total loss

Recover from the encrypted Full-Recovery bundle and encrypted identity on the
primary recovery medium or verified Windows secondary copy.  Never enter or
store the master password in a command, file, or chat.
