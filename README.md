# salesforce-flow-coverage
Python script to report on salesforce flow coverage for your org

Before executing this script, make sure you have the following installed locally
  Python
  Python Requests library

usage: flow_coverage_report.py [-h] [--org ORG] [--debug-auth] [--api-version API_VERSION]
                               [--instance-url INSTANCE_URL] [--access-token ACCESS_TOKEN] [--list-orgs]
                               [--org-json ORG_JSON] [--org-aliases ORG_ALIASES] [--dump-org-json] [--elements]
                               [--debug-elements] [--debug-tests]

Generate Flow test coverage report from a selected Salesforce org (ACTIVE Flow versions only).

options:
  -h, --help            show this help message and exit
  --org ORG             Org alias or username (sf or sfdx CLI). If omitted, interactive selection attempted.
  --debug-auth          Enable verbose auth selection diagnostics.
  --api-version API_VERSION
                        Override API version (default env SF_API_VERSION or 60.0).
  --instance-url INSTANCE_URL
                        Direct instance URL (skip CLI org selection).
  --access-token ACCESS_TOKEN
                        Direct access token (skip CLI org selection).
  --list-orgs           List available org aliases/usernames and exit.
  --org-json ORG_JSON   Path to saved 'sf org list --json' output to use when CLI listing fails.
  --org-aliases ORG_ALIASES
                        Comma-separated aliases if no CLI/org JSON available.
  --dump-org-json       After selection, dump parsed org list to flow_orgs_dump.json for troubleshooting.
  --elements            Include executed Flow element names (single column ExecutedElementNames from FlowElementTestCoverage).
  --debug-elements      Verbose executed element diagnostics (bulk FlowElementTestCoverage query).
  --debug-tests         Verbose Apex test method provenance diagnostics (FlowTestCoverage & ApexClass queries).

Examples:
  1) Interactive selection (spinner, then menu):
     python flow_coverage_report.py

  2) Specify org alias directly (non-interactive):
     python flow_coverage_report.py --org MyProdAlias

  3) Override API version (e.g. 61.0):
     python flow_coverage_report.py --org MyProdAlias --api-version 61.0

  4) List orgs only (no coverage queries):
     python flow_coverage_report.py --list-orgs

  5) Use saved JSON from prior 'sf org list --json':
     python flow_coverage_report.py --org-json orgs.json --list-orgs

  6) Manual credentials (CI/CD or when CLI unavailable):
     python flow_coverage_report.py --instance-url https://yourInstance --access-token YOUR_TOKEN

  7) Enable auth diagnostics & dump org list:
     python flow_coverage_report.py --debug-auth --dump-org-json --list-orgs

  8) Provide aliases fallback (no CLI):
     python flow_coverage_report.py --org-aliases MyProdAlias,MySandboxAlias --list-orgs

Environment Variables:
  SF_API_VERSION      -> default API version (e.g. 60.0)
  SF_PROCESS_TYPES    -> comma-separated ProcessTypes filter (e.g. AutoLaunchedFlow,Flow)
  SF_INSTANCE_URL     -> direct instance URL fallback if selection fails
  SF_ACCESS_TOKEN     -> direct token fallback
  SF_ORG_JSON_PATH    -> path to saved 'sf org list --json' output for offline listing

  SF_MAX_TEST_METHODS -> max unique Apex test method entries per Flow (default 300)

Exit Codes:
  0 success / user exit (menu option 0)
  1 no orgs found in listing mode
  2 other errors (auth, API, parsing)

Legend in report:
  ✔ indicates FlowVersion has at least one FlowTestCoverage record
Element Detail (--elements flag):
  Adds a single ExecutedElementNames column listing unique FlowElementTestCoverage.ElementName values executed by Apex tests for each active Flow version.
Test Method Provenance (always included):
  Adds TestMethods column with unique 'ApexClassName.TestMethodName' entries showing which tests exercised each FlowVersion.
  Fallback logic: If TestMethodName is not available, resolves ApexTestMethodId -> (ApexClassId, Name) via ApexTestMethod and ApexClass queries.
