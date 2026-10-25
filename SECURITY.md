# Security and private data

Do not include credentials, OAuth tokens, provider sessions, private IDs, logs, media, or your server configuration in issues or pull requests. The `.gitignore` excludes common runtime artifacts, but always inspect staged changes before pushing.

The bot's Telegram token belongs in a private `.env`. Music downloads require an explicit owner allowlist. Spotify login uses Authorization Code with PKCE on a loopback callback; its token stays in private runtime state and is not forwarded to audio providers. The gateway uses a local Unix socket with restricted access.

Review extension registries before installing their code. Provider availability and account policies are outside this repository's control.

If you expose a credential, revoke or rotate it at its provider and remove the exposed material. For a vulnerability, use GitHub's private vulnerability reporting when available; do not post an exploit containing live credentials in a public issue.
