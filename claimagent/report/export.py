"""Export a sweep's claim for sharing: the packet, report and metrics plus every file they reference.

The folder layout of the sweep is kept (frames/, evidence/, debug/), so the report's relative links
still work in the copy. Only referenced files are copied, not every recorded frame.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from claimagent.core.sweep import SweepDir
from claimagent.domain.packet import ClaimPacket

_LOCAL_LINK = re.compile(r'(?:src|href)="((?:frames|evidence|debug)/[^"#?]+)"')


def referenced_files(packet: ClaimPacket, report_html: str) -> set[str]:
    """Paths (relative to the sweep) the packet and the report point at."""
    refs = {f"frames/{b.frame_ref}" for b in packet.books} | {f"frames/{i.frame_ref}" for i in packet.items}
    refs |= {b.annotated_ref for b in packet.books if b.annotated_ref}
    for book in packet.books:
        for price in (book.replacement_cost, book.used_value):
            refs |= {price.snapshot} if price.snapshot else set()
            refs |= {q.snapshot for q in price.quotes}
    for item in packet.items:
        refs |= {q.snapshot for q in item.replacement_cost.quotes}
    refs |= set(_LOCAL_LINK.findall(report_html))
    return refs


def export_sweep(sweep: SweepDir, out: Path) -> tuple[int, list[str]]:
    """Copy the claim into `out`. Returns (files copied, referenced files that were missing)."""
    if not sweep.packet_path.exists():
        raise FileNotFoundError(f"sweep {sweep.id} has no claim packet yet")
    packet = ClaimPacket.model_validate_json(sweep.packet_path.read_text())
    report = sweep.report_path.read_text() if sweep.report_path.exists() else ""
    out.mkdir(parents=True, exist_ok=True)
    copied, missing = 0, []
    for name in ("claim_packet.json", "report.html", "metrics.json", "facts.json"):
        if (sweep.root / name).exists():
            shutil.copy2(sweep.root / name, out / name)
            copied += 1
    for ref in sorted(referenced_files(packet, report)):
        source = sweep.root / ref
        if not source.exists():
            missing.append(ref)
            continue
        (out / ref).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, out / ref)
        copied += 1
    return copied, missing
