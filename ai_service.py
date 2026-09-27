from __future__ import annotations

import os

MODEL = os.environ.get("TITLETRACE_AI_MODEL", "gpt-5.6-luna")


def get_api_key():
    return os.environ.get("OPENAI_API_KEY", "")


def configured():
    return bool(get_api_key())


def set_model(model):
    global MODEL
    MODEL = (model or MODEL).strip()


def set_api_key(key):
    os.environ["OPENAI_API_KEY"] = key.strip()


def clear_api_key():
    os.environ.pop("OPENAI_API_KEY", None)


def generate(property_row, findings, evidence):
    address = property_row.get("address") or "UNRESOLVED"
    parcel = property_row.get("parcel") or "UNRESOLVED"
    owner = property_row.get("owner") or "UNVERIFIED"

    lines = [
        "Executive Summary",
        f"Property: {address}",
        f"Parcel / RE: {parcel}",
        f"Owner shown in the current property record: {owner}",
        "",
        "Verified / Recorded Evidence",
    ]

    found = [e for e in evidence if str(e.get("status")).upper() == "FOUND"]

    if found:
        for e in found:
            lines.append(
                f"- {e.get('category')}: {e.get('finding') or 'FOUND; review source reference.'}"
            )
    else:
        lines.append("- No evidence items are currently marked FOUND.")

    lines += [
        "",
        "Open Research Items",
    ]

    if findings:
        for f in findings:
            lines.append(
                f"- {f.get('severity')}: {f.get('title')}. "
                f"Next action: {f.get('next_action')}"
            )
    else:
        lines.append("- No unresolved risk flags were generated.")

    lines += [
        "",
        "Limitations",
        "This is preliminary AI-assisted property research. "
        "It is not a title commitment, title insurance policy, certified title search, "
        "survey, appraisal, or legal opinion.",
    ]

    # The deterministic engine remains usable without an API key.
    return "\n".join(lines), ("live-openai" if configured() else "local-rules")
