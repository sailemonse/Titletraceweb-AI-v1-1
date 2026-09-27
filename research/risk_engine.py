from __future__ import annotations


def analyze(property_row, evidence):
    findings = []

    for e in evidence:
        status = str(e.get("status") or "NOT_SEARCHED").upper()

        if status in {"NOT_SEARCHED", "FOLLOW_UP", "UNAVAILABLE"}:
            severity = "HIGH" if status == "FOLLOW_UP" else "MEDIUM"

            findings.append({
                "severity": severity,
                "title": f"{e.get('category', 'Research item')} requires verification",
                "detail": e.get("finding") or "The underlying official source has not been fully verified.",
                "next_action": f"Review: {e.get('source_url') or 'official source'}",
            })

    if not property_row.get("parcel"):
        findings.append({
            "severity": "HIGH",
            "title": "Parcel identity unresolved",
            "detail": "No parcel / RE number is currently attached to this property record.",
            "next_action": "Verify the address against the official Property Appraiser record.",
        })

    return findings


def summary(findings):
    result = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}

    for finding in findings:
        severity = finding.get("severity", "INFO")
        result[severity] = result.get(severity, 0) + 1

    return result
