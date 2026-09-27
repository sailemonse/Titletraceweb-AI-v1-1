from __future__ import annotations

REQUIRED_STATUSES = [
    "NOT_SEARCHED",
    "FOLLOW_UP",
    "FOUND",
    "NOT_FOUND",
    "UNAVAILABLE",
]

OFFICIAL_SOURCES = [
    {
        "category": "Property Appraiser",
        "url": "https://paopropertysearch.coj.net/",
        "description": "Duval County Property Appraiser parcel research.",
        "searches": ["Address", "Parcel / RE number", "Owner"],
        "status": "PAO",
    },
    {
        "category": "Official Records",
        "url": "https://or.duvalclerk.com/",
        "description": "Recorded deeds, mortgages, liens and other instruments.",
        "searches": ["Parcel", "Address", "Owner", "Book/Page"],
        "status": "OFFICIAL_RECORDS",
    },
    {
        "category": "Court / CORE",
        "url": "https://core.duvalclerk.com/",
        "description": "Duval County court and foreclosure records.",
        "searches": ["Case number", "Party", "Property"],
        "status": "COURT_CORE",
    },
    {
        "category": "Tax Deeds",
        "url": "https://taxdeed.duvalclerk.com/",
        "description": "Duval County tax-deed research.",
        "searches": ["Parcel", "Address", "Tax deed"],
        "status": "TAX_DEEDS",
    },
    {
        "category": "Jacksonville Building Inspection",
        "url": "https://www.jacksonville.gov/departments/public-works/building-inspection-division/services",
        "description": "Permit and building research.",
        "searches": ["Address", "Permit", "Certificate of Occupancy"],
        "status": "MUNICIPAL",
    },
    {
        "category": "Jacksonville Municipal Code Compliance",
        "url": "https://www.jacksonville.gov/Departments/Neighborhoods/Municipal-Code-Compliance",
        "description": "Code compliance and property-safety research.",
        "searches": ["Address", "Code cases", "Municipal liens"],
        "status": "CODE",
    },
]


def packet(address="", parcel="", owner=""):
    return {
        "address": address or "",
        "parcel": parcel or "",
        "owner": owner or "",
        "sources": OFFICIAL_SOURCES,
    }
