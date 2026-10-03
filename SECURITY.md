# Security policy

## Supported versions

Security fixes currently target the latest commit on `main` until the first
versioned release is available.

## Deployment baseline

- Keep the application bound to localhost or a private network.
- Terminate TLS at a maintained reverse proxy.
- Generate unique values for every required secret in `.env`.
- Do not expose SQLite files, backups, uploads, or OAuth callback state through
  a web server.
- Run the container without additional Linux capabilities. The provided
  Compose file drops all capabilities and enables `no-new-privileges`.
- Back up all persistent volumes and test restoration periodically.
- Enable optional providers and hardware integrations one at a time.

## Reporting a vulnerability

Do not open a public issue containing credentials, personal data, or an
unpatched exploit. Use GitHub private vulnerability reporting after the public
repository is created. Include affected revision, impact, and minimal
reproduction steps without real user data.

## Release gate

Run `python scripts/public_release_audit.py` before every release. The audit is
defense in depth and does not replace manual review or GitHub secret scanning.
