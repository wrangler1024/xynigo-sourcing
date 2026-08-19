# Xynigo Sourcing

[简体中文](README.md) | [English](README_EN.md)

Xynigo Sourcing is an early-stage, open-source sourcing orchestration system
for cross-border ecommerce teams. It combines a local web interface with
HubStudio Local API and browser automation.

> This project is not affiliated with or endorsed by SHEIN, HubStudio,
> Microsoft, Feishu, or Lark. Use it only with accounts and systems you are
> authorized to operate, and comply with applicable platform terms and laws.

Maintained by **Velane Technology**. Field-tested by **Xynigo**. Business
workflows contributed by **Samforo**.

## Current modules

- Order and shipment lookup, including privacy-scoped tracking screenshots.
- Buyer-account registration with explicit terms acknowledgement and manual
  takeover for unrecognized verification flows.
- Batch HubStudio environment creation with dry-run, explicit write
  confirmation, resumable state, and credential-free mapping exports.
- Optional Lark Base ledger integration configured entirely at runtime.

The current stable release is `v0.5.0`. The project is under active
development and is not yet a hosted SaaS product.

## Requirements

- Python 3.9 or later.
- A locally installed and signed-in HubStudio client.
- `websocket-client` and `openpyxl`.
- Optional: `lark-cli` for the Lark Base adapter.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m purchase_tool
```

The local UI opens at `http://127.0.0.1:8765`. If the port is occupied, the
application tries the next available port.

On macOS, `启动-Mac.command` provides the same local development entry point.

## Runtime configuration

No deployment identifiers, API keys, cookies, account credentials, proxy
URLs, or Lark record IDs are stored in this repository.

| Environment variable | Purpose |
|---|---|
| `XYNIGO_PROXY_LINK` | HubStudio dynamic-proxy extraction URL used only when creating a new environment |
| `XYNIGO_PURCHASE_TAG` | HubStudio group for purchasing environments |
| `XYNIGO_REGISTER_TAG` | HubStudio group for registration environments |
| `XYNIGO_LARK_BASE_TOKEN` | Optional Lark Base token |
| `XYNIGO_LARK_TABLE_ID` | Optional Lark Base table ID |
| `XYNIGO_LARK_OPERATOR_OPEN_ID` | Optional operator ID used by the ledger backfill command |

Local UI preferences are stored in `config.json`, which is ignored by Git.
Sensitive input workbooks must remain outside the repository.

## Security model

- Real credentials are accepted only at runtime and are redacted from progress
  responses and logs.
- Sensitive temporary payloads use restrictive file permissions where the
  operating system supports them.
- Real platform writes require explicit confirmation.
- Batch resume files contain only irreversible account identifiers and
  non-secret progress metadata.
- Update and release packages must never contain local `config.json`, logs, or
  user input files.

See [SECURITY.md](SECURITY.md) before deploying or reporting a vulnerability.

## Tests

```bash
python -m pip install -e .
python -m unittest discover -s tests
```

All committed tests use synthetic accounts, example domains, and fake cookie
values.

## Windows portable build

```bash
bash 组装Windows绿色包.sh
```

The build is written to `dist/` and includes the full portable ZIP, a SHA-256
file, and a machine-readable update manifest. The script packages the
application with the official Windows embeddable Python runtime.

### Windows online updates

- v0.5.0 must still be downloaded and fully extracted manually once.
- Starting with v0.5.0, `启动.bat` checks the latest stable GitHub Release.
  Enter `Y` to update or `N` to skip.
- No GitHub account or Git installation is required. Downloads inherit the
  Windows network configuration, including system proxies and transparent TUN
  adapters.
- Packages are verified with SHA-256 before replacement. The current program
  is backed up first and automatically rolled back if replacement fails.
- Only managed program files are replaced. `config.json`, local data, logs,
  and user-imported files remain in place. Update-check failures never block
  normal startup.

## Roadmap

- Separate preview and stable update channels.
- Centralized version metadata and automated release builds.
- Additional sourcing, order, fulfillment, and reporting modules.

## License

Apache License 2.0. See [LICENSE](LICENSE).
