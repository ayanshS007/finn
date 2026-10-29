"""Tiny synthetic integrated-filing XBRL and NSE rows for edge-case tests."""

from __future__ import annotations

NS = (
    'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
    'xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt" '
    'xmlns:iso4217="http://www.xbrl.org/2003/iso4217"'
)


def make_xbrl(
    basis: str,
    contexts: dict[str, tuple[str, str]],
    facts: list[tuple[str, str, float]],
    nature: str | None = None,
    unit: str = "iso4217:INR",
) -> bytes:
    """contexts: id -> (start, end); facts: (concept, context id, value in rupees)."""
    ctx = "".join(
        f'<xbrli:context id="{i}"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier>'
        f"</xbrli:entity><xbrli:period><xbrli:startDate>{a}</xbrli:startDate>"
        f"<xbrli:endDate>{b}</xbrli:endDate></xbrli:period></xbrli:context>"
        for i, (a, b) in contexts.items()
    )
    first = next(iter(contexts))
    nature = nature or basis.capitalize()
    body = "".join(
        f'<in-capmkt:{c} contextRef="{x}" decimals="-5" unitRef="U">{v:.0f}</in-capmkt:{c}>'
        for c, x, v in facts
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><xbrli:xbrl {NS}>{ctx}'
        f'<xbrli:unit id="U"><xbrli:measure>{unit}</xbrli:measure></xbrli:unit>'
        f'<in-capmkt:NatureOfReportStandaloneConsolidated contextRef="{first}">{nature}'
        f"</in-capmkt:NatureOfReportStandaloneConsolidated>{body}</xbrli:xbrl>"
    ).encode()


def raw_row(period: str, basis: str, url: str, broadcast: str, sub: str = "Original") -> dict:
    """A row of /api/integrated-filing-results; period like '31-MAR-2026'."""
    return {
        "type": "Integrated Filing- Financials",
        "type_Sub": sub,
        "qe_Date": period,
        "consolidated": basis.capitalize(),
        "audited": "Audited",
        "broadcast_Date": broadcast,
        "xbrl": url,
        "ixbrl": None,
        "pdf_attach": "https://nsearchives.nseindia.com/corporate/null",
        "symbol": "TEST",
        "seq_Id": url[-6:],
    }
