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
- Optional direct Feishu/Lark Base OpenAPI integration through an enterprise
  custom app, with unified-ledger conflict preflight and post-write readback.
  Teammate computers do not need `lark-cli`.

The current stable release is `v0.8.3`. The project is under active
development and is not yet a hosted SaaS product.

v0.8.3 improves connection verification after a production Feishu Base target
is reconfigured. Opening System Settings can refresh the current Base and
table names, while saving and the dedicated connection-and-schema check report
their validation results separately. If advanced permissions hide the target
table from the Xynigo enterprise app, the UI now gives a direct authorization
hint. Metadata refresh and schema validation remain read-only, and public
configuration responses still withhold secrets and deployment identifiers.

v0.8.2 fixes a potential macOS Keychain stall while saving Feishu app
credentials and improves visibility of the configured Base target. The UI
shows the verified Base and table names through a safe new-tab link while the
configuration API continues to withhold the Base token, table ID, and original
URL. Environment creation now reports distinct submitting, background
execution, ledger writeback, partial-failure, and temporarily-unavailable
progress states, so pending or conflicting ledger rows are not reported as a
fully successful batch.

v0.8.0 upgrades post-environment ledger handling from manual TSV paste to an
optional enterprise-app OpenAPI path. It performs unified-ledger dual-key and
site conflict preflight before HubStudio writes, then writes and reads back
each successful row. Partial failures can retry only pending ledger rows
without repeating HubStudio steps. TSV remains an emergency manual artifact,
not proof of API success.

v0.7.3 extends strict buyer-intake xlsx parsing beyond the existing
`orderNo` links to the new vendor's `id + email` and email-only link formats.
The new format must match the expected path and exact parameter set, and its
email must match the account email. For rows without a business order number,
the application derives an irreversible, stable internal reference for
cross-group deduplication and idempotent recovery.

v0.7.2 fixes the post-environment-creation TSV used for direct Lark Base
pasting. It emits the current MX or US Grid View column order, omits the
header and legacy notes column, preserves a blank position for the formula
driven purchase-date field, and tells operators to paste from the first empty
`Email Account` cell rather than the automatic account-ID column. The buyer
intake xlsx template remains headered.

v0.7.1 changes tracking screenshots in logistics-query Excel exports from
floating drawing objects to true Excel pictures in cells. Colleagues can copy
and paste a continuous range of image cells in one operation. Image bytes stay
self-contained in the `.xlsx` file with no external file or URL dependency.
This feature targets Microsoft 365 and Excel 2024; older Excel versions and
WPS Office are not guaranteed to display or copy the rich image values.

v0.7.0 brings the buyer roster and backup-environment workflow to environment creation: a fixed four-person roster with English-code environment naming (for example XG-MX-0819-001), a backup/test mode that creates remark-only environments without binding, parallel creation, a builtin default proxy URL for zero-config onboarding, and automatic account-site validation based on cookie login domains.

## Requirements

- Python 3.9 or later.
- A locally installed and signed-in HubStudio client.
- `websocket-client` and `openpyxl`.
- Optional: an enterprise custom app authorized for the target Base. Configure
  it from the left-side Settings page and paste one complete `/base/` or
  `/wiki/` link containing `table=tbl...`; the system resolves the target
  automatically and does not require `lark-cli`.

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
| `XYNIGO_LARK_BASE_TOKEN` | Optional first-run migration value for the unified buyer Base token |
| `XYNIGO_LARK_TABLE_ID` | Optional first-run migration value for the unified buyer table ID |
| `XYNIGO_LARK_TABLE_ID_MX` / `XYNIGO_LARK_TABLE_ID_US` | Legacy administrator backfill compatibility only; the Web OpenAPI path uses one unified table |
| `XYNIGO_LARK_OPERATOR_OPEN_ID` | Optional operator ID used by the ledger backfill command |

The legacy administrator-only macOS backfill command still requires
`lark-cli` and validates its site explicitly. Use
`python -m purchase_tool backfill --site US ...` together with a separate
`XYNIGO_LARK_TABLE_ID_US`; the default `--site MX` only preserves the existing
workflow.

The Settings page does not require users to split Base or table tokens. Direct
`/base/` links resolve locally; `/wiki/` links use one read-only node lookup
through the app identity, so the app also needs Wiki node-read permission and
access to that knowledge space. The target can be reconfigured by confirming a
new link. Settings also provides a downloadable full unified buyer-ledger
template: the first 14 columns preserve the API/TSV contract, while eight
additional columns mirror the existing Feishu operational fields. After
switching tables, align system fields, types, display styles, and select options
with the template, then run the read-only field check. The complete URL is never persisted. Local UI
preferences and the resolved unified Base/table routing identifiers are stored
in `config.json`, which is ignored by Git. The Feishu App ID/Secret is stored
in the current user's macOS Keychain or Windows DPAPI-protected storage, while
the tenant access token remains in process memory. The Web API never returns
the Secret, complete URL, Base token, or table ID. An authorized teammate computer may store
the App Secret locally, but it must not be committed, packaged, logged, or
shown in screenshots. Sensitive input workbooks must remain outside the
repository.

One enterprise custom app may access multiple authorized Bases. Xynigo
currently configures one writable target, `买家号（统一）`; future procurement
tables can use separate routing entries while sharing the same app credential.
Backup tables are never automatic write targets.

## Security model

- Real credentials are accepted only at runtime and are redacted from progress
  responses and logs.
- Sensitive temporary payloads use restrictive file permissions where the
  operating system supports them.
- Real platform writes require explicit confirmation.
- Feishu writeback is off by default and confirmed separately from HubStudio
  writes. A full-table dual-key check runs first, and partial failures never
  repeat successful HubStudio steps.
- A Feishu write counts as successful only after a matching readback;
  uncertain outcomes remain retryable.
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

- Add independently authorized routing for more procurement Bases while
  sharing the enterprise custom-app credential.
- Separate preview and stable update channels.
- Automated dual-platform release builds.
- Additional sourcing, order, fulfillment, and reporting modules.

## License

Apache License 2.0. See [LICENSE](LICENSE).
