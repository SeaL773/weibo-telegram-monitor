# Security Policy

## Supported versions

Security fixes are applied to the latest revision of the default branch. Older commits, forks, and modified deployments are not supported.

## Reporting a vulnerability

Use GitHub's **Private vulnerability reporting** feature for this repository, or open a private draft security advisory for the maintainers. Do not open a public issue containing exploit details, cookies, tokens, chat IDs, account identifiers, screenshots, logs, or other personal data.

Include, when available:

- the affected commit and deployment method;
- a minimal reproduction with all credentials replaced by placeholders;
- impact and prerequisites;
- relevant logs with URLs, headers, cookies, tokens, UIDs, and chat IDs removed;
- a suggested fix or mitigation.

Expect an acknowledgement within seven days. Timelines for validation, fixes, and coordinated disclosure depend on severity and maintainer availability.

## Credential exposure

Weibo cookies, Telegram bot tokens, Telegram API credentials, and Pyrogram session files are secrets. If any may have been exposed:

1. Revoke or rotate the affected credential at its provider.
2. Stop the deployment and remove exposed `.env`, `data/auth_state.json`, session, log, artifact, image-layer, and backup copies.
3. Recreate the service with fresh credentials.
4. If a secret entered Git history, rotate it first, then purge the history and invalidate cached clones or artifacts.

The project stores Weibo cookie state unencrypted. Host filesystem permissions, Windows ACLs, disk encryption, backup protection, and access to the private Telegram destination are the operator's responsibility.

## Scope

Reports about this repository's code or default container configuration are in scope. Issues solely in Weibo, Telegram, Chromium, FFmpeg, or another dependency should be reported to that project's security contact unless this project introduces or worsens the issue.
