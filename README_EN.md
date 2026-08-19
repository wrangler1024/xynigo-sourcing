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

- Mexico and United States order and shipment lookup, including
  privacy-scoped tracking screenshots.
- Mexico and United States buyer-account registration tasks with explicit
  terms acknowledgement and manual takeover for unrecognized verification flows.
- Batch HubStudio environment creation for Mexico and the United States, with
  per-site purchasing groups, dry-run, explicit write confirmation, resumable
  state, and credential-free mapping exports. The buyer roster is fixed
  (新刚-XG / 志恒-ZH / 康德-KD / 宇航-YH) and environments are named with
  English codes (for example `XG-MX-0819-001`). A backup/test mode creates
  remark-only environments without cookie imports, account binding, or
  ledger writes.
- Optional Lark Base ledger integration configured entirely at runtime.

The current stable release is `v0.7.0`. The project is under active
development and is not yet a hosted SaaS product.

v0.7.0 brings the buyer roster and backup-environment workflow to environment creation: a fixed four-person roster with English-code environment naming (for example XG-MX-0819-001), a backup/test mode that creates remark-only environments without binding, parallel creation, a builtin default proxy URL for zero-config onboarding, and automatic account-site validation based on cookie login domains.

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
| `XYNIGO_PROXY_LINK` | Optional first-run default. A builtin default dynamic-proxy extraction URL ships with the tool; a custom link can be saved in settings to override it, and clearing restores the builtin default |
| `XYNIGO_PURCHASE_TAG` | HubStudio group for purchasing environments |
| `XYNIGO_PURCHASE_TAG_MX` / `XYNIGO_PURCHASE_TAG_US` | Optional per-site purchasing environment groups |
| `XYNIGO_REGISTER_TAG` | HubStudio group for registration environments |
| `XYNIGO_REGISTER_TAG_MX` / `XYNIGO_REGISTER_TAG_US` | Optional per-site registration environment groups |
| `XYNIGO_LARK_BASE_TOKEN` | Optional Lark Base token |
| `XYNIGO_LARK_TABLE_ID` | Optional legacy MX Lark Base table ID |
| `XYNIGO_LARK_TABLE_ID_MX` / `XYNIGO_LARK_TABLE_ID_US` | Optional per-site Lark Base tables; US never falls back to the legacy MX setting |
| `XYNIGO_LARK_OPERATOR_OPEN_ID` | Optional operator ID used by the ledger backfill command |

The module-three macOS backfill command validates its site explicitly. Use
`python -m purchase_tool backfill --site US ...` together with a separate
`XYNIGO_LARK_TABLE_ID_US`; the default `--site MX` only preserves the existing
workflow.

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

## Windows and macOS portable builds

```bash
bash 组装Windows绿色包.sh
bash 组装macOS绿色包.sh
```

Builds are written to `dist/` with portable ZIPs, SHA-256 checksums, and one
machine-readable cross-platform update manifest. Windows uses the official
embeddable Python runtime. macOS uses a self-contained PyInstaller runtime for
Apple Silicon `arm64`. macOS Intel is not maintained.

### Cross-platform online updates

- v0.5.0 must still be downloaded and fully extracted manually once.
- macOS has equivalent portable builds and online updates starting with
  v0.5.1.
- On Windows use `启动.bat`; on macOS use `启动-Mac.command`. After startup,
  the WebUI checks for a newer stable release and shows a top-right notice.
- Clicking the notice brings the console forward. Enter `Y` there to update
  or `N` to continue; the WebUI never installs an update silently.
- No GitHub account or Git installation is required. Downloads inherit the
  operating-system network configuration. Windows system proxies and
  transparent TUN adapters are supported.
- Packages are verified with SHA-256 before replacement. The current program
  is backed up first and automatically rolled back if replacement fails.
- Only managed program files are replaced. `config.json`, local data, logs,
  and user-imported files remain in place. Update-check failures never block
  normal startup.

## Roadmap

- Separate preview and stable update channels.
- Automated dual-platform release builds.
- Additional sourcing, order, fulfillment, and reporting modules.

## License

Apache License 2.0. See [LICENSE](LICENSE).
