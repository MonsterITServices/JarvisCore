import io
import json
import subprocess
import sys

from devices.home_assistant import _ALLOWED_ACTIONS, _allowed_entities


class SecurityAuditError(Exception):
    pass


def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def run_jarvis_self_audit(app, server_host, rules):
    checks = []
    checks.append(_check(
        "Network binding",
        "pass" if server_host == "127.0.0.1" else "fail",
        "Jarvis listens on loopback only." if server_host == "127.0.0.1" else "Jarvis is configured to listen beyond this computer.",
    ))
    debug_enabled = bool(app.debug or app.config.get("DEBUG", False))
    checks.append(_check(
        "Debug mode",
        "fail" if debug_enabled else "pass",
        "Debug mode is enabled." if debug_enabled else "Debug mode is disabled.",
    ))
    upload_limit = app.config.get("MAX_CONTENT_LENGTH")
    upload_ok = isinstance(upload_limit, int) and upload_limit <= 30 * 1024 * 1024
    checks.append(_check(
        "Upload size limit",
        "pass" if upload_ok else "fail",
        f"Maximum request size is {upload_limit // (1024 * 1024)} MB." if upload_ok else "No safe request-size limit is configured.",
    ))

    security_policy = rules.get("security_policy", {})
    external_access = security_policy.get("allow_external_access") is True
    checks.append(_check(
        "On-demand internet access",
        "review" if external_access else "pass",
        "External HTTPS page reads and configured web search are enabled only through explicit Jarvis requests."
        if external_access else "External access is disabled by config/rules.json.",
    ))

    configured_entities = _allowed_entities()
    checks.append(_check(
        "Home Assistant scope",
        "pass",
        f"{len(configured_entities)} explicitly allowlisted entities; no network discovery is performed.",
    ))
    checks.append(_check(
        "Kali runner",
        "review",
        "No SSH bridge or remote command execution is configured; active network scanning is unavailable.",
    ))

    try:
        test_client = app.test_client()
        remote_rules = test_client.post(
            "/api/assistant-rules",
            json={"hard_rules": "test"},
            headers={"Origin": "https://untrusted.invalid"},
        )
        unconfirmed_device = test_client.post(
            "/api/home-assistant/control",
            json={"entity_id": "light.test", "service": "turn_on"},
        )
        unsupported_upload = test_client.post(
            "/api/knowledge",
            data={"file": (io.BytesIO(b"test"), "unexpected.exe")},
            content_type="multipart/form-data",
        )
        protections_pass = remote_rules.status_code == 403 and unconfirmed_device.status_code == 400 and unsupported_upload.status_code == 400
        checks.append(_check(
            "Request guardrails",
            "pass" if protections_pass else "fail",
            "Remote rule edits, unconfirmed device calls, and unsupported uploads are rejected."
            if protections_pass else "One or more request guardrails did not return the expected rejection.",
        ))
    except Exception as error:
        checks.append(_check("Request guardrails", "fail", f"Self-test could not complete: {type(error).__name__}."))

    allowed_services = rules.get("home_assistant", {}).get("allowed_services", {})
    unexpected_services = []
    if isinstance(allowed_services, dict):
        for domain, services in allowed_services.items():
            if not isinstance(services, list):
                unexpected_services.append(domain)
                continue
            safe_services = _ALLOWED_ACTIONS.get(domain, set())
            if any(not isinstance(service, str) or service not in safe_services for service in services):
                unexpected_services.append(domain)
    else:
        unexpected_services.append("home_assistant")
    checks.append(_check(
        "Device service allowlist",
        "fail" if unexpected_services else "pass",
        "Unexpected service entries: " + ", ".join(unexpected_services)
        if unexpected_services else "Configured services stay within the app's limited light/switch/fan/climate allowlist.",
    ))

    return {
        "scope": "This Jarvis installation only; no host discovery, external target probes, or exploit tests are run.",
        "checks": checks,
        "passed": sum(check["status"] == "pass" for check in checks),
        "review": sum(check["status"] == "review" for check in checks),
        "failed": sum(check["status"] == "fail" for check in checks),
    }


def audit_local_dependencies(timeout=90):
    command = [
        sys.executable,
        "-m",
        "pip_audit",
        "--local",
        "--format=json",
        "--desc=off",
        "--aliases=off",
        "--progress-spinner=off",
        "--timeout=15",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise SecurityAuditError("The installed-package audit timed out.") from error
    except OSError as error:
        raise SecurityAuditError("Could not start pip-audit in the current Python environment.") from error

    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        detail = result.stderr.strip()[-1000:] or "No JSON report was returned."
        raise SecurityAuditError(f"The package audit could not complete: {detail}") from error

    dependencies = report.get("dependencies", [])
    findings_by_key = {}
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        for vulnerability in dependency.get("vulns", []):
            if not isinstance(vulnerability, dict):
                continue
            finding = {
                "name": str(dependency.get("name", "unknown"))[:200],
                "version": str(dependency.get("version", "unknown"))[:80],
                "id": str(vulnerability.get("id", "unknown"))[:100],
                "aliases": [str(alias)[:100] for alias in vulnerability.get("aliases", [])[:10]],
                "fix_versions": [str(version)[:80] for version in vulnerability.get("fix_versions", [])[:10]],
            }
            key = (finding["name"].lower(), finding["version"], finding["id"])
            if key not in findings_by_key:
                findings_by_key[key] = finding
            else:
                current = findings_by_key[key]
                current["aliases"] = sorted(set(current["aliases"]) | set(finding["aliases"]))[:10]
                current["fix_versions"] = sorted(set(current["fix_versions"]) | set(finding["fix_versions"]))[:10]
    findings = list(findings_by_key.values())
    return {
        "complete": result.returncode in {0, 1},
        "packages_scanned": len(dependencies),
        "vulnerability_count": len(findings),
        "findings": findings[:200],
        "source": "PyPI vulnerability database via pip-audit",
        "network_note": "Only package names and versions are checked against the advisory service; no target hosts are contacted.",
    }
