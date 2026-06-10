#!/usr/bin/env python3
import os
import sys
import csv
import time
import json
import subprocess
import shutil
from urllib.parse import quote
import requests
import threading
import itertools

# =========================
# Config (via environment)
# =========================
API_VERSION = os.getenv("SF_API_VERSION", "63.0").strip()
INSTANCE_URL = None  # Set after selection
ACCESS_TOKEN = None  # Set after selection
SF_CMD = None  # Resolved CLI command name (sf/sf.cmd/sf.exe)
SFDX_CMD = None  # Legacy CLI (sfdx/sfdx.cmd)
DEBUG_AUTH = False

# Use the ProcessTypes you observed in your org (case-sensitive).
# Example: AutoLaunchedFlow,Workflow,Flow
# Note: Salesforce uses 'AutolaunchedFlow' and 'WorkflowRule' in FlowDefinitionView,
# but the script handles both variants (AutoLaunchedFlow/AutolaunchedFlow, Workflow/WorkflowRule)
PT_ENV = os.getenv("SF_PROCESS_TYPES", "AutoLaunchedFlow").strip()
PROCESS_TYPES = [p.strip() for p in PT_ENV.split(",") if p.strip()] if PT_ENV != "" else None

# Max element names to list if you later enable name-enrichment (kept here for future use)
MAX_NAMES = int(os.getenv("SF_MAX_NAMES", "200"))
MAX_TEST_METHODS = int(os.getenv("SF_MAX_TEST_METHODS", "300"))  # Cap for test method provenance listing

HEADERS_JSON = {}
_SPINNER_RUNNING = False
_SPINNER_THREAD = None
_FETC_FIELD_MAP = None  # Cache for FlowElementTestCoverage field detection

def start_spinner(message: str, interval: float = 0.15):
    """Start a non-blocking spinner with a status message."""
    global _SPINNER_RUNNING, _SPINNER_THREAD
    if DEBUG_AUTH:
        # Skip spinner when debugging to keep output readable
        print(f"[DEBUG] Spinner suppressed (debug mode) message='{message}'")
        return
    if _SPINNER_RUNNING:
        return
    _SPINNER_RUNNING = True
    frames = itertools.cycle("|/-\\")
    def _spin():
        while _SPINNER_RUNNING:
            frame = next(frames)
            print(f"\r{message} {frame}", end="", flush=True)
            time.sleep(interval)
        # Clear line after stopping
        print("\r" + " " * (len(message) + 2) + "\r", end="", flush=True)
    _SPINNER_THREAD = threading.Thread(target=_spin, daemon=True)
    _SPINNER_THREAD.start()

def stop_spinner():
    """Stop spinner if running."""
    global _SPINNER_RUNNING, _SPINNER_THREAD
    if not _SPINNER_RUNNING:
        return
    _SPINNER_RUNNING = False
    if _SPINNER_THREAD and _SPINNER_THREAD.is_alive():
        _SPINNER_THREAD.join(timeout=1)
    _SPINNER_THREAD = None

def resolve_command(candidates):
    """Attempt to resolve a command returning its absolute path.
    Returns first successful full path from shutil.which or where.
    """
    for c in candidates:
        p = shutil.which(c)
        if p:
            if DEBUG_AUTH:
                print(f"[DEBUG] resolve_command: shutil.which found {c} -> {p}")
            return p
    # PowerShell/Windows 'where' fallback
    try:
        for c in candidates:
            proc = subprocess.run(["where", c], capture_output=True, text=True, timeout=3)
            if proc.returncode == 0 and proc.stdout.strip():
                first = proc.stdout.strip().splitlines()[0]
                if DEBUG_AUTH:
                    print(f"[DEBUG] resolve_command: where found {c} -> {first}")
                return first
    except Exception as e:
        if DEBUG_AUTH:
            print(f"[DEBUG] resolve_command: where exception: {e}")
    return None

def resolve_sf_command():
    return resolve_command(["sf", "sf.cmd", "sf.exe"])

def resolve_sfdx_command():
    return resolve_command(["sfdx", "sfdx.cmd", "sfdx.exe"])

def sf_cli_available():
    """Check if modern 'sf' CLI available; cache command name."""
    global SF_CMD
    if SF_CMD:
        return True
    cmd = resolve_sf_command()
    if not cmd:
        return False
    try:
        subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
        SF_CMD = cmd
        if DEBUG_AUTH:
            print(f"[DEBUG] Found sf CLI: {SF_CMD}")
        return True
    except Exception:
        return False

def sfdx_cli_available():
    """Check if legacy 'sfdx' CLI available; cache command name."""
    global SFDX_CMD
    if SFDX_CMD:
        return True
    cmd = resolve_sfdx_command()
    if not cmd:
        return False
    try:
        subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
        SFDX_CMD = cmd
        if DEBUG_AUTH:
            print(f"[DEBUG] Found sfdx CLI: {SFDX_CMD}")
        return True
    except Exception:
        return False

def list_orgs():
    """List orgs from either sf or sfdx CLI. Returns uniform list of dicts with alias/username.

    Diagnostic improvements:
    - Captures raw stdout/stderr length
    - Broadly scans all list-valued keys inside result for org-like dicts
    - Dumps raw JSON to a temp file when DEBUG_AUTH enabled
    - Attempts alternative command variants (sf org list --all --json)
    """
    def _aggregate(data: dict):
        result = data.get("result") or {}
        if DEBUG_AUTH:
            print(f"[DEBUG] list_orgs: result keys -> {list(result.keys())}")
        orgs = []
        # Known keys
        known_keys = [
            "scratchOrgs", "nonScratchOrgs", "sandboxes", "devHubs",
            "otherOrgs", "other", "functions", "orgs"
        ]
        # Add any list-valued keys not in known_keys for completeness
        for k, v in result.items():
            if isinstance(v, list) and k not in known_keys:
                known_keys.append(k)
        for key in known_keys:
            arr = result.get(key)
            if isinstance(arr, list):
                orgs.extend(arr)
        # Filter to entries that look org-like
        filtered = []
        seen = set()
        for o in orgs:
            if not isinstance(o, dict):
                continue
            alias = o.get("alias")
            uname = o.get("username") or o.get("user")
            if not alias and not uname:
                continue
            ident = (alias, uname)
            if ident in seen:
                continue
            seen.add(ident)
            filtered.append(o)
        if DEBUG_AUTH:
            print(f"[DEBUG] list_orgs: aggregated {len(filtered)} org entries")
        return filtered

    def _invoke_sf(cmd_variant):
        try:
            proc = subprocess.run(cmd_variant, capture_output=True, text=True, timeout=45)
        except Exception as e:
            if DEBUG_AUTH:
                print(f"[DEBUG] sf invoke exception for {cmd_variant}: {e}")
            return []
        if DEBUG_AUTH:
            print(f"[DEBUG] sf cmd={' '.join(cmd_variant)} exit={proc.returncode} stdout_len={len(proc.stdout)} stderr_len={len(proc.stderr)}")
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        raw = proc.stdout
        try:
            data = json.loads(raw)
        except Exception as je:
            if DEBUG_AUTH:
                print(f"[DEBUG] sf JSON parse failed: {je}")
            return []
        if DEBUG_AUTH:
            # Dump raw json for inspection (overwrites each call)
            try:
                with open("sf_org_list_raw.json", "w", encoding="utf-8") as f:
                    f.write(raw)
                print("[DEBUG] Wrote raw CLI JSON to sf_org_list_raw.json")
            except Exception as de:
                print(f"[DEBUG] Failed writing sf_org_list_raw.json: {de}")
        return _aggregate(data)

    # Attempt sf CLI first (multiple variants)
    sf_cmd = resolve_sf_command()
    if DEBUG_AUTH:
        print(f"[DEBUG] list_orgs: resolved sf_cmd={sf_cmd}")
    if sf_cmd:
        variants = [
            [sf_cmd, "org", "list", "--json"],
            [sf_cmd, "org", "list", "--all", "--json"],  # legacy style (if needed)
        ]
        for v in variants:
            orgs = _invoke_sf(v)
            if orgs:
                return orgs
    # Fallback to sfdx
    if sfdx_cli_available():
        try:
            proc = subprocess.run([SFDX_CMD, "force:org:list", "--json"], capture_output=True, text=True, timeout=45)
            if DEBUG_AUTH:
                print(f"[DEBUG] sfdx force:org:list exit={proc.returncode} stdout_len={len(proc.stdout)} stderr_len={len(proc.stderr)}")
            if proc.returncode != 0 or not proc.stdout.strip():
                raise RuntimeError(proc.stderr.strip())
            data = json.loads(proc.stdout)
            if DEBUG_AUTH:
                try:
                    with open("sfdx_org_list_raw.json", "w", encoding="utf-8") as f:
                        f.write(proc.stdout)
                    print("[DEBUG] Wrote raw CLI JSON to sfdx_org_list_raw.json")
                except Exception as de:
                    print(f"[DEBUG] Failed writing sfdx_org_list_raw.json: {de}")
            return _aggregate(data)
        except Exception as e:
            if DEBUG_AUTH:
                print(f"[DEBUG] sfdx listing exception: {e}")
            else:
                print(f"WARNING: sfdx force:org:list failed: {e}")
    # Fallback: local JSON file via env SF_ORG_JSON_PATH
    json_path = os.getenv("SF_ORG_JSON_PATH")
    if json_path and os.path.isfile(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if DEBUG_AUTH:
                print(f"[DEBUG] Using SF_ORG_JSON_PATH fallback: {json_path}")
            return _aggregate(data)
        except Exception as e:
            if DEBUG_AUTH:
                print(f"[DEBUG] Failed parsing SF_ORG_JSON_PATH={json_path}: {e}")
    return []

def _parse_org_json(data):
    result = data.get("result") or {}
    orgs = []
    for key in ["scratchOrgs", "nonScratchOrgs", "sandboxes", "devHubs", "otherOrgs", "other", "functions"]:
        arr = result.get(key)
        if isinstance(arr, list):
            orgs.extend(arr)
    seen = set(); uniq = []
    for o in orgs:
        ident = (o.get("alias"), o.get("username"))
        if ident in seen:
            continue
        seen.add(ident); uniq.append(o)
    if DEBUG_AUTH:
        print(f"[DEBUG] _parse_org_json aggregated entries={len(uniq)}")
    return uniq

def get_org_auth(target: str):
    """Fetch instanceUrl/accessToken via sf or sfdx for given target alias/username."""
    if sf_cli_available():
        proc = subprocess.run([SF_CMD, "org", "display", "--json", "--target-org", target], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"sf org display failed for {target}: {proc.stderr.strip()}")
        data = json.loads(proc.stdout)
        res = data.get("result") or {}
        inst = res.get("instanceUrl")
        token = res.get("accessToken")
        if DEBUG_AUTH:
            print(f"[DEBUG] sf display instanceUrl={inst}")
        if inst and token:
            return inst, token
    if sfdx_cli_available():
        proc = subprocess.run([SFDX_CMD, "force:org:display", "--json", "--targetusername", target], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"sfdx force:org:display failed for {target}: {proc.stderr.strip()}")
        data = json.loads(proc.stdout)
        res = data.get("result") or {}
        inst = res.get("instanceUrl") or res.get("loginUrl")
        token = res.get("accessToken")
        if DEBUG_AUTH:
            print(f"[DEBUG] sfdx display instanceUrl={inst}")
        if inst and token:
            return inst, token
    raise RuntimeError("Missing instanceUrl/accessToken from CLI display output")

def interactive_select_org():
    # Caller ensures spinner stopped before this interactive prompt
    orgs = list_orgs()
    if not orgs:
        print("No orgs returned by CLI. You may need to login first (e.g. 'sf org login web --alias MyAlias').")
        return None
    print("\nAvailable Salesforce orgs:")
    for i, o in enumerate(orgs, 1):
        alias = o.get("alias") or "(no-alias)"
        uname = o.get("username") or "?"
        typ = o.get("instanceUrl") or o.get("loginUrl") or ""
        print(f"  {i:2}. {alias:20} {uname:40} {typ}")
    print("  0 . Exit")
    while True:
        choice = input("Select org number (Enter to cancel): ")
        if choice.strip() == "":
            return None
        if not choice.isdigit():
            print("Enter a valid number.")
            continue
        idx = int(choice)
        if idx == 0:
            print("Exiting per user selection (0).")
            sys.exit(0)
        if 1 <= idx <= len(orgs):
            return orgs[idx - 1]
        print("Out of range.")

def manual_alias_prompt():
    """Prompt user to manually enter an alias/username when org list is empty."""
    while True:
        entered = input("Enter org alias/username (or press Enter to abort): ")
        if not entered.strip():
            return None
        return entered.strip()

def choose_org(target: str = None):
    """Determine org selection via --org, interactive list, manual entry, or env vars.
    This version decouples from CLI availability checks by directly invoking org list commands.
    """
    # 1. Explicit --org value (try display first, fallback to list scan)
    if target:
        if DEBUG_AUTH:
            print(f"[DEBUG] Explicit target provided: {target}")
        try:
            return get_org_auth(target)
        except Exception as e:
            if DEBUG_AUTH:
                print(f"[DEBUG] get_org_auth failed for {target}: {e}; will try list scan")
            # Attempt to find matching org in list_orgs output to extract token directly
            orgs = list_orgs()
            for o in orgs:
                if target in (o.get("alias"), o.get("username")):
                    inst = o.get("instanceUrl") or o.get("loginUrl")
                    token = o.get("accessToken")
                    if inst and token:
                        if DEBUG_AUTH:
                            print(f"[DEBUG] Using token directly from list for {target}")
                        return inst, token
    # 2. Interactive selection (uses list_orgs even if version checks fail)
    orgs = list_orgs()
    # Stop spinner after first enumeration attempt
    stop_spinner()
    if orgs:
        if DEBUG_AUTH:
            print(f"[DEBUG] Interactive org list length={len(orgs)}")
        org = interactive_select_org()
        if org:
            chosen = org.get("alias") or org.get("username")
            if chosen:
                # Try display; if fails, use direct list token
                try:
                    return get_org_auth(chosen)
                except Exception as e:
                    if DEBUG_AUTH:
                        print(f"[DEBUG] get_org_auth failed for {chosen}: {e}; using list token")
                    inst = org.get("instanceUrl") or org.get("loginUrl")
                    token = org.get("accessToken")
                    if inst and token:
                        return inst, token
        else:
            manual = manual_alias_prompt()
            if manual:
                try:
                    return get_org_auth(manual)
                except Exception as e:
                    if DEBUG_AUTH:
                        print(f"[DEBUG] manual alias get_org_auth failed: {e}; scanning list")
                    for o in orgs:
                        if manual in (o.get("alias"), o.get("username")):
                            inst = o.get("instanceUrl") or o.get("loginUrl")
                            token = o.get("accessToken")
                            if inst and token:
                                return inst, token
    # 3. Environment fallback
    inst = os.getenv("SF_INSTANCE_URL")
    token = os.getenv("SF_ACCESS_TOKEN")
    if inst and token:
        if DEBUG_AUTH:
            print("[DEBUG] Using environment variables for auth fallback")
        return inst, token
    if DEBUG_AUTH:
        print(f"[DEBUG] Auth sources exhausted. SF_INSTANCE_URL={inst} token_present={'yes' if token else 'no'}")
    raise RuntimeError("No org available. Provide --org, log in with sf/sfdx CLI (sf org login web --alias MyAlias), or set SF_INSTANCE_URL/SF_ACCESS_TOKEN.")

def set_auth(instance_url: str, access_token: str):
    global INSTANCE_URL, ACCESS_TOKEN, HEADERS_JSON
    INSTANCE_URL = instance_url.rstrip('/')
    ACCESS_TOKEN = access_token
    HEADERS_JSON = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

# =========================
# Tooling API helpers
# =========================
def tooling_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{INSTANCE_URL}/services/data/v{API_VERSION}/tooling{path}"

def tooling_get(path: str):
    r = requests.get(tooling_url(path), headers=HEADERS_JSON)
    if r.status_code >= 300:
        raise RuntimeError(f"Tooling GET {path} failed: {r.status_code} {r.text}")
    return r.json()

def tooling_describe(sobject: str):
    """Describe a Tooling API sObject, returning its field names (list)."""
    try:
        data = tooling_get(f"/sobjects/{sobject}/describe")
        fields = [f.get("name") for f in data.get("fields", []) if f.get("name")]
        return fields
    except Exception as e:
        if DEBUG_AUTH:
            print(f"[DEBUG] tooling_describe failed for {sobject}: {e}")
        return []

def tooling_query(soql: str):
    """Execute Tooling SOQL with pagination. Returns list of records (dicts)."""
    url = f"/query/?q={quote(soql)}"
    out = []
    while True:
        data = tooling_get(url)
        out.extend(data.get("records", []))
        if not data.get("done"):
            next_url = data.get("nextRecordsUrl")
            prefix = f"/services/data/v{API_VERSION}/tooling"
            url = next_url[len(prefix):] if next_url and next_url.startswith(prefix) else next_url
        else:
            break
    return out

# =========================
# Flow element enumeration (best-effort)
# =========================
def get_flow_elements(flow_version_id: str):
    """Return list of element names/types for a FlowVersion.

    Strategy:
    1. Attempt Tooling API query against FlowElement (if available in current API version).
       Example SOQL: SELECT Id, Name, DeveloperName, ElementType FROM FlowElement WHERE FlowVersionId = '...'
       (Field names differ by version; we probe multiple possibilities.)
    2. If query fails (unsupported object/fields), return empty list.

    NOTE: Salesforce does not currently expose per-element coverage flags in FlowTestCoverage.
    We therefore can only confidently list ALL elements (and treat them all as uncovered when total coverage is zero).
    For partially covered flows, uncovered element names are not derivable from aggregate counts alone.
    """
    # Candidate field sets to maximize compatibility across API versions.
    queries = [
        f"SELECT Id, Name, ElementType FROM FlowElement WHERE FlowVersionId = '{flow_version_id}'",
        f"SELECT Id, DeveloperName, ElementType FROM FlowElement WHERE FlowVersionId = '{flow_version_id}'",
        f"SELECT Id, Name FROM FlowElement WHERE FlowVersionId = '{flow_version_id}'",
    ]
    for q in queries:
        try:
            recs = tooling_query(q)
        except Exception as e:
            # If the object or fields invalid, continue to next variant
            if DEBUG_AUTH:
                print(f"[DEBUG] get_flow_elements query failed: {e}")
            continue
        if not recs:
            continue
        names = []
        for r in recs:
            nm = r.get("Name") or r.get("DeveloperName") or r.get("Id")
            et = r.get("ElementType") or r.get("Type") or ""
            if nm:
                if et:
                    names.append(f"{nm} ({et})")
                else:
                    names.append(nm)
        return names
    return []

def get_flow_elements_via_metadata(api_name: str):
    """Attempt to retrieve Flow metadata XML via sf CLI and parse element names.

    Requires 'sf' CLI. Uses a temporary retrieve into a .tmp_flow_meta directory.
    Parses common element tags (<flow:*>). Returns list of element descriptors.
    Cleans up directory afterward.
    """
    if not sf_cli_available():
        return []
    tmp_dir = "_flow_meta_tmp"
    try:
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(tmp_dir, exist_ok=True)
        # Use CLI to retrieve a single Flow by full name (API name)
        # The command below assumes a scratch/project context isn't mandatory; if it fails, we return empty.
        cmd = [SF_CMD, "project", "retrieve", "start", "--metadata", f"Flow:{api_name}", "--output-dir", tmp_dir, "--json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            if DEBUG_AUTH:
                print(f"[DEBUG] metadata retrieve failed for {api_name}: {proc.stderr.strip()}")
            return []
        # Find flow XML file
        flow_xml = None
        for root, dirs, files in os.walk(tmp_dir):
            for f in files:
                if f.lower() == f"{api_name.lower()}.flow" or f.endswith(".flow"):
                    if api_name.lower() in f.lower():
                        flow_xml = os.path.join(root, f)
                        break
            if flow_xml:
                break
        if not flow_xml:
            if DEBUG_AUTH:
                print(f"[DEBUG] No flow XML found for {api_name} after retrieve")
            return []
        import xml.etree.ElementTree as ET
        try:
            tree = ET.parse(flow_xml)
            root_el = tree.getroot()
        except Exception as e:
            if DEBUG_AUTH:
                print(f"[DEBUG] XML parse failed for {flow_xml}: {e}")
            return []
        ns_strip = lambda tag: tag.split('}')[-1]
        element_names = []
        for el in root_el.iter():
            tag = ns_strip(el.tag)
            # Skip container-level tags that are not actual flow elements
            if tag in {"Flow","processType","status"}:
                continue
            name_attr = el.attrib.get('name') or el.attrib.get('label')
            if not name_attr:
                # Some elements store name in child <name>
                name_child = el.find('./name')
                if name_child is not None and name_child.text:
                    name_attr = name_child.text
            if name_attr:
                descriptor = f"{name_attr} ({tag})"
                element_names.append(descriptor)
        # Deduplicate while preserving order
        seen = set(); ordered = []
        for n in element_names:
            if n not in seen:
                seen.add(n); ordered.append(n)
        return ordered
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

# =========================
# FlowElementTestCoverage (per-element coverage) helper
# =========================
def get_flow_element_test_coverage(flow_version_id: str):
    """Return tuple (covered_names, uncovered_names, all_names) for a FlowVersion using FlowElementTestCoverage object.

    Attempts multiple field name variants for compatibility across API versions:
      ElementName / Name / DeveloperName
      ElementType / Type
      IsCovered / Covered (boolean)

    If querying FlowElementTestCoverage fails (object missing) returns empty lists, allowing fallback.
    """
    global _FETC_FIELD_MAP
    if _FETC_FIELD_MAP is None:
        # Detect available fields once
        fields = tooling_describe("FlowElementTestCoverage")
        name_field = None
        for cand in ["ElementName", "Name", "DeveloperName"]:
            if cand in fields:
                name_field = cand
                break
        covered_field = None
        for cand in ["IsCovered", "Covered"]:
            if cand in fields:
                covered_field = cand
                break
        type_field = None
        for cand in ["ElementType", "Type"]:
            if cand in fields:
                type_field = cand
                break
        _FETC_FIELD_MAP = {
            "name": name_field or "ElementName",  # fallback
            "covered": covered_field,  # may be None
            "type": type_field,        # may be None
        }
        if DEBUG_AUTH:
            print(f"[DEBUG] FlowElementTestCoverage describe fields detected: {_FETC_FIELD_MAP}")
    # Build dynamic SOQL selecting only existing fields
    select_fields = ["FlowVersionId"]
    if _FETC_FIELD_MAP.get("name"):
        select_fields.append(_FETC_FIELD_MAP["name"])
    if _FETC_FIELD_MAP.get("type"):
        select_fields.append(_FETC_FIELD_MAP["type"])
    if _FETC_FIELD_MAP.get("covered"):
        select_fields.append(_FETC_FIELD_MAP["covered"])
    # Deduplicate
    select_fields = list(dict.fromkeys(select_fields))
    soql = f"SELECT {', '.join(select_fields)} FROM FlowElementTestCoverage WHERE FlowVersionId = '{flow_version_id}'"
    query_variants = [soql]
    recs = None
    for q in query_variants:
        try:
            recs = tooling_query(q)
            if recs:
                break
        except Exception as e:
            if DEBUG_AUTH:
                print(f"[DEBUG] FlowElementTestCoverage query failed variant: {e}")
            continue
    if not recs:
        return [], [], []
    covered = []
    uncovered = []
    all_names = []
    for r in recs:
        nm = r.get(_FETC_FIELD_MAP.get("name")) or r.get("Id")
        et = r.get(_FETC_FIELD_MAP.get("type")) if _FETC_FIELD_MAP.get("type") else ""
        flag = r.get(_FETC_FIELD_MAP.get("covered")) if _FETC_FIELD_MAP.get("covered") else None
        if isinstance(flag, str):
            flag = flag.lower() == 'true'
        descriptor = nm if nm else "(unnamed)"
        if et:
            descriptor = f"{descriptor} ({et})"
        all_names.append(descriptor)
        if flag is True:
            covered.append(descriptor)
        elif flag is False:
            uncovered.append(descriptor)
    return covered, uncovered, all_names

# =========================
# Covered versions helper
# =========================
def get_versions_with_any_coverage():
    """Return a set of FlowVersionIds that have any FlowTestCoverage rows.
    
    Uses COUNT_DISTINCT approach as recommended by Salesforce support.
    """
    covered = set()
    # Use COUNT_DISTINCT as recommended by Salesforce
    try:
        # Try COUNT_DISTINCT first (more efficient)
        soql = "SELECT COUNT_DISTINCT(FlowVersionId) cnt FROM FlowTestCoverage"
        recs = tooling_query(soql)
        if recs and recs[0].get("cnt"):
            # If COUNT_DISTINCT works, we still need the actual IDs, so fall back to GROUP BY
            pass
    except Exception:
        pass
    # Get actual FlowVersionIds (GROUP BY approach)
    for r in tooling_query("SELECT FlowVersionId FROM FlowTestCoverage GROUP BY FlowVersionId"):
        fid = r.get("FlowVersionId")
        if fid:
            covered.add(fid)
    return covered

def get_coverage_aggregate(flow_version_id: str):
    """
    Authoritative coverage numbers per FlowVersion.
    
    Strategy:
    1. First, try to use FlowElementTestCoverage to get actual per-element coverage
       (this aggregates across all test methods correctly)
    2. If FlowElementTestCoverage is not available, fall back to FlowTestCoverage
       but use a better aggregation method (not just MAX)
    
    Returns (covered, notCovered).
    """
    # Try FlowElementTestCoverage first (most accurate - shows actual element coverage)
    global _FETC_FIELD_MAP
    if _FETC_FIELD_MAP is None:
        fields = tooling_describe("FlowElementTestCoverage")
        name_field = next((f for f in ["ElementName", "Name", "DeveloperName"] if f in fields), None)
        covered_field = next((f for f in ["IsCovered", "Covered"] if f in fields), None)
        _FETC_FIELD_MAP = {
            "name": name_field or "ElementName",
            "covered": covered_field,
        }
    
    if _FETC_FIELD_MAP.get("covered"):
        # Use FlowElementTestCoverage for accurate per-element coverage
        # This gives us the actual coverage state across all test methods
        try:
            select_fields = ["FlowVersionId", _FETC_FIELD_MAP["covered"]]
            soql = f"SELECT {', '.join(select_fields)} FROM FlowElementTestCoverage WHERE FlowVersionId = '{flow_version_id}'"
            recs = tooling_query(soql)
            covered_count = 0
            uncovered_count = 0
            for r in recs:
                flag = r.get(_FETC_FIELD_MAP["covered"])
                if isinstance(flag, str):
                    flag = flag.lower() == 'true'
                if flag is True:
                    covered_count += 1
                elif flag is False:
                    uncovered_count += 1
            
            # If we got results, return them (FlowElementTestCoverage is authoritative)
            # Empty results might mean no tests run, but we'll still return (0, 0) and let fallback handle it
            if recs:
                return (covered_count, uncovered_count)
            # If no records, the object might not have data yet - fall through to FlowTestCoverage
        except Exception as e:
            if DEBUG_AUTH:
                print(f"[DEBUG] FlowElementTestCoverage query failed for {flow_version_id}: {e}, falling back to FlowTestCoverage")
    
    # Fallback: Use FlowTestCoverage with better aggregation
    # Get total elements from FlowElement query first (to validate numbers)
    total_elements = 0
    try:
        element_recs = tooling_query(f"SELECT Id FROM FlowElement WHERE FlowVersionId = '{flow_version_id}'")
        total_elements = len(element_recs) if element_recs else 0
    except Exception:
        # If FlowElement query fails, we'll use FlowTestCoverage numbers
        pass
    
    # Query FlowTestCoverage - get the record with highest coverage
    # Note: Each FlowTestCoverage record represents coverage from one test method
    # Using the one with highest coverage gives us the most comprehensive view
    soql = f"""
    SELECT NumElementsCovered, NumElementsNotCovered
    FROM FlowTestCoverage
    WHERE FlowVersionId = '{flow_version_id}'
    ORDER BY NumElementsCovered DESC
    LIMIT 1
    """
    try:
        recs = tooling_query(soql)
        if recs:
            r = recs[0]
            m_cov = r.get("NumElementsCovered") or 0
            m_not = r.get("NumElementsNotCovered") or 0
            try:
                covered = int(m_cov or 0)
                not_covered = int(m_not or 0)
                # Validate against total_elements if we have it
                if total_elements > 0:
                    # Ensure consistency: total should match covered + not_covered
                    calculated_total = covered + not_covered
                    if calculated_total != total_elements:
                        # If FlowTestCoverage total doesn't match FlowElement count,
                        # recalculate not_covered based on actual total
                        not_covered = max(0, total_elements - covered)
                return (covered, not_covered)
            except Exception:
                return (int(float(m_cov or 0)), int(float(m_not or 0)))
    except Exception as e:
        if DEBUG_AUTH:
            print(f"[DEBUG] FlowTestCoverage query failed for {flow_version_id}: {e}")
    
    # If we have total_elements but no coverage data, all are uncovered
    if total_elements > 0:
        return (0, total_elements)
    
    # No coverage data and can't determine total elements
    return (0, 0)

# =========================
# Active flows retrieval
# =========================
def get_active_flows():
    """
    Returns ACTIVE Flow versions with related FlowDefinition fields for names.
    Filters by PROCESS_TYPES if provided. Uses same ProcessType values as org summary
    (AutoLaunchedFlow -> AutolaunchedFlow, Workflow -> WorkflowRule) for consistency.
    """
    # Map to Salesforce's canonical values (Flow object may return either variant)
    process_type_map = {
        "AutoLaunchedFlow": "AutolaunchedFlow",
        "AutolaunchedFlow": "AutolaunchedFlow",
        "Workflow": "WorkflowRule",
        "WorkflowRule": "WorkflowRule",
    }
    base = """
    SELECT Id, DefinitionId, Definition.DeveloperName, Definition.MasterLabel,
           MasterLabel, ProcessType, Status, VersionNumber
    FROM Flow
    WHERE Status = 'Active'
    """
    if PROCESS_TYPES:
        # Include both user-supplied and Salesforce canonical names (Flow may return either)
        all_vals = set()
        for p in PROCESS_TYPES:
            all_vals.add(p)
            all_vals.add(process_type_map.get(p, p))
        pts = ",".join([f"'{p}'" for p in sorted(all_vals)])
        base += f" AND ProcessType IN ({pts})"
    return tooling_query(base)

# =========================
# Org-level summary (ACTIVE ONLY)
# =========================
def get_org_level_summary_active_only(process_types=None):
    """
    Org-level flow coverage summary following Salesforce's official formula.
    
    Based on Salesforce support guidance:
    - Numerator: COUNT_DISTINCT(FlowVersionId) FROM FlowTestCoverage (flow versions with coverage)
    - Denominator: Total active autolaunched flows/processes + latest inactive versions with coverage
    
    Uses FlowDefinitionView as recommended by Salesforce (more accurate than Flow).
    Includes latest inactive versions with coverage in denominator (per Salesforce formula).
    
    Formula: (Covered Flow Versions / Total Active Flow Versions) × 100
    """
    # Try FlowDefinitionView first (recommended by Salesforce)
    use_flow_definition_view = False
    total_versions = set()
    
    # Map process types to Salesforce's expected values
    # Salesforce uses 'AutolaunchedFlow' and 'WorkflowRule'
    process_type_map = {
        'AutoLaunchedFlow': 'AutolaunchedFlow',  # Common variant
        'AutolaunchedFlow': 'AutolaunchedFlow',
        'Workflow': 'WorkflowRule',
        'WorkflowRule': 'WorkflowRule',
    }
    
    # Build ProcessType filter
    if process_types:
        # Convert to Salesforce's expected values
        mapped_types = []
        for pt in process_types:
            mapped = process_type_map.get(pt, pt)
            if mapped not in mapped_types:
                mapped_types.append(mapped)
        pts = ",".join([f"'{p}'" for p in mapped_types])
        process_filter = f"ProcessType IN ({pts})"
    else:
        # Default to autolaunched flows and workflows
        process_filter = "ProcessType IN ('AutolaunchedFlow', 'WorkflowRule')"
    
    try:
        # Query FlowDefinitionView for total active versions + latest inactive with coverage
        # Try complex OR query first
        soql = f"""
        SELECT Id, ActiveVersionId, IsLatestVersion, HasTestCoverage, ProcessType
        FROM FlowDefinitionView
        WHERE {process_filter}
        AND (ActiveVersionId != null 
             OR (ActiveVersionId = null AND IsLatestVersion = true AND HasTestCoverage = true))
        """
        defn_recs = tooling_query(soql)
        
        # Collect version IDs
        for r in defn_recs:
            # For active versions, use ActiveVersionId
            active_ver_id = r.get("ActiveVersionId")
            if active_ver_id:
                total_versions.add(active_ver_id)
            # For latest inactive with coverage, we need to get the version ID
            # FlowDefinitionView doesn't directly give us the version ID for inactive
            # So we'll need to query Flow to get the latest version for this definition
            elif r.get("IsLatestVersion") and r.get("HasTestCoverage"):
                def_id = r.get("Id")
                if def_id:
                    # Get the latest version for this definition
                    try:
                        ver_recs = tooling_query(
                            f"SELECT Id FROM Flow WHERE DefinitionId = '{def_id}' AND VersionNumber != null ORDER BY VersionNumber DESC LIMIT 1"
                        )
                        if ver_recs:
                            total_versions.add(ver_recs[0].get("Id"))
                    except Exception:
                        pass
        
        if total_versions:
            use_flow_definition_view = True
    except Exception as e:
        # If the complex OR query fails, try simpler approach
        if DEBUG_AUTH:
            print(f"[DEBUG] FlowDefinitionView complex query failed: {e}, trying simpler query")
        try:
            # Query active versions first
            soql_active = f"""
            SELECT Id, ActiveVersionId, ProcessType
            FROM FlowDefinitionView
            WHERE {process_filter} AND ActiveVersionId != null
            """
            defn_recs = tooling_query(soql_active)
            
            # Collect active version IDs
            for r in defn_recs:
                active_ver_id = r.get("ActiveVersionId")
                if active_ver_id:
                    total_versions.add(active_ver_id)
            
            # Then query latest inactive with coverage
            soql_inactive = f"""
            SELECT Id, IsLatestVersion, HasTestCoverage, ProcessType
            FROM FlowDefinitionView
            WHERE {process_filter} AND ActiveVersionId = null 
            AND IsLatestVersion = true AND HasTestCoverage = true
            """
            inactive_recs = tooling_query(soql_inactive)
            
            # Get version IDs for inactive definitions with coverage
            for r in inactive_recs:
                def_id = r.get("Id")
                if def_id:
                    try:
                        ver_recs = tooling_query(
                            f"SELECT Id FROM Flow WHERE DefinitionId = '{def_id}' AND VersionNumber != null ORDER BY VersionNumber DESC LIMIT 1"
                        )
                        if ver_recs:
                            total_versions.add(ver_recs[0].get("Id"))
                    except Exception:
                        pass
            
            if total_versions:
                use_flow_definition_view = True
        except Exception as e2:
            if DEBUG_AUTH:
                print(f"[DEBUG] FlowDefinitionView query failed: {e2}, falling back to Flow")
            use_flow_definition_view = False
    
    # Fallback to Flow if FlowDefinitionView not available
    if not use_flow_definition_view or not total_versions:
        # Use same process type mapping as FlowDefinitionView path for consistency
        fallback_pts = None
        if process_types:
            mapped = [process_type_map.get(pt, pt) for pt in process_types]
            fallback_pts = ",".join([f"'{p}'" for p in mapped])
        # Pull ACTIVE versions from Flow (fallback)
        soql_active = "SELECT Id, ProcessType FROM Flow WHERE Status = 'Active'"
        if fallback_pts:
            soql_active += f" AND ProcessType IN ({fallback_pts})"
        actives = tooling_query(soql_active)
        total_versions = {r["Id"] for r in actives}
        
        # Also include latest inactive versions with coverage (per Salesforce formula)
        # FlowDefinition.ProcessType exists in Tooling API; if query fails, use Flow-based filter
        process_filter = f"ProcessType IN ({fallback_pts})" if fallback_pts else "1=1"
        try:
            def_recs = tooling_query(f"SELECT Id FROM FlowDefinition WHERE {process_filter}")
        except Exception:
            # Some API versions/orgs may not expose ProcessType on FlowDefinition; get all definitions
            def_recs = tooling_query("SELECT Id FROM FlowDefinition")
        for def_rec in def_recs:
            try:
                def_id = def_rec.get("Id")
                if not def_id:
                    continue
                ver_recs = tooling_query(
                    f"SELECT Id, Status FROM Flow WHERE DefinitionId = '{def_id}' ORDER BY VersionNumber DESC LIMIT 1"
                )
                if ver_recs:
                    latest_ver = ver_recs[0]
                    if latest_ver.get("Status") != "Active":
                        ver_id = latest_ver.get("Id")
                        covered_ids = get_versions_with_any_coverage()
                        if ver_id in covered_ids:
                            total_versions.add(ver_id)
            except Exception:
                pass
    
    active_total = len(total_versions)

    # Which versions have any coverage? (Numerator)
    covered_ids = get_versions_with_any_coverage()
    active_covered = len(total_versions & covered_ids)

    denominator = active_total
    numerator = active_covered
    pct = (numerator / denominator * 100.0) if denominator > 0 else 0.0

    return {
        "active_total": active_total,
        "active_covered": active_covered,
        "denominator": denominator,
        "numerator": numerator,
        "org_percent": round(pct, 2),
    }

# =========================
# Main
# =========================
def main():
    import argparse
    invoked_with_no_args = len(sys.argv) == 1
    parser = argparse.ArgumentParser(
        description="Generate Flow test coverage report from a selected Salesforce org (ACTIVE Flow versions only).",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  1) Interactive selection (spinner, then menu):\n"
            "     python flow_coverage_report.py\n\n"
            "  2) Specify org alias directly (non-interactive):\n"
            "     python flow_coverage_report.py --org MyProdAlias\n\n"
            "  3) Override API version (e.g. 61.0):\n"
            "     python flow_coverage_report.py --org MyProdAlias --api-version 61.0\n\n"
            "  4) List orgs only (no coverage queries):\n"
            "     python flow_coverage_report.py --list-orgs\n\n"
            "  5) Use saved JSON from prior 'sf org list --json':\n"
            "     python flow_coverage_report.py --org-json orgs.json --list-orgs\n\n"
            "  6) Manual credentials (CI/CD or when CLI unavailable):\n"
            "     python flow_coverage_report.py --instance-url https://yourInstance --access-token YOUR_TOKEN\n\n"
            "  7) Enable auth diagnostics & dump org list:\n"
            "     python flow_coverage_report.py --debug-auth --dump-org-json --list-orgs\n\n"
            "  8) Provide aliases fallback (no CLI):\n"
            "     python flow_coverage_report.py --org-aliases MyProdAlias,MySandboxAlias --list-orgs\n\n"
            "Environment Variables:\n"
            "  SF_API_VERSION      -> default API version (e.g. 60.0)\n"
            "  SF_PROCESS_TYPES    -> comma-separated ProcessTypes filter (e.g. AutoLaunchedFlow,Flow)\n"
            "  SF_INSTANCE_URL     -> direct instance URL fallback if selection fails\n"
            "  SF_ACCESS_TOKEN     -> direct token fallback\n"
            "  SF_ORG_JSON_PATH    -> path to saved 'sf org list --json' output for offline listing\n\n"
            "  SF_MAX_TEST_METHODS -> max unique Apex test method entries per Flow (default 300)\n\n"
            "Exit Codes:\n"
            "  0 success / user exit (menu option 0)\n"
            "  1 no orgs found in listing mode\n"
            "  2 other errors (auth, API, parsing)\n\n"
            "Legend in report:\n"
            "  ✔ indicates FlowVersion has at least one FlowTestCoverage record\n"
            "Element Detail (--elements flag):\n"
            "  Adds a single ExecutedElementNames column listing unique FlowElementTestCoverage.ElementName values executed by Apex tests for each active Flow version.\n"
            "Test Method Provenance (always included):\n"
            "  Adds TestMethods column with unique 'ApexClassName.TestMethodName' entries showing which tests exercised each FlowVersion.\n"
            "  Fallback logic: If TestMethodName is not available, resolves ApexTestMethodId -> (ApexClassId, Name) via ApexTestMethod and ApexClass queries.\n"
        )
    )
    parser.add_argument("--org", help="Org alias or username (sf or sfdx CLI). If omitted, interactive selection attempted.")
    parser.add_argument("--debug-auth", action="store_true", help="Enable verbose auth selection diagnostics.")
    parser.add_argument("--api-version", help="Override API version (default env SF_API_VERSION or 60.0).")
    parser.add_argument("--instance-url", help="Direct instance URL (skip CLI org selection).")
    parser.add_argument("--access-token", help="Direct access token (skip CLI org selection).")
    parser.add_argument("--list-orgs", action="store_true", help="List available org aliases/usernames and exit.")
    parser.add_argument("--org-json", help="Path to saved 'sf org list --json' output to use when CLI listing fails.")
    parser.add_argument("--org-aliases", help="Comma-separated aliases if no CLI/org JSON available.")
    parser.add_argument("--dump-org-json", action="store_true", help="After selection, dump parsed org list to flow_orgs_dump.json for troubleshooting.")
    parser.add_argument("--elements", action="store_true", help="Include executed Flow element names (single column ExecutedElementNames from FlowElementTestCoverage).")
    parser.add_argument("--debug-elements", action="store_true", help="Verbose executed element diagnostics (bulk FlowElementTestCoverage query).")
    parser.add_argument("--debug-tests", action="store_true", help="Verbose Apex test method provenance diagnostics (FlowTestCoverage & ApexClass queries).")
    args = parser.parse_args()

    if args.api_version:
        global API_VERSION
        API_VERSION = args.api_version.strip()
    if args.debug_auth:
        global DEBUG_AUTH
        DEBUG_AUTH = True
        # Simple script version hash (count lines + file size) for clarity user is running updated script
        try:
            st = os.stat(__file__)
            version_marker = f"lines={sum(1 for _ in open(__file__, 'r', encoding='utf-8'))} bytes={st.st_size}"
        except Exception:
            version_marker = "(version unknown)"
        print(f"[DEBUG] Auth debug enabled | Script version: {version_marker}")
        print(f"[DEBUG] PATH={os.getenv('PATH')}")

    if args.list_orgs:
        if args.org_json and os.path.isfile(args.org_json):
            try:
                with open(args.org_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                orgs = _parse_org_json(data)
            except Exception as e:
                print(f"ERROR: Could not parse --org-json file: {e}")
                sys.exit(2)
        else:
            orgs = list_orgs()
        if not orgs and args.org_aliases:
            orgs = [{"alias": a.strip(), "username": "(unknown)"} for a in args.org_aliases.split(',') if a.strip()]
        if not orgs:
            print("No orgs found via CLI.")
            sys.exit(1)
        print("Available orgs:")
        for o in orgs:
            alias = o.get("alias") or "(no-alias)"
            uname = o.get("username") or "?"
            inst = o.get("instanceUrl") or o.get("loginUrl") or ""
            print(f"  {alias:25} {uname:45} {inst}")
        if args.dump_org_json:
            try:
                with open("flow_orgs_dump.json", "w", encoding="utf-8") as f:
                    json.dump(orgs, f, indent=2)
                print("[DEBUG] Wrote flow_orgs_dump.json")
            except Exception as e:
                print(f"[DEBUG] Failed writing flow_orgs_dump.json: {e}")
        sys.exit(0)

    if args.instance_url and args.access_token:
        if DEBUG_AUTH:
            print("[DEBUG] Using manual credentials from flags")
        set_auth(args.instance_url.strip(), args.access_token.strip())
        stop_spinner()
    else:
        # When run with no arguments, show hint first so user sees it before org selection
        if invoked_with_no_args:
            print("For supported arguments and usage details, run this script with the --help flag.\n")
        # Inform user we're about to enumerate available org connections (only when not listing only)
        if not args.list_orgs:
            print("Checking available connections... (enumerating authenticated orgs; press Ctrl+C to abort)")
            start_spinner("Enumerating org connections")
        if args.org_json and os.path.isfile(args.org_json) and not args.org:
            try:
                with open(args.org_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                preload = _parse_org_json(data)
                if preload:
                    def _stub_list_orgs():
                        return preload
                    globals()['list_orgs'] = _stub_list_orgs
                    if DEBUG_AUTH:
                        print(f"[DEBUG] Using preload org list from --org-json for selection")
            except Exception as e:
                if DEBUG_AUTH:
                    print(f"[DEBUG] Failed reading --org-json: {e}")
        inst, token = choose_org(args.org)
        set_auth(inst, token)
        stop_spinner()
        if args.dump_org_json:
            try:
                orgs_current = list_orgs()
                with open("flow_orgs_dump.json", "w", encoding="utf-8") as f:
                    json.dump(orgs_current, f, indent=2)
                if DEBUG_AUTH:
                    print("[DEBUG] Wrote flow_orgs_dump.json after selection")
            except Exception as e:
                if DEBUG_AUTH:
                    print(f"[DEBUG] Failed writing flow_orgs_dump.json post-selection: {e}")

    # 1) Per-flow (ACTIVE ONLY, filtered by PROCESS_TYPES)
    flows = get_active_flows()
    covered_versions = get_versions_with_any_coverage()
    # Pre-fetch Apex test method provenance (dynamic field detection)
    test_methods_map = {}  # FlowVersionId -> list of 'ClassName.MethodName'
    provenance_supported = True
    try:
        ftc_fields = tooling_describe("FlowTestCoverage")
        if args.debug_tests or DEBUG_AUTH:
            print(f"[DEBUG] FlowTestCoverage describe fields: {ftc_fields}")
        # Detect possible fields
        field_map = {
            "class_id": next((f for f in ["ApexClassId", "ApexTestClassId"] if f in ftc_fields), None),
            "method_name": next((f for f in ["TestMethodName", "MethodName"] if f in ftc_fields), None),
            "method_id": next((f for f in ["ApexTestMethodId", "TestMethodId"] if f in ftc_fields), None),
        }
        if args.debug_tests or DEBUG_AUTH:
            print(f"[DEBUG] Provenance field map: {field_map}")
        if not any(field_map.values()):
            provenance_supported = False
            if args.debug_tests or DEBUG_AUTH:
                print("[DEBUG] No test method provenance fields present on FlowTestCoverage; column will remain blank.")
        if provenance_supported:
            flow_ids_all = [f.get("Id") for f in flows if f.get("Id")]
            batch_size = 100
            all_ftc_rows = []
            # Build dynamic SELECT list (always FlowVersionId + available fields)
            select_parts = ["FlowVersionId"]
            for key in ["class_id", "method_name", "method_id"]:
                if field_map[key]:
                    select_parts.append(field_map[key])
            for i in range(0, len(flow_ids_all), batch_size):
                batch = flow_ids_all[i:i+batch_size]
                if not batch:
                    continue
                in_clause = ",".join([f"'{fid}'" for fid in batch])
                soql = f"SELECT {', '.join(select_parts)} FROM FlowTestCoverage WHERE FlowVersionId IN ({in_clause})"
                try:
                    recs = tooling_query(soql)
                except Exception as e:
                    if args.debug_tests or DEBUG_AUTH:
                        print(f"[DEBUG] FlowTestCoverage provenance query failed batch starting {i}: {e}")
                    recs = []
                all_ftc_rows.extend(recs)
            # If we only have method_id (ApexTestMethodId) we need to resolve to method + class via ApexTestMethod
            apex_class_name_map = {}
            apex_test_method_map = {}  # methodId -> (classId, methodName)
            if field_map.get("method_id") and not field_map.get("method_name"):
                method_ids = {r.get(field_map["method_id"]) for r in all_ftc_rows if r.get(field_map["method_id"])}
                method_ids = [m for m in method_ids if m]
                for i in range(0, len(method_ids), batch_size):
                    batch = method_ids[i:i+batch_size]
                    in_clause = ",".join([f"'{mid}'" for mid in batch])
                    soql = f"SELECT Id, Name, ApexClassId FROM ApexTestMethod WHERE Id IN ({in_clause})"
                    try:
                        recs = tooling_query(soql)
                    except Exception as e:
                        if args.debug_tests or DEBUG_AUTH:
                            print(f"[DEBUG] ApexTestMethod query failed batch starting {i}: {e}")
                        recs = []
                    for r in recs:
                        mid = r.get("Id")
                        nm = r.get("Name") or mid
                        cid = r.get("ApexClassId")
                        if mid:
                            apex_test_method_map[mid] = (cid, nm)
                # Collect class ids from method map
                class_ids_all = {cid for (cid, _) in apex_test_method_map.values() if cid}
            else:
                # Collect class ids directly from FlowTestCoverage rows if class_id field present
                class_ids_all = {r.get(field_map["class_id"]) for r in all_ftc_rows if field_map.get("class_id") and r.get(field_map["class_id"]) }
            # Resolve ApexClass names when we have class ids
            class_ids_all = [cid for cid in class_ids_all if cid]
            for i in range(0, len(class_ids_all), batch_size):
                batch = class_ids_all[i:i+batch_size]
                in_clause = ",".join([f"'{cid}'" for cid in batch])
                soql = f"SELECT Id, Name FROM ApexClass WHERE Id IN ({in_clause})"
                try:
                    recs = tooling_query(soql)
                except Exception as e:
                    if args.debug_tests or DEBUG_AUTH:
                        print(f"[DEBUG] ApexClass name query failed batch starting {i}: {e}")
                    recs = []
                for r in recs:
                    cid = r.get("Id")
                    nm = r.get("Name") or cid
                    if cid and nm:
                        apex_class_name_map[cid] = nm
            # Assemble provenance entries
            for r in all_ftc_rows:
                fid = r.get("FlowVersionId")
                if not fid:
                    continue
                if field_map.get("method_name"):
                    method_name_val = r.get(field_map["method_name"]) or "(unknownMethod)"
                    class_id_val = r.get(field_map["class_id"]) if field_map.get("class_id") else None
                elif field_map.get("method_id"):
                    mid = r.get(field_map["method_id"])
                    class_id_val, method_name_val = apex_test_method_map.get(mid, (None, mid or "(unknownMethod)"))
                else:
                    continue  # No usable fields
                class_name = apex_class_name_map.get(class_id_val, class_id_val or "(unknownClass)") if class_id_val else "(unknownClass)"
                combined = f"{class_name}.{method_name_val}" if method_name_val else class_name
                test_methods_map.setdefault(fid, []).append(combined)
            # Deduplicate & cap
            for fid, entries in list(test_methods_map.items()):
                dedup = []
                seen = set()
                for e in entries:
                    if e not in seen:
                        seen.add(e)
                        dedup.append(e)
                if len(dedup) > MAX_TEST_METHODS:
                    dedup = dedup[:MAX_TEST_METHODS] + [f"... (+{len(entries) - MAX_TEST_METHODS} more)"]
                test_methods_map[fid] = dedup
            if args.debug_tests or DEBUG_AUTH:
                print(f"[DEBUG] Test method provenance loaded for {len(test_methods_map)} FlowVersions | ApexClass count={len(apex_class_name_map)}")
    except Exception as e:
        provenance_supported = False
        if args.debug_tests or DEBUG_AUTH:
            print(f"[DEBUG] Test method provenance overall failure: {e}")

    # Pre-fetch per-element coverage if requested
    flow_executed_map = {}
    if args.elements:
        if args.debug_elements or DEBUG_AUTH:
            print("[DEBUG] Bulk FlowElementTestCoverage query (executed elements)")
        flow_ids = [f.get("Id") for f in flows if f.get("Id")]
        batch_size = 100
        global _FETC_FIELD_MAP
        if _FETC_FIELD_MAP is None:
            fields = tooling_describe("FlowElementTestCoverage")
            name_field = next((f for f in ["ElementName", "Name", "DeveloperName"] if f in fields), None)
            _FETC_FIELD_MAP = {"name": name_field or "ElementName"}
            if args.debug_elements or DEBUG_AUTH:
                print(f"[DEBUG] Executed element field map: {_FETC_FIELD_MAP}")
        for i in range(0, len(flow_ids), batch_size):
            batch = flow_ids[i:i+batch_size]
            in_clause = ",".join([f"'{fid}'" for fid in batch])
            soql = f"SELECT FlowVersionId, {_FETC_FIELD_MAP['name']} FROM FlowElementTestCoverage WHERE FlowVersionId IN ({in_clause})"
            try:
                recs = tooling_query(soql)
            except Exception as e:
                if args.debug_elements or DEBUG_AUTH:
                    print(f"[DEBUG] Bulk FlowElementTestCoverage query failed: {e}")
                recs = []
            for r in recs:
                vid = r.get("FlowVersionId")
                if not vid:
                    continue
                element_name = r.get(_FETC_FIELD_MAP.get("name")) or r.get("Id")
                if element_name:
                    flow_executed_map.setdefault(vid, []).append(element_name)
        if args.debug_elements or DEBUG_AUTH:
            print(f"[DEBUG] Executed element sets for {len(flow_executed_map)} flows loaded")

    rows = []
    print(f"{'Flow (Label / API)':50}  {'Ver':>3}  {'Type':18}  {'Covered':>7}  {'Not':>5}  {'Total':>5}  {'%':>6}")
    print("-" * 110)

    if not flows:
        print("No active flows found for the selected ProcessTypes.")
    else:
        for f in flows:
            fid = f["Id"]
            defn = f.get("Definition") or {}
            api_name = defn.get("DeveloperName") or (f.get("MasterLabel") or fid)
            label = f.get("MasterLabel") or defn.get("MasterLabel") or api_name
            ptype = f.get("ProcessType") or ""
            ver = f.get("VersionNumber")

            # Authoritative counts from FlowTestCoverage
            covered_count, not_covered_count = get_coverage_aggregate(fid)
            total = covered_count + not_covered_count
            pct = (covered_count / total * 100.0) if total > 0 else 0.0

            # Optional element enumeration
            executed_element_names = []
            if args.elements:
                try:
                    executed_element_names = flow_executed_map.get(fid, [])
                except Exception as e:
                    if DEBUG_AUTH:
                        print(f"[DEBUG] get_flow_elements failed for {fid}: {e}")
                # Deduplicate & cap
                dedup_exec = []
                seen_exec = set()
                for nm in executed_element_names:
                    if nm not in seen_exec:
                        seen_exec.add(nm)
                        dedup_exec.append(nm)
                if len(dedup_exec) > MAX_NAMES:
                    dedup_exec = dedup_exec[:MAX_NAMES] + [f"... (+{len(executed_element_names) - MAX_NAMES} more)"]

            mark = "✔" if fid in covered_versions else " "
            print(f"{mark} {(label + ' / ' + api_name)[:50]:50}  {ver:>3}  {ptype[:18]:18}  "
                  f"{covered_count:>7}  {not_covered_count:>5}  {total:>5}  {pct:>6.2f}")

            rows.append({
                "FlowLabel": label,
                "FlowApiName": api_name,
                "ProcessType": ptype,
                "VersionNumber": ver,
                "FlowVersionId": fid,
                "ElementsTotal": total,
                "ElementsCovered": covered_count,
                "ElementsNotCovered": not_covered_count,
                "CoveragePercent": round(pct, 2),
                "TestMethods": ";".join(test_methods_map.get(fid, [])),
                **({"ExecutedElementNames": ";".join(dedup_exec)} if args.elements else {}),
            })

    print("\nLegend: ✔ = FlowVersion has FlowTestCoverage rows (it contributed to the numerator)")

    # 2) Org-level summary (ACTIVE ONLY)
    summary = get_org_level_summary_active_only(PROCESS_TYPES)
    pt_label = ",".join(PROCESS_TYPES) if PROCESS_TYPES else "ALL"
    print("\n" + "=" * 110)
    print(f"ORG-LEVEL FLOW COVERAGE SUMMARY (ACTIVE ONLY)  [ProcessTypes={pt_label}]")
    print("(Following Salesforce's official formula: Covered Flow Versions / Total Active Flow Versions)")
    print(f"Active versions (total):             {summary['active_total']}")
    print(f"Active versions WITH coverage:       {summary['active_covered']}")
    print(f"Denominator (active + latest inactive with coverage): {summary['denominator']}")
    print(f"Numerator   (covered among active):  {summary['numerator']}")
    print(f"Org-level coverage:                  {summary['org_percent']:.2f}%")
    print("=" * 110 + "\n")

    # 3) CSV export
    out_csv = f"flow_coverage_active_only_{int(time.time())}.csv"
    fieldnames = [
        "FlowLabel","FlowApiName","ProcessType","VersionNumber","FlowVersionId",
        "ElementsTotal","ElementsCovered","ElementsNotCovered","CoveragePercent","TestMethods"
    ]
    # Always include element-related columns when --elements was used for consistency
    if any("ExecutedElementNames" in r for r in rows):
        fieldnames.append("ExecutedElementNames")
    with open(out_csv, "w", newline="", encoding="utf-8") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote CSV: {out_csv}")
    print(f"Org Instance: {INSTANCE_URL}  API Version: {API_VERSION}")
    if any("ExecutedElementNames" in r for r in rows):
        print("Element detail notes: ExecutedElementNames are unique FlowElementTestCoverage.ElementName values executed by Apex tests for each Flow version.")
    print("Test method provenance notes: TestMethods lists unique 'ApexClassName.TestMethodName' entries from FlowTestCoverage rows that exercised each Flow version (capped by SF_MAX_TEST_METHODS). Use --debug-tests for diagnostics.")
    if not provenance_supported:
        print("(TestMethods unsupported: FlowTestCoverage lacks class/method fields in this org/API version.)")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(2)
