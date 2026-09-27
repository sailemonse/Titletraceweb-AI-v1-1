from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

BASE = __import__('pathlib').Path(__file__).resolve().parents[1]
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

PAO_SEARCH_URL = "https://paopropertysearch.coj.net/Basic/Search.aspx"
PAO_DETAIL_URL = "https://paopropertysearch.coj.net/Basic/Detail.aspx?RE={}"

SOURCES = {
    "Property Appraiser": PAO_SEARCH_URL,
    "Official Records": "https://or.duvalclerk.com/",
    "Court / CORE": "https://core.duvalclerk.com/",
    "Tax Deeds": "https://taxdeed.duvalclerk.com/",
    "Jacksonville Building Inspection": "https://www.jacksonville.gov/departments/public-works/building-inspection-division/services",
    "Jacksonville Municipal Code Compliance": "https://www.jacksonville.gov/Departments/Neighborhoods/Municipal-Code-Compliance",
}


def _progress(cb, stage, percent, message, **extra):
    if cb:
        payload = {"stage": stage, "percent": int(percent), "message": message}
        payload.update(extra)
        try:
            cb(payload)
        except Exception:
            pass


def _clean(v):
    return re.sub(r"\s+", " ", str(v or "").replace("\x00", " ")).strip()


def _number(v):
    try:
        x = _clean(v).replace("$", "").replace(",", "")
        return float(x) if x else None
    except Exception:
        return None


def normalize_address(v):
    x = _clean(v).upper()
    x = re.sub(r"[^A-Z0-9]+", " ", x)
    return " ".join(x.split())


def _fetch(url: str, timeout=30, session=None, method="GET", data=None):
    headers = {
        "User-Agent": "TitleTrace-AI/1.0 (property research service)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if session is not None:
        response = session.request(method, url, data=data, timeout=timeout, headers=headers, allow_redirects=True)
        response.raise_for_status()
        return response.status_code, response.headers.get("content-type", ""), response.text, response.url
    req = Request(url, headers=headers, method=method, data=data.encode() if isinstance(data, str) else data)
    with urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("content-type", ""), r.read().decode("utf-8", "replace"), r.geturl()


def _label_text(tag):
    if not tag:
        return ""
    parts = []
    if tag.get("id"):
        label = tag.find_parent().find("label", attrs={"for": tag.get("id")}) if tag.find_parent() else None
        if label:
            parts.append(label.get_text(" ", strip=True))
    for attr in ("aria-label", "placeholder", "name", "id"):
        if tag.get(attr):
            parts.append(tag.get(attr))
    return _clean(" ".join(parts)).lower()


def _control_score(tag, words):
    text = _label_text(tag)
    return sum(1 for w in words if w in text)


def _find_control(soup, words, tag_names=("input", "select")):
    candidates = []
    for name in tag_names:
        for tag in soup.find_all(name):
            typ = (tag.get("type") or "").lower()
            if typ in {"hidden", "submit", "button", "image", "reset"}:
                continue
            score = _control_score(tag, words)
            if score:
                candidates.append((score, tag))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1] if candidates else None


def _form_payload(soup, address):
    form = soup.find("form")
    if not form:
        raise RuntimeError("Property Appraiser search form was not found")
    payload = {}
    for tag in form.find_all(["input", "select", "textarea"]):
        name = tag.get("name")
        if not name:
            continue
        typ = (tag.get("type") or "").lower()
        if typ in {"submit", "button", "image", "reset", "file"}:
            continue
        if tag.name == "select":
            opt = tag.find("option", selected=True) or tag.find("option")
            payload[name] = opt.get("value", "") if opt else ""
        else:
            payload[name] = tag.get("value", "")

    raw = _clean(address)
    zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", raw)
    zip_code = zip_match.group(1) if zip_match else ""
    raw_no_zip = re.sub(r"\b\d{5}(?:-\d{4})?\b", "", raw).strip(" ,")
    # Strip the common Duval municipality/state suffix before identifying the street name.
    raw_no_zip = re.sub(r"\s+(?:JACKSONVILLE(?:\s+BEACH)?|ATLANTIC\s+BEACH|NEPTUNE\s+BEACH|BALDWIN)\s+(?:FL(?:ORIDA)?|F\.?L\.?)\s*$", "", raw_no_zip, flags=re.I)
    m = re.match(r"^\s*(\d+[A-Za-z0-9-]*)\s+(.*)$", raw_no_zip)
    street_num = m.group(1) if m else ""
    street_name = m.group(2) if m else raw_no_zip

    mappings = [
        (("street", "number"), street_num),
        (("street", "#"), street_num),
        (("street", "name"), street_name),
        (("zip",), zip_code),
    ]
    for words, value in mappings:
        ctl = _find_control(soup, words)
        if ctl is not None and ctl.get("name"):
            payload[ctl["name"]] = value

    # Search buttons on this ASP.NET form can have arbitrary generated names.
    button = None
    for b in form.find_all(["input", "button"]):
        text = _clean(b.get("value") or b.get_text(" ", strip=True)).lower()
        if "search" in text:
            button = b
            break
    if button is not None and button.get("name"):
        payload[button["name"]] = button.get("value", "Search")
    return form.get("action") or PAO_SEARCH_URL, payload


def _normalize_re(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) >= 10:
        return digits[:10]
    return digits


def _extract_re_numbers(text):
    text = unescape(str(text or ""))
    patterns = [
        r"(?:[?&]RE=|RE(?:AL\s+ESTATE)?\s*#?\s*[:=]?\s*)([0-9]{6}[- ]?[0-9]{4})",
        r"\b([0-9]{6})[- ]([0-9]{4})\b",
    ]
    found = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            re_num = _normalize_re(m.group(1) if m.groups() else m.group(0))
            if len(re_num) == 10 and re_num not in found:
                found.append(re_num)
    return found


def _extract_detail_links(html, base_url):
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = unescape(a["href"])
        if "Detail.aspx" in href and re.search(r"(?:[?&]RE=)([0-9A-Za-z-]+)", href, re.I):
            links.append(urljoin(base_url, href))
    # Some ASP.NET result controls hide the RE in postback/script markup instead of
    # a normal anchor. Recover those RE numbers and construct the official detail URL.
    for re_num in _extract_re_numbers(html):
        links.append(PAO_DETAIL_URL.format(re_num))
    return list(dict.fromkeys(links))


def _extract_detail(html, url, query_address):
    """Extract identity fields from a live Duval PAO detail page.

    The PAO page is not a simple key/value table: owner is presented as a page
    heading, the site address is a heading/section, and the current (2026)
    assessed value is the right-most value in the Value Summary table.  Keep
    the parser explicit about those structures so we do not accidentally take
    the 2025 certified value or a placeholder legal-description row.
    """
    if BeautifulSoup is None:
        raise RuntimeError("BeautifulSoup is required for live PAO retrieval")

    soup = BeautifulSoup(html, "html.parser")
    text = _clean(soup.get_text(" ", strip=True))

    # RE number: prefer the URL, then explicit page text.
    m = re.search(r"[?&]RE=([0-9A-Za-z-]+)", url, re.I)
    parcel = _normalize_re(m.group(1)) if m else ""
    if not parcel:
        re_candidates = _extract_re_numbers(text)
        parcel = re_candidates[0] if re_candidates else ""

    rows = []
    for tr in soup.find_all("tr"):
        cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)

    def find_row(label):
        needle = label.lower()
        for cells in rows:
            for i, cell in enumerate(cells):
                if cell.lower() == needle or needle in cell.lower():
                    return cells
        return None

    # Owner is the prominent heading immediately before the mailing address.
    owner = ""
    for h2 in soup.find_all(["h1", "h2", "h3"]):
        value = _clean(h2.get_text(" ", strip=True))
        if value and "property detail" not in value.lower() and "primary site" not in value.lower():
            owner = value
            break

    # Site address: first prefer a row explicitly labelled Site Address; then
    # use the visible Primary Site Address section; finally use a regex fallback.
    address = ""
    for label in ("site address", "primary site address", "building 1 site address", "building site address"):
        row = find_row(label)
        if row:
            idx = next((i for i, c in enumerate(row) if label in c.lower()), None)
            if idx is not None:
                for candidate in row[idx + 1:]:
                    if candidate and not re.fullmatch(r"-+", candidate):
                        address = candidate
                        break
            if address:
                break

    if not address:
        # The live page currently renders: Primary Site Address / 203 ANNE AVE /
        # Jacksonville FL 32254-. Capture only the street portion.
        mm = re.search(
            r"Primary Site Address\s+(.+?)\s+Jacksonville\s+FL\s+\d{5}(?:-\d{4})?",
            text, re.I,
        )
        if mm:
            address = _clean(mm.group(1))

    if not address:
        mm = re.search(r"(?:Site Address|Building\s+\d+\s+Site Address)\s+(.+?)(?=\s+(?:Unit|Building Type|Year Built|Property Detail|Official Record)\b|$)", text, re.I)
        address = _clean(mm.group(1)) if mm else ""

    # Legal description: collect the actual legal rows rather than the
    # navigation/header text.  The PAO currently presents lines such as
    # "15-85 13-2S-25E", "WESTWOOD ESTATES", "LOT 85".
    legal_parts = []
    legal_header_seen = False
    for tr in soup.find_all("tr"):
        cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue
        joined = " | ".join(cells).lower()
        if "legal description" in joined and len(cells) <= 3:
            legal_header_seen = True
            continue
        if legal_header_seen:
            if any(k in joined for k in ("buildings", "building 1", "value summary")):
                break
            if len(cells) >= 2 and re.fullmatch(r"\d+", cells[0]):
                legal_parts.append(cells[-1])
    legal = " | ".join(dict.fromkeys(legal_parts))
    if not legal:
        mm = re.search(r"Land\s*&\s*Legal.*?Legal\s+LN\s+Legal Description\s+(.+?)\s+Buildings\b", text, re.I)
        if mm:
            legal = _clean(mm.group(1))

    # Value Summary: select the 2026 In Progress column (right-most numeric
    # value in the Assessed Value row) rather than the 2025 certified value.
    assessed = None
    for cells in rows:
        if cells and cells[0].strip().lower() == "assessed value":
            nums = [_number(c) for c in cells[1:] if _number(c) is not None]
            if nums:
                assessed = nums[-1]
                break
    if assessed is None:
        values = [float(x.replace(",", "")) for x in re.findall(r"Assessed Value\s+\$?([\d,]+(?:\.\d+)?)", text, re.I)]
        if values:
            assessed = values[-1]

    # Current page value is the 2026 in-progress value. Also retain just value
    # as an auxiliary field for the report/UI.
    just_value = None
    for cells in rows:
        if cells and cells[0].strip().lower() in {"just (market) value", "just market value"}:
            nums = [_number(c) for c in cells[1:] if _number(c) is not None]
            if nums:
                just_value = nums[-1]
                break

    return {
        "parcel": parcel,
        "owner": owner,
        "legal_description": legal,
        "assessed_value": assessed,
        "just_value": just_value,
        "address": address or _clean(query_address),
        "address_norm": normalize_address(address or query_address),
        "source_url": url,
        "source_file": "live PAO online parcel database",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _search_duval_property_map(address, cb=None):
    """Best-effort secondary identity resolver using the official Duval GIS map.

    The GIS application is interactive, so this fallback accepts an RE discovered
    in its rendered/search HTML when available. It never invents a parcel number.
    """
    _progress(cb, "pao", 58, "Trying the official Duval Property Map for parcel resolution…")
    try:
        import requests
        session = requests.Session()
        url = "https://maps.coj.net/duvalproperty/"
        r = session.get(url, timeout=30, headers={"User-Agent": "TitleTrace-AI/1.0"})
        r.raise_for_status()
        html = r.text
        # If the map page exposes an address-specific RE in its HTML/state, use it.
        res = _extract_re_numbers(html)
        if len(res) == 1:
            re_num = res[0]
            detail_url = f"https://maps.coj.net/duvalproperty/default.aspx?RE={re_num[:6]}-{re_num[6:]}"
            rr = session.get(detail_url, timeout=30, headers={"User-Agent": "TitleTrace-AI/1.0"})
            if rr.ok:
                text = _clean(BeautifulSoup(rr.text, "html.parser").get_text(" ", strip=True)) if BeautifulSoup else _clean(rr.text)
                mm = re.search(r"Real Estate #\s*:?\s*([0-9]{6})\s*[- ]\s*([0-9]{4})", text, re.I)
                parcel = _normalize_re(mm.group(1)+mm.group(2)) if mm else re_num
                addr_m = re.search(r"Address\s*:?\s*([^\n]+?)(?=\s+Zip Code\s*:|\s+Transaction Price\s*:|$)", text, re.I)
                owner_m = re.search(r"Owner\s*:?\s*(.*?)(?=\s+Address\s*:|\s+Zip Code\s*:|$)", text, re.I)
                return {"source":"Property Appraiser / Duval Property Map", "state":"FOUND", "parcel":parcel,
                        "owner":_clean(owner_m.group(1)) if owner_m else "",
                        "address":_clean(addr_m.group(1)) if addr_m else address,
                        "address_norm":normalize_address(_clean(addr_m.group(1)) if addr_m else address),
                        "source_url":detail_url, "detail":"Parcel resolved from the official Duval Property Map."}
    except Exception:
        pass
    return None



DUVAL_PARCEL_QUERY_URL = "https://maps.coj.net/coj/rest/services/CityBiz/Parcels/MapServer/0/query"
DUVAL_PARCEL_FIELDS = (
    "RE,RE_NOSPACE,LNAMEOWNER,STREET_NO,ST_NAME,ST_TYPE,ST_DIR,"
    "UNIT_NO,ZIPCODE,ADDRCITY,LEGAL1,LEGAL2,LEGAL3,LEGAL4,LEGAL5,CAMA_VAL,"
    "TOT_LND_VA,TOT_BLD_VA"
)

def _parse_input_address(address):
    """Parse a full street address into GIS-friendly components.

    Handles the comma-separated format produced by the structured web form,
    e.g. ``203 Anne Ave, Jacksonville, FL 32254``.  Older versions left the
    city/state attached to ST_NAME, which caused GIS queries such as
    ``ST_NAME = 'ANNE AVE'`` and returned zero parcels.
    """
    raw = _clean(address).replace("\u00a0", " ")
    z = re.search(r"\b(\d{5})(?:-\d{4})?\b", raw)
    zip_code = z.group(1) if z else ""

    # Remove ZIP, then normalize commas so both "Ave, Jacksonville, FL" and
    # "Ave Jacksonville FL" are handled consistently.
    x = re.sub(r"\b\d{5}(?:-\d{4})?\b", "", raw).strip(" ,")
    x = re.sub(r"\s*,\s*", ",", x)
    x = re.sub(r"\s+", " ", x).strip(" ,")

    # Strip a trailing city/state portion. Jacksonville is the normal target,
    # but the other Duval municipality names are supported too.
    x = re.sub(
        r",?\s*(?:JACKSONVILLE(?:\s+BEACH)?|ATLANTIC\s+BEACH|NEPTUNE\s+BEACH|BALDWIN)"
        r"(?:\s*,?\s*(?:FL(?:ORIDA)?|F\.?L\.?))?\s*$",
        "", x, flags=re.I)
    x = re.sub(r"\s*,\s*$", "", x)

    m = re.match(r"^\s*(\d+[A-Za-z0-9-]*)\s+(.+?)\s*$", x)
    if not m:
        return "", "", "", "", zip_code

    number, street = m.group(1), m.group(2).strip(" ,")
    parts = [part for part in street.upper().replace(",", " ").split() if part]
    dirs = {"N","S","E","W","NE","NW","SE","SW"}
    types = {
        "ST","STREET","AVE","AVENUE","RD","ROAD","DR","DRIVE","BLVD","BOULEVARD",
        "LN","LANE","CT","COURT","CIR","CIRCLE","PL","PLACE","PKWY","PARKWAY",
        "HWY","HIGHWAY","WAY","TER","TERRACE","TRL","TRAIL","LOOP","EXPY","EXPRESSWAY",
        "FWY","FREEWAY","CV","COVE","PLZ","PLAZA","RUN","XING","CROSSING"
    }
    direction = parts.pop() if parts and parts[-1] in dirs else ""
    st_type = parts.pop() if parts and parts[-1] in types else ""

    # Defensive cleanup for any city/state tokens that survived unusual input.
    while parts and parts[-1] in {"FL", "FLORIDA"}:
        parts.pop()
    return number, " ".join(parts), st_type, direction, zip_code

def _duval_gis_resolve(address, cb=None):
    """Resolve one address against the live Duval ArcGIS parcel layer.

    This function is intentionally diagnostic-heavy: if ArcGIS rejects a WHERE
    clause, the caller receives the exact query/error instead of a generic
    "parcel unresolved" message.  STREET_NO is treated as both numeric and
    text because the county schema has changed across published layers.
    """
    _progress(cb, "pao", 20, "Resolving the parcel through the official Duval GIS parcel layer…")
    diagnostics = {"input_address": address, "queries": [], "features_returned": 0,
                   "candidate_count": 0, "errors": [], "matched_parcel": ""}
    try:
        import requests
        number, street_name, st_type, direction, zip_code = _parse_input_address(address)
        diagnostics.update({"street_number": number, "street_name": street_name,
                            "street_type": st_type, "direction": direction, "zip": zip_code})
        if not number or not street_name:
            diagnostics["errors"].append("Could not parse street number/street name from address.")
            return None, diagnostics
        esc=lambda v:str(v or "").replace("'", "''")
        n=esc(number); name=esc(street_name.upper()); typ=esc(st_type.upper());
        full=esc(" ".join(x for x in (street_name,st_type,direction) if x).upper())
        raw_norm=normalize_address(address)
        # ArcGIS may define STREET_NO as numeric. Try numeric first, then quoted
        # text, and use ST_NAME/ZIP combinations that do not depend on LONGNAME.
        queries=[
            f"STREET_NO = {n} AND UPPER(ST_NAME) = '{name}'",
            f"STREET_NO = {n} AND UPPER(ST_NAME) = '{name}' AND UPPER(ST_TYPE) = '{typ}'" if typ else f"STREET_NO = {n} AND UPPER(ST_NAME) = '{name}'",
            f"STREET_NO = '{n}' AND UPPER(ST_NAME) = '{name}'",
            f"STREET_NO = {n} AND UPPER(ST_NAME) LIKE '{name}%'",
            f"LONGNAME LIKE '{n} {full}%'",
            f"LONGNAME LIKE '{n} {name}%'",
            f"STREET_NO = {n} AND UPPER(STNM_TYPE) LIKE '{name}%'"
        ]
        session=requests.Session(); features=[]
        for where in queries:
            diagnostics["queries"].append(where)
            try:
                r=session.get(DUVAL_PARCEL_QUERY_URL,params={"where":where,"outFields":"*","returnGeometry":"false","f":"json","resultRecordCount":"100"},timeout=30,headers={"User-Agent":"TitleTrace-AI/1.0"})
                r.raise_for_status(); payload=r.json() or {}
                if payload.get("error"):
                    err=payload.get("error") or {}
                    diagnostics["errors"].append({"where":where,"code":err.get("code"),"message":err.get("message"),"details":err.get("details")})
                    continue
                got=payload.get("features") or []
                diagnostics["features_returned"] += len(got)
                features.extend(got)
            except Exception as exc:
                diagnostics["errors"].append({"where":where,"type":type(exc).__name__,"message":str(exc)})
        unique={}
        for f in features:
            aa=f.get("attributes") or {}; re_num=_normalize_re(aa.get("RE_NOSPACE") or aa.get("RE"))
            if len(re_num)==10: unique[re_num]=f
        diagnostics["candidate_count"] = len(unique)
        if not unique:
            diagnostics["result"]="NO_GIS_MATCH"
            _progress(cb,"pao",38,"Duval GIS returned no address match; trying the official PAO address search…", diagnostics=diagnostics)
            return None, diagnostics
        requested_zip=str(zip_code or "").zfill(5); requested_type=st_type.upper(); requested_dir=direction.upper(); candidates=[]
        for f in unique.values():
            aa=f.get("attributes") or {}; re_num=_normalize_re(aa.get("RE_NOSPACE") or aa.get("RE"))
            row_zip=str(aa.get("ZIPCODE") or "").split(".")[0].zfill(5); row_no=_clean(aa.get("STREET_NO")); row_name=_clean(aa.get("ST_NAME")); row_combined=_clean(aa.get("STNM_TYPE")); row_type=_clean(aa.get("ST_TYPE")); row_dir=_clean(aa.get("ST_DIR")); row_long=_clean(aa.get("LONGNAME"))
            site_street=row_long or " ".join(x for x in (row_no,row_name,row_type,row_dir) if x); city=_clean(aa.get("ADDRCITY") or "Jacksonville"); site=site_street+(f", {city} FL {row_zip}" if row_zip else "")
            legal=" | ".join(_clean(aa.get(k)) for k in ("LEGAL1","LEGAL2","LEGAL3","LEGAL4","LEGAL5") if _clean(aa.get(k)))
            norm_site=normalize_address(site); score=SequenceMatcher(None,raw_norm,norm_site).ratio(); overlap=len(set(raw_norm.split()) & set(norm_site.split()))/max(1,len(raw_norm.split())); score=max(score,overlap)
            if row_no == number: score += .20
            if row_name.upper() == street_name.upper(): score += .20
            elif street_name.upper() in row_combined.upper() or street_name.upper() in row_long.upper(): score += .15
            if requested_type and (row_type.upper()==requested_type or requested_type in row_combined.upper() or requested_type in row_long.upper()): score += .10
            if requested_dir and row_dir.upper()==requested_dir: score += .05
            if requested_zip and row_zip==requested_zip: score += .15
            candidates.append({"parcel":re_num,"owner":_clean(aa.get("LNAMEOWNER")),"legal_description":legal,"assessed_value":_number(aa.get("CAMA_VAL")),"just_value":None,"address":site or address,"address_norm":norm_site,"source_url":f"https://maps.coj.net/duvalproperty/default.aspx?RE={re_num[:6]}-{re_num[6:]}&pao=pao","source_file":"live Jacksonville/Duval County GIS parcel layer","checked_at":datetime.now(timezone.utc).isoformat(),"match_score":score})
        candidates.sort(key=lambda x:x["match_score"],reverse=True)
        diagnostics["top_candidates"]=[{"parcel":c["parcel"],"address":c["address"],"score":round(c["match_score"],4)} for c in candidates[:10]]
        if not candidates or candidates[0]["match_score"]<.60:
            diagnostics["result"]="LOW_CONFIDENCE"
            _progress(cb,"pao",38,"Duval GIS returned candidates, but none passed the safe address-match threshold.",diagnostics=diagnostics)
            return None, diagnostics
        best=candidates[0]; diagnostics["result"]="FOUND"; diagnostics["matched_parcel"]=best["parcel"]
        _progress(cb,"pao",45,f"Parcel resolved by live Duval GIS — {best['parcel']}.",diagnostics=diagnostics)
        best["diagnostics"] = diagnostics
        return best, diagnostics
    except Exception as exc:
        diagnostics["result"]="ERROR"; diagnostics["errors"].append({"type":type(exc).__name__,"message":str(exc)})
        _progress(cb,"pao",38,f"Duval GIS parcel lookup failed; continuing with PAO search ({type(exc).__name__}).",diagnostics=diagnostics)
        return None, diagnostics

def _merge_detail_with_gis(detail, gis):
    out=dict(gis or {})
    for k,v in (detail or {}).items():
        if v not in (None,""):
            out[k]=v
    out["parcel"]=(detail or {}).get("parcel") or (gis or {}).get("parcel") or ""
    out["owner"]=(detail or {}).get("owner") or (gis or {}).get("owner") or ""
    out["legal_description"]=(detail or {}).get("legal_description") or (gis or {}).get("legal_description") or ""
    out["address"]=(detail or {}).get("address") or (gis or {}).get("address") or ""
    out["address_norm"]=normalize_address(out["address"])
    out["source_url"]=(detail or {}).get("source_url") or (gis or {}).get("source_url")
    return out


def search_pao(address, force=False, cb=None):
    """Dynamic live property resolution; no county-wide PAO index is used."""
    _progress(cb,"pao",10,"Starting live property lookup…")
    if BeautifulSoup is None:
        return {"source":"Property Appraiser","state":"UNAVAILABLE","detail":"BeautifulSoup is required for live PAO retrieval."}
    try:
        gis, gis_diagnostics=_duval_gis_resolve(address,cb=cb)
        if gis:
            re_num=gis["parcel"]
            detail={}
            try:
                import requests
                session=requests.Session()
                detail_url=PAO_DETAIL_URL.format(re_num)
                _,_,html,final_url=_fetch(detail_url,timeout=35,session=session)
                detail=_extract_detail(html,final_url,address)
            except Exception:
                detail={}
            best=_merge_detail_with_gis(detail,gis)
            score=SequenceMatcher(None,normalize_address(address),best.get("address_norm","")).ratio()
            if best.get("parcel") and (score >= 0.45 or gis.get("parcel")):
                _progress(cb,"pao",70,f"Live PAO/GIS lookup complete — parcel {best['parcel']}.")
                return {"source":"Property Appraiser","state":"FOUND",
                        "detail":"Property identity resolved dynamically through official Duval GIS and PAO sources.",
                        "results":[best],
                        "diagnostics": gis_diagnostics,"source_url":best.get("source_url",PAO_SEARCH_URL),
                        "checked_at":best.get("checked_at")}
        # Secondary PAO Basic Search resolver.
        _progress(cb,"pao",40,"Trying the live Property Appraiser address search…")
        import requests
        session=requests.Session()
        _,_,search_html,final_url=_fetch(PAO_SEARCH_URL,timeout=30,session=session)
        action,payload=_form_payload(BeautifulSoup(search_html,"html.parser"),address)
        post_url=urljoin(final_url,action)
        _,_,results_html,results_url=_fetch(post_url,timeout=45,session=session,method="POST",data=payload)
        links=_extract_detail_links(results_html,results_url)
        candidates=[]
        for link in list(dict.fromkeys(links))[:15]:
            try:
                _,_,detail_html,detail_url=_fetch(link,timeout=30,session=session)
                rec=_extract_detail(detail_html,detail_url,address)
                if rec.get("parcel"):
                    rec["match_score"]=SequenceMatcher(None,normalize_address(address),rec.get("address_norm","")).ratio()
                    candidates.append(rec)
            except Exception:
                continue
        candidates.sort(key=lambda x:x.get("match_score",0),reverse=True)
        if candidates and candidates[0].get("match_score",0)>=0.60:
            best=candidates[0]
            _progress(cb,"pao",70,f"Live PAO lookup complete — parcel {best['parcel']}.")
            return {"source":"Property Appraiser","state":"FOUND",
                    "detail":"Property identity retrieved dynamically from the official PAO website.",
                    "results":candidates,"source_url":best.get("source_url",PAO_SEARCH_URL),
                    "checked_at":best.get("checked_at")}
        return {"source":"Property Appraiser","state":"NO_MATCH",
                "detail":"The official live Duval parcel and PAO searches did not produce a safely address-matched parcel.",
                "diagnostics": gis_diagnostics,
                "source_url":PAO_SEARCH_URL}
    except Exception as exc:
        _progress(cb,"pao",70,f"Live property lookup failed: {type(exc).__name__}")
        return {"source":"Property Appraiser","state":"UNAVAILABLE",
                "detail":f"{type(exc).__name__}: {exc}","source_url":PAO_SEARCH_URL}

def _probe_one(name, url):
    try:
        import requests
        r = requests.get(url, timeout=20, headers={"User-Agent": "TitleTrace-AI/1.0"}, allow_redirects=True)
        return {"source": name, "state": "AVAILABLE" if r.ok else "HTTP_ERROR", "checked_at": datetime.now(timezone.utc).isoformat(), "detail": f"HTTP {r.status_code}", "source_url": r.url}
    except Exception as exc:
        return {"source": name, "state": "UNAVAILABLE", "checked_at": datetime.now(timezone.utc).isoformat(), "detail": f"{type(exc).__name__}: {exc}", "source_url": url}


def probe_sources(cb=None):
    total = len(SOURCES) - 1
    names = [(n, u) for n, u in SOURCES.items() if n != "Property Appraiser"]
    results = []
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(names)))) as ex:
        futures = {ex.submit(_probe_one, n, u): n for n, u in names}
        done = 0
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            _progress(cb, "records", 72 + int(done * 20 / max(1, total)), f"Checking official sources… {done} of {total}")
    results.sort(key=lambda x: x.get("source", ""))
    return results

