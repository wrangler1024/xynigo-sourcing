# Security Policy

## Supported versions

Only the latest published release receives security fixes while the project is
in alpha.

## Reporting a vulnerability

Do not disclose a vulnerability in a public issue if it could expose account
credentials, cookies, local browser sessions, user data, or writable platform
operations. Use GitHub private vulnerability reporting for this repository.

Reports should include affected versions, reproduction steps using synthetic
data, expected impact, and any suggested mitigation. Never attach real account
data, cookies, tokens, proxy URLs, or customer/order records.

## Operational safety

- Keep all real input files outside the repository.
- Do not commit `config.json`, logs, exported cookies, release credentials, or
  platform tokens.
- Run write operations only after reviewing a dry-run plan.
- Treat local browser debugging ports and sessions as sensitive.
