from __future__ import annotations

from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle


BASE = Path(__file__).resolve().parent
OUT = BASE / "reports"
OUT.mkdir(parents=True, exist_ok=True)


def safe_text(value, default=""):
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def safe_html(value, default=""):
    return escape(safe_text(value, default))


def safe_url(url):
    url = safe_text(url)
    if not url:
        return None
    try:
        p = urlparse(url)
        if p.scheme.lower() not in {"http", "https"} or not p.netloc:
            return None
        return url
    except Exception:
        return None


def safe_filename(value):
    value = safe_text(value, "unknown")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    cleaned = "".join(c if c in allowed else "_" for c in value)
    return cleaned[:100] or "unknown"


def money(value):
    try:
        return f"${float(value or 0):,.0f}"
    except (TypeError, ValueError):
        return safe_text(value, "$0")


def draw_page_chrome(canvas, document):
    canvas.saveState()
    width, height = letter
    page = canvas.getPageNumber()

    canvas.setFont("Helvetica-Bold", 8.5)
    canvas.setFillColor(colors.HexColor("#175cd3"))
    canvas.drawString(.55 * inch, height - .36 * inch, "TitleTrace AI")

    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#667085"))
    canvas.drawRightString(
        width - .55 * inch,
        height - .36 * inch,
        "Property Due-Diligence Research Report",
    )

    canvas.setStrokeColor(colors.HexColor("#dbe1e7"))
    canvas.line(
        .55 * inch, height - .45 * inch,
        width - .55 * inch, height - .45 * inch,
    )

    canvas.line(
        .55 * inch, .42 * inch,
        width - .55 * inch, .42 * inch,
    )

    canvas.setFont("Helvetica", 7)
    canvas.drawString(
        .55 * inch, .25 * inch,
        "TitleTrace AI • Preliminary AI-Assisted Research",
    )
    canvas.drawRightString(
        width - .55 * inch, .25 * inch,
        f"Page {page}",
    )

    canvas.restoreState()


def severity_colors(value):
    v = safe_text(value, "INFO").upper()
    if v in {"HIGH", "CRITICAL", "URGENT"}:
        return colors.HexColor("#FEE4E2"), colors.HexColor("#B42318")
    if v in {"MEDIUM", "WARNING", "MODERATE"}:
        return colors.HexColor("#FEF0C7"), colors.HexColor("#B54708")
    if v == "LOW":
        return colors.HexColor("#ECFDF3"), colors.HexColor("#027A48")
    return colors.HexColor("#F2F4F7"), colors.HexColor("#475467")


def generate_pdf(property_row, findings, evidence, narrative, source_urls=None):
    property_row = property_row or {}
    findings = findings or []
    evidence = evidence or []

    pid = safe_filename(
        property_row.get("id")
        or property_row.get("parcel")
        or property_row.get("address")
        or "unknown"
    )

    path = OUT / f"property_{pid}_TitleTrace_Report.pdf"

    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        rightMargin=.55 * inch,
        leftMargin=.55 * inch,
        topMargin=.68 * inch,
        bottomMargin=.58 * inch,
        title="TitleTrace AI Property Due-Diligence Research Report",
        author="TitleTrace AI",
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="TTTitle", parent=styles["Title"],
        fontSize=22, leading=27,
        textColor=colors.HexColor("#17202a"),
        spaceAfter=5, alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="TTSubtitle", parent=styles["Heading3"],
        fontSize=11, leading=14,
        textColor=colors.HexColor("#475467"),
        alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="TTDate", parent=styles["BodyText"],
        fontSize=8, leading=10,
        textColor=colors.HexColor("#667085"),
        alignment=TA_CENTER, spaceAfter=10,
    ))
    styles.add(ParagraphStyle(
        name="TTH", parent=styles["Heading2"],
        fontSize=14, leading=18,
        textColor=colors.HexColor("#175cd3"),
        spaceBefore=13, spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="TTBody", parent=styles["BodyText"],
        fontSize=9.5, leading=13, spaceAfter=5,
        textColor=colors.HexColor("#344054"),
    ))
    styles.add(ParagraphStyle(
        name="TTSmall", parent=styles["BodyText"],
        fontSize=8, leading=10.5,
        textColor=colors.HexColor("#667085"),
    ))
    styles.add(ParagraphStyle(
        name="TTTable", parent=styles["BodyText"],
        fontSize=7.5, leading=9.5,
        textColor=colors.HexColor("#344054"),
    ))

    story = [
        Paragraph("TitleTrace AI", styles["TTTitle"]),
        Paragraph("Property Due-Diligence Research Report", styles["TTSubtitle"]),
        Paragraph(
            f"Report Date: {safe_html(datetime.now().strftime('%B %d, %Y'))}",
            styles["TTDate"],
        ),
    ]

    meta = [
        ["Property", safe_text(property_row.get("address"), "UNRESOLVED")],
        ["Parcel / RE", safe_text(property_row.get("parcel"), "UNRESOLVED")],
        ["Owner", safe_text(property_row.get("owner"), "UNVERIFIED")],
        ["Legal description", safe_text(property_row.get("legal_description"), "UNVERIFIED")],
        ["Assessed value", money(property_row.get("assessed_value"))],
    ]

    table = Table(
        [[
            Paragraph(f"<b>{safe_html(k)}</b>", styles["TTSmall"]),
            Paragraph(safe_html(v), styles["TTTable"]),
        ] for k, v in meta],
        colWidths=[1.35 * inch, 5.8 * inch],
    )

    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#f4f6f8")),
        ("BOX", (0,0), (-1,-1), .5, colors.HexColor("#dbe1e7")),
        ("INNERGRID", (0,0), (-1,-1), .25, colors.HexColor("#e7ebef")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 7),
        ("RIGHTPADDING", (0,0), (-1,-1), 7),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))

    story += [table, Spacer(1, 10), Paragraph("AI Analysis", styles["TTH"])]

    for block in safe_text(narrative, "No AI analysis was provided.").splitlines():
        block = block.strip()
        if not block:
            story.append(Spacer(1, 4))
        elif block.endswith(":"):
            story.append(Paragraph(safe_html(block), styles["Heading3"]))
        else:
            story.append(Paragraph(safe_html(block), styles["TTBody"]))

    story.append(Paragraph("Research / Risk Flags", styles["TTH"]))

    if findings:
        data = [[
            Paragraph("<b>Priority</b>", styles["TTSmall"]),
            Paragraph("<b>Finding</b>", styles["TTSmall"]),
            Paragraph("<b>Next action</b>", styles["TTSmall"]),
        ]]

        commands = [
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f4f6f8")),
            ("GRID", (0,0), (-1,-1), .25, colors.HexColor("#dbe1e7")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 5),
            ("RIGHTPADDING", (0,0), (-1,-1), 5),
            ("TOPPADDING", (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ]

        for i, f in enumerate(findings[:30], start=1):
            sev = safe_text(f.get("severity"), "INFO").upper()
            bg, fg = severity_colors(sev)

            data.append([
                Paragraph(f"<b>{safe_html(sev)}</b>", styles["TTTable"]),
                Paragraph(safe_html(f.get("title")), styles["TTTable"]),
                Paragraph(safe_html(f.get("next_action")), styles["TTTable"]),
            ])

            commands += [
                ("BACKGROUND", (0,i), (0,i), bg),
                ("TEXTCOLOR", (0,i), (0,i), fg),
            ]

        ft = Table(
            data,
            colWidths=[.75*inch, 3.0*inch, 3.4*inch],
            repeatRows=1,
            splitByRow=1,
        )
        ft.setStyle(TableStyle(commands))
        story.append(ft)
    else:
        story.append(Paragraph(
            "No risk flags were generated from the evidence currently recorded.",
            styles["TTBody"],
        ))

    story.append(Paragraph("Evidence Status", styles["TTH"]))

    data = [[
        Paragraph("<b>Category</b>", styles["TTSmall"]),
        Paragraph("<b>Status</b>", styles["TTSmall"]),
        Paragraph("<b>Reference</b>", styles["TTSmall"]),
        Paragraph("<b>Finding</b>", styles["TTSmall"]),
    ]]

    for e in evidence:
        data.append([
            Paragraph(safe_html(e.get("category")), styles["TTTable"]),
            Paragraph(safe_html(e.get("status")), styles["TTTable"]),
            Paragraph(safe_html(e.get("document_ref")), styles["TTTable"]),
            Paragraph(safe_html(e.get("finding")), styles["TTTable"]),
        ])

    if len(data) == 1:
        data.append([
            Paragraph("—", styles["TTTable"]),
            Paragraph("—", styles["TTTable"]),
            Paragraph("—", styles["TTTable"]),
            Paragraph("No evidence records were supplied.", styles["TTTable"]),
        ])

    et = Table(
        data,
        colWidths=[1.45*inch, 1.15*inch, 1.2*inch, 3.4*inch],
        repeatRows=1,
        splitByRow=1,
    )
    et.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f4f6f8")),
        ("GRID", (0,0), (-1,-1), .25, colors.HexColor("#dbe1e7")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 5),
        ("RIGHTPADDING", (0,0), (-1,-1), 5),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    story += [et, Spacer(1, 10)]

    story.append(Paragraph("Important limitation", styles["TTH"]))
    story.append(Paragraph(
        safe_html(
            "This is preliminary AI-assisted property research. It is not a "
            "title commitment, title insurance policy, certified title search, "
            "survey, appraisal, or legal opinion. Unresolved or unavailable "
            "online records require additional verification from the underlying "
            "official source or a qualified professional."
        ),
        styles["TTBody"],
    ))

    valid_sources = []
    for source in source_urls or []:
        if not source or len(source) != 2:
            continue
        name, url = source
        url = safe_url(url)
        if url:
            valid_sources.append((safe_text(name, "Official source"), url))

    if valid_sources:
        story.append(Paragraph("Official Source Directory", styles["TTH"]))
        for name, url in valid_sources:
            story.append(Paragraph(
                f'<link href={quoteattr(url)} color="#175cd3">'
                f"<u>{safe_html(name)}</u></link>",
                styles["TTSmall"],
            ))

    doc.build(
        story,
        onFirstPage=draw_page_chrome,
        onLaterPages=draw_page_chrome,
    )

    return path
