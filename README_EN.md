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
- Buyer-account resources use shared PostgreSQL as the only business source.
  Listing, vendor-import duplicate checks, checkout reservation, and status
  decisions use cloud APIs. Complete credentials are stored in an authenticated
  encrypted envelope, while a new independent Feishu test Base is an authorized
  outbound collaboration mirror. The legacy `买家号（统一）` table is migration
  input only.

The current online coordination test release is `v0.12.4`, with update
packages for Windows x86_64 and macOS ARM64. The project is under active
development and is not yet a production hosted SaaS product.

The current coordinated test release is `v0.12.4`. It moves procurement
collaboration imports into the cloud workspace and makes the desktop launcher
open that cloud workspace by default, while retaining a dedicated local UI for
HubStudio/CDP/SHEIN work and troubleshooting. This remains a shared-test
candidate, not a production release.

v0.10.0 adds an opt-in safe parallel mode. When enabled, one order/shipment
query batch may run alongside either one bound-environment batch or one
backup/test-environment batch. The local executor reserves environment names
and container codes, continues to reject opening the same environment twice,
serializes cross-module browser control requests, and caps total HubStudio
Local API pressure. Registration, two query batches, and two environment
creation batches remain mutually exclusive. The setting defaults to off for
the initial rollout.

v0.9.0 extends the unified buyer ledger contract with the HubStudio
environment-group name, including idempotency checks and post-write readback.
Moving the same environment to another group updates its existing record,
while binding an account to a different environment remains a conflict.
Feishu Email/URL text display styles now pass schema validation, and URL values
use the object shape required by the raw OpenAPI. A completed HubStudio batch
that did not initially enable ledger writeback can be supplemented later after
a separate confirmation and read-only preflight, without rerunning HubStudio
writes.

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

The desktop launcher starts the local executor in the background and opens the
cloud workspace at `https://xynigo.samforo.icu` by default. The local service
continues to listen on `http://127.0.0.1:8765` (or the next available port) for
HubStudio/CDP/SHEIN capabilities. Run `python -m purchase_tool --local-ui`, or
use the package's dedicated local-executor launcher, to open the legacy local UI.

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

The legacy Base link retained in Settings is migration-source configuration
for super administrators only. The page never reads that table automatically;
only the explicit read-only verification action accesses its metadata. The
complete URL is never persisted. Resolved routing identifiers are stored in
Git-ignored `config.json`, the Feishu App ID/Secret uses macOS Keychain or
Windows DPAPI-protected storage, and the tenant token remains in process
memory. The Web API never returns the secret, complete URL, Base token, or
table ID. Sensitive input workbooks must remain outside the repository.

The cloud worker routes buyer mirrors, environment results, and logistics
results to three tables in a new independent Base. They share enterprise-app
authentication but have no relation to the legacy `买家号（统一）` table, which
is never a daily automatic write target.

## Security model

- Real buyer credentials are accepted by authorized APIs, stored in an
  authenticated encrypted envelope, and decrypted only for permission-gated
  reads and the Base sync worker.
- Sensitive temporary payloads use restrictive file permissions where the
  operating system supports them.
- Real platform writes require explicit confirmation.
- Vendor intake performs PostgreSQL duplicate checks using stable account/order
  references. Passwords, cookies, and verification keys enter encrypted
  PostgreSQL and the authorized test Base, but never the outbox payload, logs,
  error details, Git, or test fixtures.
- Database transaction outbox events drive Base mirroring. Only a matching
  post-write readback marks sync complete; uncertain outcomes remain retryable
  without changing the PostgreSQL fact.
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
