# Contributing

Contributions that improve reliability, privacy, documentation, portability, or test coverage are welcome.

## Ground rules

- Use the software only with accounts and public content you are permitted to access. Follow applicable platform terms, laws, privacy, and copyright rules.
- Never commit or attach real cookies, tokens, API credentials, chat IDs, Pyrogram sessions, monitored-user data, screenshots, media, logs, or backups.
- Use obviously fictional placeholders in tests and documentation.
- Keep polling conservative and preserve jitter; do not add rate-limit or access-control bypasses.
- By submitting a contribution, you agree that it is licensed under MIT and confirm that you have the right to submit it.

## Development setup

Create an isolated Python environment, install the pinned dependencies, and install Chromium when browser work is required:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Use test credentials and a private Telegram destination. Do not reuse a production `.env` for development.

## Tests

Run the offline unit tests and validate Compose before opening a pull request:

```bash
python -m unittest discover -v -p "test_*.py"
docker compose config
```

Do not include manual/live push scripts in the default automated test suite. Any live test must require an explicit opt-in, clearly state that it sends a real message, and use dedicated test accounts.

## Changes and pull requests

1. Create a focused branch and keep unrelated formatting changes out.
2. Add or update tests and documentation for behavior changes.
3. Run the checks above and summarize the results in the pull request.
4. Explain configuration migrations and privacy/security effects.
5. Use Conventional Commits, for example `fix: persist rotated cookies`.

For vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
