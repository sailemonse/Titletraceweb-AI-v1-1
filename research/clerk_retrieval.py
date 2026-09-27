def retrieval_instructions(kind, parcel="", owner="", address=""):
    anchors = [
        f"Property address: {address or 'UNRESOLVED'}",
        f"Parcel / RE: {parcel or 'UNRESOLVED'}",
        f"Owner: {owner or 'UNVERIFIED'}",
    ]

    base = {
        "OFFICIAL_RECORDS": [
            "Search the official recorded-instrument system.",
            "Check deeds and conveyances before and after the relevant ownership date.",
            "Capture instrument number and Book/Page when available.",
        ],
        "COURT_CORE": [
            "Search the official court record using case number or party name.",
            "Capture the docket entry and document reference.",
            "Do not treat a secondary database as proof of a court outcome.",
        ],
        "FORECLOSURE": [
            "Review Final Judgment, Notice of Sale, Certificate of Sale and Certificate of Title when applicable.",
            "Record dates, case number, docket number and relevant amounts.",
        ],
        "TAX_DEEDS": [
            "Search the official tax-deed system by parcel and address.",
            "Record whether a tax-deed case or sale is actually shown.",
        ],
    }

    return anchors + base.get(kind, [
        "Open the official source.",
        "Capture the actual result and source reference.",
    ])
