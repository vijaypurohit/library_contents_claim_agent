"""Render a claim packet as one self-contained HTML page (report.html in the sweep folder).

All links are relative to the sweep folder (frames/, evidence/, debug/), so the folder can be
zipped or copied as a whole and the report still opens every frame and price page it cites.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from claimagent.domain.packet import ClaimPacket

TEMPLATES = Path(__file__).parent / "templates"


def money(value: float | None, currency: str) -> str:
    """"₹1,234" style amounts; an empty value is shown as a dash, never as zero."""
    if value is None:
        return "–"
    symbol = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}.get(currency, f"{currency} ")
    return f"{symbol}{value:,.0f}" if value >= 100 else f"{symbol}{value:,.2f}"


def render_report(packet: ClaimPacket, shelves: list[dict], crops: dict[str, str], warnings: list[str]) -> str:
    """HTML for a packet. `shelves` lists annotated shelf images; `crops` maps book id to its spine image."""
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    env.filters["money"] = lambda v: money(v, packet.sweep.currency)
    return env.get_template("report.html.j2").render(p=packet, shelves=shelves, crops=crops, warnings=warnings)
