# Salesforce Flow Coverage Reporter

Generate a Salesforce Flow test coverage report for active Flow versions in a Salesforce org.

## What it does

- Connects to a Salesforce org using `sf`, `sfdx`, or direct credentials.
- Queries Flow test coverage through the Salesforce Tooling API.
- Prints a per-flow coverage table.
- Prints an org-level Flow coverage summary.
- Exports results to a timestamped CSV file.
- Optionally includes executed Flow element names and Apex test method provenance.

## Requirements

- Python 3.8+
- Salesforce CLI: `sf` recommended, `sfdx` supported as fallback
- Python package: `requests`
- An authenticated Salesforce org

## Installation

```bash
git clone https://github.com/magdielhf/salesforce-flow-coverage.git
cd salesforce-flow-coverage
python -m pip install requests
```

## Authenticate to Salesforce

```bash
sf org login web --alias MySandbox
```

Or use direct credentials:

```bash
export SF_INSTANCE_URL="https://yourInstance.my.salesforce.com"
export SF_ACCESS_TOKEN="YOUR_ACCESS_TOKEN"
```

## Quick start

Interactive org selection:

```bash
python flow_coverage_report.py
```

Run against a specific org alias:

```bash
python flow_coverage_report.py --org MySandbox
```

List available orgs:

```bash
python flow_coverage_report.py --list-orgs
```

Generate a report with executed Flow elements:

```bash
python flow_coverage_report.py --org MySandbox --elements
```

## Common options

| Option | Description |
|---|---|
| `--org ORG` | Salesforce org alias or username. |
| `--list-orgs` | Lists available authenticated orgs and exits. |
| `--api-version VERSION` | Overrides the Salesforce API version. |
| `--elements` | Adds executed Flow element names to the CSV. |
| `--debug-auth` | Prints authentication and org-selection diagnostics. |
| `--debug-elements` | Prints Flow element coverage diagnostics. |
| `--debug-tests` | Prints Apex test provenance diagnostics. |
| `--instance-url URL` | Uses a direct Salesforce instance URL. |
| `--access-token TOKEN` | Uses a direct Salesforce access token. |

## Environment variables

| Variable | Purpose |
|---|---|
| `SF_API_VERSION` | Salesforce API version used for Tooling API calls. |
| `SF_PROCESS_TYPES` | Comma-separated Flow process types to include. |
| `SF_INSTANCE_URL` | Direct instance URL fallback. |
| `SF_ACCESS_TOKEN` | Direct access token fallback. |
| `SF_ORG_JSON_PATH` | Path to saved `sf org list --json` output. |
| `SF_MAX_TEST_METHODS` | Max Apex test method entries per Flow. |

## Output

The script prints:

1. A per-flow coverage table.
2. An org-level Flow coverage summary.
3. A CSV file named like:

```text
flow_coverage_active_only_1712345678.csv
```

The CSV includes fields such as:

```text
FlowLabel, FlowApiName, ProcessType, VersionNumber, FlowVersionId,
ElementsTotal, ElementsCovered, ElementsNotCovered, CoveragePercent,
TestMethods
```

When `--elements` is used, the CSV also includes:

```text
ExecutedElementNames
```

## Report legend

`✔` means the Flow version has at least one `FlowTestCoverage` row and contributes to the covered-flow numerator.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Success or user-selected exit. |
| `1` | No orgs found in listing mode. |
| `2` | Authentication, API, parsing, or other runtime error. |

## Troubleshooting

### No orgs found

Log in with Salesforce CLI:

```bash
sf org login web --alias MySandbox
```

Then verify the org appears:

```bash
sf org list
```

### Authentication issues

Run with auth diagnostics:

```bash
python flow_coverage_report.py --debug-auth --list-orgs
```

### CI/CD usage

Use direct credentials:

```bash
python flow_coverage_report.py \
  --instance-url "$SF_INSTANCE_URL" \
  --access-token "$SF_ACCESS_TOKEN"
```

## License

MIT
