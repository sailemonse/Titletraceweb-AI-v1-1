CORE_URL = "https://core.duvalclerk.com/"


def build_session(pid, parcel="", owner="", address="", case_number=""):
    return {
        "property_id": pid,
        "parcel": parcel,
        "owner": owner,
        "address": address,
        "case_number": case_number,
    }


def search_targets(session):
    return [
        ("Address", session.get("address", "")),
        ("Parcel / RE", session.get("parcel", "")),
        ("Owner", session.get("owner", "")),
        ("Case number", session.get("case_number", "")),
    ]


def checklist():
    return [
        "Final Judgment, if a foreclosure case exists.",
        "Notice of Sale.",
        "Proof of Publication when applicable.",
        "Auction or sale result.",
        "Certificate of Sale.",
        "Certificate of Title.",
        "Any subsequent recorded conveyance.",
    ]
