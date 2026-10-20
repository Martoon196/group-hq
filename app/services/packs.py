"""The packs: the Local Partner prospectus and the territory report for one opportunity, as HTML print views.

No PDF library is used. Each pack is a self-contained HTML page with an A4 print stylesheet
(app/templates/packs/_print_base.html); the browser's Print to PDF makes the file that is sent. WeasyPrint
was not added because it needs system libraries (Pango, Cairo) that the Render Python runtime does not
carry, and a print view is enough for a pack that head office prints and attaches by hand.

Money rule: every figure the territory model produces is a proposal until the founder confirms the
assumptions behind it (app.services.territory_rules, the assumptions table). While any assumption is
unconfirmed a pack carries a [CONFIRM] watermark and shows NO money at all: no quoted fee, no modelled
earnings, no fee per unit. Public facts about a district (private-rented households, ONS rents, licensing)
are not money and are shown either way. HQ's own contribution figure is never in a pack; it is internal.

The prospectus is converted from the Hello New Home workspace's PARTNER-PROSPECTUS.md at build time with
markdown_to_html() below (a small routine covering what that document uses: headings, paragraphs, lists,
tables, blockquotes, rules, bold, italics, code and links) and shipped as
app/templates/packs/prospectus_body.html. Re-run the conversion when the prospectus changes:

    .venv/bin/python -m app.services.packs <path to PARTNER-PROSPECTUS.md> > app/templates/packs/prospectus_body.html
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from markupsafe import escape

from .. import config

PROSPECTUS_BODY = config.APP_DIR / "templates" / "packs" / "prospectus_body.html"
WATERMARK = "DRAFT [CONFIRM]"
WITHHELD = "Figures withheld until the founder confirms the territory model."


# ---- money as text ------------------------------------------------------------------------------------
def pounds(pence: int | None) -> str:
    """1300000 pence as £13,000.00; None as an empty string."""
    if pence is None:
        return ""
    value = Decimal(int(pence)).scaleb(-2).quantize(Decimal("0.01"))
    return f"£{value:,.2f}"


def money(amount: Any) -> str:
    """Decimal('144') as £144.00; None as an empty string."""
    if amount is None:
        return ""
    return f"£{Decimal(str(amount)).quantize(Decimal('0.01')):,.2f}"


def to_pence(amount: Any) -> int:
    """Decimal('13000.00') as 1300000, half pennies up."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1")))


def number(value: Any) -> str:
    """3500 as 3,500; None as a dash the print view can show."""
    if value is None:
        return ""
    return f"{int(value):,}"


# ---- the confirmation state --------------------------------------------------------------------------------
def assumptions_confirmed(db) -> bool:
    """True only when the assumptions table exists with rows and every one is confirmed. An empty table (a
    database that has not been seeded) counts as unconfirmed, so a pack can never show money by accident."""
    from ..models import Assumption
    rows = db.query(Assumption.confirmed).all()
    return bool(rows) and all(confirmed for (confirmed,) in rows)


def unconfirmed_count(db) -> int:
    from ..services.territory_rules import unconfirmed_assumptions
    return len(unconfirmed_assumptions(db))


# ---- a small markdown routine -------------------------------------------------------------------------------
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$")
_UL = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_OL = re.compile(r"^\s{0,3}\d+[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_SAFE_HREF = re.compile(r"^(https?://|mailto:|/|#)", re.I)


def inline(text: str) -> str:
    """Inline markdown on one escaped line: code spans, bold, italics and links. The text is HTML-escaped
    first, so nothing in the source document can inject markup into a pack."""
    out = str(escape(text))
    codes: list[str] = []

    def keep(m: re.Match) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    out = _CODE_SPAN.sub(keep, out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_STAR.sub(r"<em>\1</em>", out)
    out = _ITALIC_UNDER.sub(r"<em>\1</em>", out)

    def link(m: re.Match) -> str:
        label, href = m.group(1), m.group(2)
        if not _SAFE_HREF.match(href):
            return label
        return f'<a href="{href}">{label}</a>'

    out = _LINK.sub(link, out)
    for index, code in enumerate(codes):
        out = out.replace(f"\x00{index}\x00", code)
    return out


def _table_cells(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def markdown_to_html(text: str) -> str:
    """The document as HTML. Block structure is decided line by line: a blank line ends a paragraph or a
    list; a table is a run of lines starting with a pipe whose second line is the header separator; a
    blockquote is a run of lines starting with >; everything else is a paragraph. Good enough for the
    prospectus and the heads of terms, which use nothing beyond that."""
    lines = text.replace("\r\n", "\n").split("\n")
    html: list[str] = []
    paragraph: list[str] = []
    list_tag: str | None = None
    list_items: list[str] = []
    quote: list[str] = []
    table: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            html.append(f"<p>{inline(' '.join(s.strip() for s in paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_tag and list_items:
            html.append(f"<{list_tag}>" + "".join(f"<li>{inline(item)}</li>" for item in list_items) + f"</{list_tag}>")
        list_items.clear()
        list_tag = None

    def flush_quote() -> None:
        if quote:
            html.append("<blockquote>" + markdown_to_html("\n".join(quote)) + "</blockquote>")
            quote.clear()

    def flush_table() -> None:
        if not table:
            return
        rows = [_table_cells(line) for line in table]
        has_header = len(table) > 1 and _TABLE_SEP.match(table[1]) is not None
        body_rows = rows[2:] if has_header else rows
        out = ["<table>"]
        if has_header:
            out.append("<thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in rows[0]) + "</tr></thead>")
        if body_rows:
            out.append("<tbody>" + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>" for row in body_rows) + "</tbody>")
        out.append("</table>")
        html.append("".join(out))
        table.clear()

    def flush_all() -> None:
        flush_paragraph()
        flush_list()
        flush_quote()
        flush_table()

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_all()
            continue
        if line.lstrip().startswith("|"):
            flush_paragraph(); flush_list(); flush_quote()
            table.append(line)
            continue
        flush_table()
        if line.startswith(">"):
            flush_paragraph(); flush_list()
            quote.append(line[1:].lstrip() if line.startswith("> ") or line == ">" else line[1:])
            continue
        flush_quote()
        heading = _HEADING.match(line)
        if heading:
            flush_paragraph(); flush_list()
            level = len(heading.group(1))
            html.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            continue
        if _RULE.match(line):
            flush_paragraph(); flush_list()
            html.append("<hr>")
            continue
        ul, ol = _UL.match(line), _OL.match(line)
        if ul or ol:
            flush_paragraph()
            tag = "ul" if ul else "ol"
            if list_tag != tag:
                flush_list()
                list_tag = tag
            list_items.append((ul or ol).group(1))
            continue
        if list_tag and raw.startswith((" ", "\t")):
            list_items[-1] += " " + line.strip()          # an indented continuation of the last item
            continue
        flush_list()
        paragraph.append(line)
    flush_all()
    return "\n".join(html)


def build_prospectus(source: Path, *, version_note: str = "") -> str:
    """The prospectus body as HTML with a comment naming where it came from, for the file shipped in the
    repository. The comment is the only place the source path appears; nothing in it reaches the page."""
    text = source.read_text(encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    note = f" {version_note}" if version_note else ""
    header = (f"<!-- Generated {stamp} by app.services.packs.markdown_to_html from {source.name}{note}. Do not edit by hand: "
              "re-run `python -m app.services.packs <path to PARTNER-PROSPECTUS.md>` and replace this file. -->\n")
    return header + markdown_to_html(text) + "\n"


# ---- the territory report -----------------------------------------------------------------------------------
def report_context(db, opportunity, *, today: date | None = None) -> dict:
    """Everything packs/territory.html needs for one opportunity. Rows come back in the order the
    opportunity lists its districts; a district that has disappeared from the model is reported as missing
    rather than silently dropped. Money appears only when assumptions_confirmed(db)."""
    from ..models import PostcodeDistrict
    from ..services import territory_rules as tr

    codes = [str(c).upper() for c in (opportunity.districts or [])]
    rows = db.query(PostcodeDistrict).filter(PostcodeDistrict.district.in_(codes)).all() if codes else []
    by_code = {row.district: row for row in rows}
    confirmed = assumptions_confirmed(db)
    assumptions = tr.load_assumptions(db)

    districts: list[dict] = []
    for code in codes:
        row = by_code.get(code)
        if row is None:
            districts.append({"district": code, "missing": True})
            continue
        item: dict[str, Any] = {
            "district": row.district, "missing": False, "area": row.area, "nation": row.nation,
            "la_name": row.la_name, "la_gss": row.la_gss, "dwellings": row.dwellings,
            "prs_households": row.prs_households, "prs_share": row.prs_share,
            "rent_1bed": row.rent_1bed, "rent_2bed": row.rent_2bed, "rent_3bed": row.rent_3bed, "rent_date": row.rent_date,
            "licensing": row.licensing, "licensing_note": row.licensing_note, "band": row.band, "state": row.state,
            "model_run": row.model_run,
        }
        if confirmed:
            item.update({
                "achievable_fee": row.achievable_fee, "units_y1": row.units_y1, "units_y2": row.units_y2, "units_y3": row.units_y3,
                "partner_net_y1": row.partner_net_y1, "monthly_fee": tr.monthly_fee(row.band, assumptions),
            })
        districts.append(item)

    runs = {d["model_run"].id: d["model_run"] for d in districts if not d["missing"] and d.get("model_run") is not None}
    waived = (opportunity.quoted_upfront_pence == 0 and opportunity.quoted_monthly_pence == 0
              and opportunity.quoted_upfront_pence is not None)
    return {
        "opportunity": opportunity,
        "person": opportunity.person,
        "districts": districts,
        "codes": codes,
        "confirmed": confirmed,
        "unconfirmed": 0 if confirmed else unconfirmed_count(db),
        "watermark": "" if confirmed else WATERMARK,
        "withheld": WITHHELD,
        "quote": {
            "upfront": pounds(opportunity.quoted_upfront_pence) if confirmed else "",
            "monthly": pounds(opportunity.quoted_monthly_pence) if confirmed else "",
            "waived": waived,
            "band": opportunity.band,
        },
        "prs_total": sum(int(d["prs_households"]) for d in districts if not d["missing"] and d["prs_households"] is not None),
        "dwellings_total": sum(int(d["dwellings"]) for d in districts if not d["missing"] and d["dwellings"] is not None),
        "model_runs": list(runs.values()),
        "today": today or datetime.now(timezone.utc).date(),
        "fee_cap": assumptions["fee_cap"], "management_pct": assumptions["management_pct"],
        "pounds": pounds, "money": money, "number": number,
    }


if __name__ == "__main__":     # pragma: no cover
    if len(sys.argv) < 2:
        print("usage: python -m app.services.packs <path to PARTNER-PROSPECTUS.md> [version note]", file=sys.stderr)
        sys.exit(2)
    sys.stdout.write(build_prospectus(Path(sys.argv[1]), version_note=" ".join(sys.argv[2:])))
