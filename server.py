from __future__ import annotations

import html
import os
import json
import threading
import uuid
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from app import (
    db,
    match,
    seed_property,
    report,
    initialize_clerk_packet,
    update_evidence_status,
)
from research.clerk_packet import REQUIRED_STATUSES, packet
from research.clerk_retrieval import retrieval_instructions
from research.risk_engine import analyze, summary
from ai_service import generate as generate_ai, configured as ai_configured, MODEL as AI_MODEL
from pdf_report import generate_pdf
from research.live_sources import probe_sources, search_pao


APP_NAME = "TitleTrace AI"


def esc(value):
    return html.escape(str(value or ""))


def page(body, title=APP_NAME):
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{margin:0;font-family:Arial,sans-serif;background:#f5f7fa;color:#17202a}}
.wrap{{max-width:1200px;margin:auto;padding:24px}}
.top{{display:flex;justify-content:space-between;gap:20px;margin-bottom:18px}}
.logo{{font-size:28px;font-weight:800}}
.tag,.muted{{color:#667085;font-size:13px}}
.nav{{display:flex;gap:8px;flex-wrap:wrap}}
a{{color:#175cd3;text-decoration:none}}
.card{{background:white;border:1px solid #dbe1e7;border-radius:14px;padding:18px;margin-bottom:16px}}
.hero{{background:linear-gradient(135deg,#fff,#eef4ff);border:1px solid #d7e4ff;border-radius:16px;padding:24px;margin-bottom:18px}}
.grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
.kpi{{background:white;border:1px solid #dbe1e7;border-radius:12px;padding:14px}}
.num{{font-size:24px;font-weight:800}}
.lbl{{font-size:11px;color:#667085}}
input,select,textarea,button{{font:inherit;width:100%;padding:10px 12px;border:1px solid #c8ced6;border-radius:9px;margin-top:6px;box-sizing:border-box}}
button{{background:#175cd3;color:#fff;border-color:#175cd3;font-weight:bold;cursor:pointer}}
label{{font-size:12px;font-weight:bold;color:#475467}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:9px;border-bottom:1px solid #edf0f2;text-align:left;vertical-align:top;font-size:13px}}
th{{font-size:12px;color:#667085;background:#fafbfc}}
.badge{{display:inline-block;padding:4px 8px;border-radius:999px;background:#f2f4f7;font-size:11px;font-weight:bold}}
.high{{background:#fee4e2;color:#b42318}}
.medium{{background:#fef0c7;color:#b54708}}
.success{{padding:12px;background:#edf8f2;border:1px solid #b7e1c7;border-radius:10px;margin-bottom:14px}}
.report{{white-space:pre-wrap;background:#101828;color:#f8fafc;padding:16px;border-radius:10px;font:12px/1.5 monospace;overflow:auto}}
@media(max-width:800px){{.grid3{{grid-template-columns:1fr}}.top{{flex-direction:column}}}}
</style>
</head>
<body><div class="wrap">{body}</div></body>
</html>"""


def save_source_run(pid, result):
    c = db()
    c.execute(
        "INSERT INTO source_runs(property_id,source,state,checked_at,detail,result_json) VALUES (?,?,?,?,?,?)",
        (pid, result.get("source", ""), result.get("state", ""), result.get("checked_at", ""),
         result.get("detail", ""), json.dumps(result, ensure_ascii=False)[:500000]),
    )
    c.commit()
    c.close()



JOB_LOCK = threading.RLock()
JOBS = {}
JOB_TTL_SECONDS = 3600


def _job_update(job_id, **values):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(values)
        job["updated_at"] = time.time()


def _job_progress(job_id, info):
    _job_update(job_id, stage=info.get("stage", "research"), percent=info.get("percent", 0), message=info.get("message", "Researching…"))


def _run_job(job_id, pid, force=False):
    try:
        _job_update(job_id, state="running", percent=5, stage="property", message="Preparing the property file…")
        c = db(); p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone(); c.close()
        if not p:
            raise RuntimeError("Property record not found")

        _job_update(job_id, percent=10, stage="pao", message="Looking up the property live with the Property Appraiser…")
        pao = search_pao(p["address"], force=force, cb=lambda x: _job_progress(job_id, x))
        save_source_run(pid, pao)
        _job_update(job_id, diagnostics=pao.get("diagnostics", {}))
        if pao.get("state") == "UNAVAILABLE":
            raise RuntimeError("Property Appraiser research failed: " + pao.get("detail", "unknown error"))

        candidates = pao.get("results", []) or []
        best = candidates[0] if candidates else {}

        # Defensive handoff: the live resolver diagnostics can prove a parcel was
        # found even if an older/partial PAO response omitted the results array.
        # Never discard a verified GIS match just because the results payload is empty.
        if not best:
            diag = pao.get("diagnostics", {}) or {}
            matched = str(diag.get("matched_parcel") or "").strip()
            if matched:
                top = (diag.get("top_candidates") or [{}])[0] or {}
                best = {
                    "parcel": matched,
                    "address": top.get("address") or p["address"],
                    "match_score": top.get("score", 0),
                    "owner": top.get("owner", ""),
                    "legal_description": top.get("legal_description", ""),
                    "assessed_value": top.get("assessed_value", ""),
                    "diagnostics_recovery": True,
                }
                candidates = [best]
                _job_update(job_id, message=f"Parcel verified by GIS diagnostics: {matched}. Completing the property record…")

        parcel = str(best.get("parcel") or best.get("re") or best.get("RE") or "").strip()
        if not parcel:
            raise RuntimeError("Property Appraiser lookup completed, but the parcel / RE number could not be resolved. The report was not finalized.")
        c = db()
        c.execute("UPDATE properties SET parcel=?, owner=?, legal_description=?, assessed_value=? WHERE id=?", (parcel, best.get("owner", ""), best.get("legal_description", ""), _number(best.get("assessed_value", "")), pid))
        c.commit(); c.close()

        _job_update(job_id, parcel=parcel, owner=best.get("owner", "") or "",
                    legal_description=best.get("legal_description", "") or "",
                    assessed_value=_number(best.get("assessed_value", "")),
                    property_address=p["address"],
                    message=f"Parcel resolved: {parcel}. Checking official record sources…")
        _job_update(job_id, percent=80, stage="records", message=f"Parcel resolved: {parcel}. Checking official record sources…")
        for probe in probe_sources(cb=lambda x: _job_progress(job_id, x)):
            save_source_run(pid, probe)

        _job_update(job_id, percent=96, stage="risk", message="Analyzing property risk and preparing the report…")
        initialize_clerk_packet(pid)
        c = db(); p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone(); evidence = c.execute("SELECT * FROM evidence WHERE property_id=? ORDER BY category,id", (pid,)).fetchall(); c.close()
        findings = analyze(dict(p), [dict(e) for e in evidence])
        _ = findings
        report(pid)
        _job_update(job_id, state="done", percent=100, stage="report", message="Report ready.", pid=pid)
    except Exception as exc:
        print(f"TitleTrace job {job_id} failed: {type(exc).__name__}: {exc}", flush=True)
        _job_update(job_id, state="error", percent=100, stage="error", message=f"Research stopped: {type(exc).__name__}: {exc}", error=f"{type(exc).__name__}: {exc}")


def start_research_job(pid, force=False):
    job_id = uuid.uuid4().hex
    with JOB_LOCK:
        JOBS[job_id] = {"state":"queued","percent":1,"stage":"property","message":"Queued for research…","pid":pid,"created_at":time.time(),"updated_at":time.time(),"parcel":"","owner":"","legal_description":"","assessed_value":None,"property_address":"", "diagnostics":{}}
    t = threading.Thread(target=_run_job, args=(job_id, pid, force), name=f"titletrace-job-{job_id[:8]}", daemon=True)
    t.start()
    return job_id


def cleanup_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with JOB_LOCK:
        for key in list(JOBS):
            if JOBS[key].get("updated_at", 0) < cutoff:
                JOBS.pop(key, None)


def run_live_research(pid, force_pao=False, force_sources=False):
    """Synchronous compatibility wrapper for internal/admin callers."""
    c = db(); p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone(); c.close()
    if not p:
        return {"error":"Property not found"}
    pao = search_pao(p["address"], force=force_pao)
    save_source_run(pid, pao)
    if pao.get("state") == "UNAVAILABLE":
        return pao
    candidates = pao.get("results", [])
    if candidates:
        best = candidates[0]
        c = db(); c.execute("UPDATE properties SET parcel=?,owner=?,legal_description=?,assessed_value=? WHERE id=?", (best.get("parcel",""),best.get("owner",""),best.get("legal_description",""),_number(best.get("assessed_value","")),pid)); c.commit(); c.close()
    for probe in probe_sources():
        save_source_run(pid, probe)
    return pao


class Handler(BaseHTTPRequestHandler):

    def send_page(self, body, code=200, content_type="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/live-research":
            pid = int(q.get("id", [0])[0])
            force = q.get("force", ["0"])[0] == "1"
            result = run_live_research(pid, force_pao=force, force_sources=force)
            c = db(); p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
            runs = c.execute("SELECT * FROM source_runs WHERE property_id=? ORDER BY id DESC LIMIT 20", (pid,)).fetchall(); c.close()
            rows = "".join(
                f"<tr><td>{esc(r['source'])}</td><td><span class='badge'>{esc(r['state'])}</span></td><td>{esc(r['checked_at'])}</td><td>{esc(r['detail'])}</td></tr>"
                for r in runs
            )
            body=f"""
            <div class='top'><div><div class='logo'>Live Source Research</div><div class='tag'>{esc(p['address'])}</div></div><div class='nav'><a href='/run?id={pid}'>AI summary</a><a href='/report?id={pid}'>Report</a></div></div>
            <div class='success'><b>Live research completed.</b> The Property Appraiser online parcel database was queried first. Other official source sites were then checked for availability. Property-specific Clerk searches that require interactive controls remain marked for verification rather than being guessed.</div>
            <div class='card'><h2>Property identity after live PAO check</h2><p><b>Parcel / RE:</b> {esc(p['parcel']) or 'UNRESOLVED'} &nbsp; <b>Owner:</b> {esc(p['owner']) or 'UNVERIFIED'} &nbsp; <b>Assessed:</b> {('$'+format(p['assessed_value'],',.0f')) if p['assessed_value'] else 'UNVERIFIED'}</p><p><b>Legal description:</b> {esc(p['legal_description']) or 'UNVERIFIED'}</p></div>
            <div class='card'><h2>Source checks</h2><table><tr><th>Source</th><th>State</th><th>Checked</th><th>Details</th></tr>{rows}</table></div>
            <div class='card'><p class='muted'>Run again to retrieve the current PAO property record.</p><a href='/live-research?id={pid}&force=1'>Refresh live PAO lookup and rerun</a></div>
            """
            self.send_page(page(body, "TitleTrace Live Research")); return

        if u.path == "/health":
            self.send_page("OK", content_type="text/plain; charset=utf-8")
            return

        if u.path == "/":
            # The homepage is served ONLY from templates/index.html.
            # This prevents an old inline single-address form from ever
            # replacing the structured address intake screen.
            c = db()
            count = c.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
            c.close()
            template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
            try:
                with open(template_path, "r", encoding="utf-8") as fh:
                    index_html = fh.read().replace("__COUNT__", f"{count:,}")
                self.send_page(index_html, headers={"Cache-Control": "no-store, max-age=0"})
            except Exception as exc:
                print(f"TitleTrace index template error: {type(exc).__name__}: {exc}", flush=True)
                self.send_page(
                    page("<div class='card'><h2>TitleTrace index template could not be loaded.</h2><p>Check that templates/index.html is present in the deployed repository.</p></div>"),
                    500,
                )
            return

        if u.path == "/diagnostic-test":
            address = q.get("address", ["203 Anne Ave, Jacksonville, FL 32254"])[0].strip()
            try:
                result = search_pao(address, force=True)
                diag = result.get('diagnostics', {}) or {}
                if address.strip().upper().startswith('203 ANNE AVE'):
                    diag['known_test_expected_parcel'] = '0052970000'
                    found = ''
                    results = result.get('results') or []
                    if results:
                        found = str(results[0].get('parcel') or '')
                    diag['known_test_actual_parcel'] = found
                    diag['known_test_passed'] = bool(found == '0052970000')
                body = f"""<div class='top'><div><div class='logo'>TitleTrace AI Diagnostics</div><div class='tag'>Live parcel resolver test</div></div><a href='/'>Back</a></div>
                <div class='card'><h2>Known-address test</h2><p><b>Address:</b> {esc(address)}</p><p><b>State:</b> {esc(result.get('state'))}</p><p><b>Detail:</b> {esc(result.get('detail'))}</p><pre style='white-space:pre-wrap;background:#101828;color:#f8fafc;padding:16px;border-radius:10px;overflow:auto'>{esc(json.dumps(diag,indent=2,ensure_ascii=False))}</pre></div>"""
                self.send_page(page(body, "TitleTrace Diagnostics"), headers={"Cache-Control":"no-store"})
            except Exception as exc:
                body=f"<div class='card'><h2>Diagnostic test failed</h2><pre>{esc(type(exc).__name__ + ': ' + str(exc))}</pre><a href='/'>Back</a></div>"
                self.send_page(page(body, "TitleTrace Diagnostics"), 500, headers={"Cache-Control":"no-store"})
            return

        if u.path == "/search":
            # Structured address fields are the canonical query format.
            # Legacy free-text address remains supported for compatibility.
            street_number = q.get("street_number", [""])[0].strip()
            street_name = q.get("street_name", [""])[0].strip()
            street_type = q.get("street_type", [""])[0].strip()
            unit = q.get("unit", [""])[0].strip()
            city = q.get("city", ["Jacksonville"])[0].strip() or "Jacksonville"
            state = q.get("state", ["FL"])[0].strip().upper() or "FL"
            zip_code = q.get("zip", [""])[0].strip()
            legacy = q.get("address", [""])[0].strip()
            if street_number and street_name and street_type and zip_code:
                address = f"{street_number} {street_name} {street_type}"
                if unit:
                    address += f" {unit}"
                address += f", {city}, {state} {zip_code}"
            else:
                address = legacy
            if not address:
                self.send_page(page("<div class='card'><h2>Please complete the required address fields.</h2><a href='/'>Back</a></div>"), 400)
                return
            cleanup_jobs()
            rows = match(address)
            if rows and rows[0][0] >= .98:
                pid = rows[0][1]["id"]
            else:
                pid = seed_property(address)
            job_id = start_research_job(pid)
            self.redirect(f"/progress?job={job_id}")
            return

        if u.path == "/progress":
            job_id = q.get("job", [""])[0]
            with JOB_LOCK:
                job = dict(JOBS.get(job_id, {}))
            if not job:
                self.send_page(page("<div class='card'><h2>Research job not found</h2><p>The job may have expired. Start a new search.</p><a href='/'>New search</a></div>"), 404)
                return
            body = f"""
            <div class='top'><div><div class='logo'>TitleTrace AI Research</div><div class='tag'>Property research in progress</div></div><a href='/'>New search</a></div>
            <div class='card'>
              <h2 id='job-title'>{esc(job.get('message','Researching…'))}</h2>
              <div id='resolved-property' style='display:{'block' if job.get('parcel') else 'none'};margin:12px 0;padding:14px;border:1px solid #b7d7c0;border-radius:10px;background:#f3fbf5'>
                <b>Property resolved</b><br>
                <span id='resolved-address'>{esc(job.get('property_address',''))}</span><br>
                <b>Parcel / RE: <span id='resolved-parcel'>{esc(job.get('parcel',''))}</span></b>
                <span id='resolved-owner'>{(' · Owner: ' + esc(job.get('owner',''))) if job.get('owner') else ''}</span>
              </div>
              <div class='progress-track'><div id='job-bar' class='progress-bar' style='width:{int(job.get('percent',1))}%'></div></div>
              <p id='job-message' class='muted'>{esc(job.get('message','Researching…'))}</p>
              <div class='progress-steps'><span id='s1'>Property</span><span id='s2'>PAO</span><span id='s3'>Records</span><span id='s4'>Risk</span><span id='s5'>Report</span></div>
              <div id='job-error'></div>
            </div>
            <script>
            (function(){{
              const job={json.dumps(job_id)};
              const bar=document.getElementById('job-bar'), msg=document.getElementById('job-message'), title=document.getElementById('job-title');
              const resolved=document.getElementById('resolved-property'), parcelEl=document.getElementById('resolved-parcel'), addressEl=document.getElementById('resolved-address'), ownerEl=document.getElementById('resolved-owner');
              const steps={{property:1,pao:2,records:3,risk:4,report:5,error:0}};
              function poll(){{fetch('/job-status?job='+encodeURIComponent(job),{{cache:'no-store'}}).then(r=>r.json()).then(j=>{{
                bar.style.width=(j.percent||0)+'%'; msg.textContent=j.message||'Researching…'; title.textContent=j.state==='error'?'Research stopped':(j.state==='done'?'Report ready':(j.parcel?'Parcel resolved — research continuing':'Researching…'));
                if(j.parcel){{ resolved.style.display='block'; parcelEl.textContent=j.parcel; addressEl.textContent=j.property_address||''; ownerEl.textContent=j.owner?' · Owner: '+j.owner:''; }}
                for(let i=1;i<=5;i++){{const el=document.getElementById('s'+i); if(el) el.classList.toggle('active',i<=(steps[j.stage]||0));}}
                if(j.state==='done'){{window.location='/run?id='+j.pid; return;}}
                if(j.state==='error'){{
                  let safe=String(j.error||j.message||'Research failed').replace(/[<>&]/g,function(c){{return {{'<':'&lt;','>':'&gt;','&':'&amp;'}}[c]}});
                  let diag=''; try{{ if(j.diagnostics && Object.keys(j.diagnostics).length) diag='<details open><summary><b>Parcel lookup diagnostics</b></summary><pre style="white-space:pre-wrap;font-size:12px;background:#f8fafc;padding:12px;border-radius:8px;overflow:auto">'+JSON.stringify(j.diagnostics,null,2).replace(/[<>&]/g,function(c){{return {{'<':'&lt;','>':'&gt;','&':'&amp;'}}[c]}})+'</pre></details>'; }}catch(e){{}}
                  document.getElementById('job-error').innerHTML='<p class="badge high">'+safe+'</p>'+diag+'<p><a href="/">Start a new search</a> · <a href="/retry?job='+encodeURIComponent(job)+'">Run this query again</a></p>'; return;
                }}
                setTimeout(poll,1000);
              }}).catch(function(){{setTimeout(poll,2000);}})}}
              poll();
            }})();
            </script>
            """
            self.send_page(page(body, "TitleTrace Research Progress")); return

        if u.path == "/retry":
            old_job_id = q.get("job", [""])[0]
            with JOB_LOCK:
                old_job = dict(JOBS.get(old_job_id, {}))
            if not old_job or not old_job.get("pid"):
                self.redirect("/")
                return
            new_job = start_research_job(int(old_job["pid"]), force=True)
            self.redirect(f"/progress?job={new_job}")
            return

        if u.path == "/job-status":
            job_id = q.get("job", [""])[0]
            with JOB_LOCK:
                job = dict(JOBS.get(job_id, {}))
            if not job:
                self.send_page(json.dumps({"state":"error","error":"Research job not found"}), 404, "application/json; charset=utf-8", {"Cache-Control":"no-store"})
            else:
                self.send_page(json.dumps(job), content_type="application/json; charset=utf-8", headers={"Cache-Control":"no-store"})
            return

        if u.path == "/run":
            try:
                pid = int(q.get("id", ["0"])[0])
            except ValueError:
                self.send_page(page("<div class='card'><h2>Invalid property ID</h2></div>"), 400)
                return

            c = db()
            p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
            c.close()

            if not p:
                self.send_page(page("<div class='card'><h2>Property not found</h2></div>"), 404)
                return

            c = db()
            existing_run = c.execute("SELECT 1 FROM source_runs WHERE property_id=? LIMIT 1", (pid,)).fetchone()
            c.close()
            if not existing_run:
                job_id = start_research_job(pid)
                self.redirect(f"/progress?job={job_id}")
                return
            initialize_clerk_packet(pid)

            c = db()
            p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
            evidence = c.execute(
                "SELECT * FROM evidence WHERE property_id=? ORDER BY category,id",
                (pid,),
            ).fetchall()
            c.close()

            p_dict = dict(p)
            ev_dict = [dict(e) for e in evidence]
            findings = analyze(p_dict, ev_dict)
            counts = summary(findings)

            body = f"""
            <div class="top">
              <div>
                <div class="logo">TitleTrace AI Run</div>
                <div class="tag">{esc(p['address'])}</div>
              </div>
              <div class="nav">
                <a href="/">New property</a> ·
                <a href="/research?id={pid}">Evidence</a> ·
                <a href="/report?id={pid}">Report</a>
              </div>
            </div>

            <div class="success">
              <b>Research file initialized.</b>
              Official-source evidence items are being tracked individually.
            </div>

            <div class="grid3">
              <div class="kpi">
                <div class="num">{counts.get('HIGH', 0)}</div>
                <div class="lbl">High-priority flags</div>
              </div>
              <div class="kpi">
                <div class="num">{counts.get('MEDIUM', 0)}</div>
                <div class="lbl">Medium-priority flags</div>
              </div>
              <div class="kpi">
                <div class="num">{sum(e['status'] == 'FOUND' for e in evidence)}</div>
                <div class="lbl">Verified evidence items</div>
              </div>
            </div>

            <div class="card">
              <h2>Property identity</h2>
              <table>
                <tr><th>Address</th><td>{esc(p['address'])}</td></tr>
                <tr><th>Parcel / RE</th><td>{esc(p['parcel']) or 'UNRESOLVED'}</td></tr>
                <tr><th>Owner</th><td>{esc(p['owner']) or 'UNVERIFIED'}</td></tr>
                <tr><th>Legal description</th><td>{esc(p['legal_description']) or 'UNVERIFIED'}</td></tr>
                <tr><th>Assessed value</th><td>${float(p['assessed_value'] or 0):,.0f}</td></tr>
              </table>
            </div>

            <div class="card">
              <h2>Research flags</h2>
            """

            if findings:
                body += """
                <table>
                <tr><th>Priority</th><th>Finding</th><th>Next action</th></tr>
                """
                for f in findings[:30]:
                    cls = "high" if f["severity"] == "HIGH" else "medium"
                    body += f"""
                    <tr>
                      <td><span class="badge {cls}">{esc(f['severity'])}</span></td>
                      <td><b>{esc(f['title'])}</b><br>{esc(f['detail'])}</td>
                      <td>{esc(f['next_action'])}</td>
                    </tr>
                    """
                body += "</table>"
            else:
                body += "<p>No unresolved flags.</p>"

            body += f"""
            </div>

            <div class="card">
              <h2>Next steps</h2>
              <p class="muted">
                Record the actual results from official sources before relying
                on any finding as verified.
              </p>
              <p>
                <a href="/research?id={pid}">Open evidence workflow →</a>
                &nbsp; | &nbsp;
                <a href="/report?id={pid}">View report →</a>
                &nbsp; | &nbsp;
                <a href="/download-pdf?id={pid}">Download PDF →</a>
              </p>
            </div>
            """

            self.send_page(page(body))
            return

        if u.path == "/research":
            try:
                pid = int(q.get("id", ["0"])[0])
            except ValueError:
                self.send_page(page("<div class='card'><h2>Invalid property ID</h2></div>"), 400)
                return

            if q.get("evidence_id") and q.get("status"):
                update_evidence_status(
                    int(q["evidence_id"][0]),
                    q["status"][0],
                    q.get("finding", [""])[0],
                    q.get("document_ref", [""])[0],
                )
                self.redirect(f"/research?id={pid}")
                return

            initialize_clerk_packet(pid)

            c = db()
            p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
            evidence = c.execute(
                "SELECT * FROM evidence WHERE property_id=? ORDER BY category,id",
                (pid,),
            ).fetchall()
            c.close()

            body = f"""
            <div class="top">
              <div>
                <div class="logo">Evidence Capture</div>
                <div class="tag">{esc(p['address'])}</div>
              </div>
              <a href="/run?id={pid}">Back to AI summary</a>
            </div>
            """

            for e in evidence:
                key = {
                    "Official Records": "OFFICIAL_RECORDS",
                    "Court / CORE": "COURT_CORE",
                    "Tax Deeds": "TAX_DEEDS",
                }.get(e["category"], "")

                instructions = retrieval_instructions(
                    key,
                    p["parcel"],
                    p["owner"],
                    p["address"],
                )

                options = "".join(
                    f"<option value='{esc(s)}' "
                    f"{'selected' if s == e['status'] else ''}>{esc(s)}</option>"
                    for s in REQUIRED_STATUSES
                )

                body += f"""
                <div class="card">
                  <h2>{esc(e['category'])}</h2>
                  <p><a target="_blank" href="{esc(e['source_url'])}">
                    Open official source →
                  </a></p>

                  <ol>
                """

                for instruction in instructions:
                    body += f"<li class='muted'>{esc(instruction)}</li>"

                body += f"""
                  </ol>

                  <form>
                    <input type="hidden" name="id" value="{pid}">
                    <input type="hidden" name="evidence_id" value="{e['id']}">

                    <label>Status</label>
                    <select name="status">{options}</select>

                    <label>Document / record reference</label>
                    <input name="document_ref"
                           value="{esc(e['document_ref'])}"
                           placeholder="Instrument #, Book/Page, case/docket #">

                    <label>What did the official source show?</label>
                    <textarea name="finding"
                              placeholder="Record the actual result.">{esc(e['finding'])}</textarea>

                    <button>Save evidence</button>
                  </form>
                </div>
                """

            self.send_page(page(body))
            return

        if u.path == "/report":
            pid = int(q.get("id", ["0"])[0])
            c = db()
            has_run = c.execute("SELECT 1 FROM source_runs WHERE property_id=? LIMIT 1", (pid,)).fetchone()
            c.close()
            if not has_run:
                job_id = start_research_job(pid)
                self.redirect(f"/progress?job={job_id}")
                return

            c = db()
            p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
            evidence = c.execute(
                "SELECT * FROM evidence WHERE property_id=? ORDER BY category,id",
                (pid,),
            ).fetchall()
            c.close()

            p_dict = dict(p)
            ev_dict = [dict(e) for e in evidence]
            findings = analyze(p_dict, ev_dict)

            ai_text, ai_mode = generate_ai(
                p_dict,
                findings,
                ev_dict,
            )

            text_path = report(pid)

            body = f"""
            <div class="top">
              <div>
                <div class="logo">TitleTrace AI Report</div>
                <div class="tag">{esc(p['address'])}</div>
              </div>
              <div>
                <a href="/run?id={pid}">AI summary</a> ·
                <a href="/research?id={pid}">Evidence</a> ·
                <a href="/download-pdf?id={pid}">Download PDF</a>
              </div>
            </div>

            <div class="card">
              <h2>{esc('Live AI narrative' if ai_mode == 'live-openai' else 'Local rules narrative')}</h2>
              <div class="report">{esc(ai_text)}</div>
            </div>

            <div class="card">
              <h2>Evidence-backed report</h2>
              <div class="report">{esc(text_path.read_text(encoding='utf-8'))}</div>
            </div>
            """

            self.send_page(page(body))
            return

        if u.path == "/download-pdf":
            pid = int(q.get("id", ["0"])[0])

            c = db()
            p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
            evidence = c.execute(
                "SELECT * FROM evidence WHERE property_id=? ORDER BY category,id",
                (pid,),
            ).fetchall()
            c.close()

            p_dict = dict(p)
            ev_dict = [dict(e) for e in evidence]
            findings = analyze(p_dict, ev_dict)
            ai_text, _ = generate_ai(p_dict, findings, ev_dict)

            sources = [
                (s["category"], s["url"])
                for s in packet(
                    p["address"],
                    p["parcel"],
                    p["owner"],
                )["sources"]
            ]

            path = generate_pdf(
                p_dict,
                findings,
                ev_dict,
                ai_text,
                sources,
            )

            self.send_page(
                path.read_bytes(),
                content_type="application/pdf",
                headers={
                    "Content-Disposition":
                    f'attachment; filename="TitleTrace_Property_{pid}.pdf"'
                },
            )
            return

        if u.path == "/ai-status":
            status = "LIVE OPENAI" if ai_configured() else "LOCAL RULES"

            body = f"""
            <div class="card">
              <h2>AI engine status</h2>
              <p><span class="badge">{status}</span></p>
              <p class="muted">
                Model: {esc(AI_MODEL)}
              </p>
              <p>
                The deterministic evidence and risk engine works without
                an API key.
              </p>
              <a href="/">Back to TitleTrace</a>
            </div>
            """

            self.send_page(page(body))
            return

        self.send_page(
            page("<div class='card'><h2>Not found</h2></div>"),
            404,
        )

    def log_message(self, format, *args):
        try:
            print("TitleTrace HTTP: " + (format % args), flush=True)
        except Exception:
            print("TitleTrace HTTP request", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"TitleTrace AI listening on 0.0.0.0:{port}", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()
