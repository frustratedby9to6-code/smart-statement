#!/usr/bin/env python3
"""Parse HDFC-style statement PDF text into transactions.json for the static page."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

try:
    from pypdf import PdfReader
except ImportError:
    print("Install pypdf: pip install pypdf", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
PDF_PATH = ROOT / "assets" / "transactions.pdf"
OUT_PATH = ROOT / "assets" / "transactions.json"

# Date at line start; narration may continue on following lines or same line as amounts
START_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})(?:\s+(.*))?$")
TAIL_RE = re.compile(
    r"\s+(\d{2}/\d{2}/\d{4})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$"
)
REF_DIGITS = re.compile(r"^\d{10,15}$")
AMT_TOKEN = re.compile(r"^[\d,]+\.\d{2}$")
NARRATION_FOLLOWS_DATE = re.compile(
    r"^(UPI-|NEFT|RTGS|IMPS|ACH|ATM|nfs-|SELF|Cash|BY TRANSFER|BY CASH|CHQ|DD )",
    re.I,
)
# PDF extract often inserts a space before a hyphenated vendor token (YBL -YESB → YBL-YESB)
FIX_HYPHEN_SPACE = re.compile(r"([A-Za-z0-9]) -([A-Z0-9])")


def normalize_bank_text(s: str) -> str:
    s = FIX_HYPHEN_SPACE.sub(r"\1-\2", s)
    s = re.sub(r"(\d)@\s+([A-Z])", r"\1@\2", s)
    s = re.sub(r"([A-Za-z0-9])@\s+([A-Z])", r"\1@\2", s)
    s = re.sub(r"(\d)\s+(@[A-Z])", r"\1\2", s)
    # PDF line break: …YBL-YES B0YBLUPI → …YBL-YESB0YBLUPI
    s = re.sub(r"-YES B0YBL", r"-YESB0YBL", s, flags=re.I)
    return s.strip()


def smart_join_detail_parts(parts: list[str]) -> str:
    """Join PDF narration fragments without bogus spaces (YBL + -YESB0, GUNTI + KOVELA, …22 + 1-UPI)."""
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return ""
    out = parts[0]
    for nxt in parts[1:]:
        # Shattered NEFT: duplicate "503678517672" line after narration already ends with that ref
        if REF_DIGITS.fullmatch(nxt) and re.search(
            rf"{re.escape(nxt)}\s*$", out.rstrip()
        ):
            continue
        # e.g. …54004778922 + 1-UPI → …540047789221-UPI
        m = re.match(r"^(\d+)(-UPI)$", nxt)
        if m and out and re.search(r"\d$", out):
            out = out + m.group(1) + m.group(2)
            continue
        if nxt.startswith("-"):
            out = out.rstrip() + nxt
        elif out.endswith("@") and nxt:
            out = out + nxt
        # …-503 + 667057526-PAYMENT (CRED / split numeric tail; nxt is full next line)
        elif re.search(r"-\d{1,4}$", out) and re.match(r"^\d{6,}", nxt):
            out = out + nxt
        # …YBL-YES \n B0YBLUPI- (VANKUDAVATH-style line break)
        elif re.search(r"-YES\s*$", out.rstrip()) and re.match(r"^B0YBL", nxt, re.I):
            out = out.rstrip() + nxt
        # GUNTI \n KOVELA-
        elif out.endswith("GUNTI") and nxt.startswith("KOVELA"):
            out = out + nxt
        # YESB0YBLUP \n I-503… → YESB0YBLUPI-503…
        elif out.endswith("BLUP") and re.match(r"^I-\d", nxt):
            out = out + "I" + nxt[1:]
        else:
            out = out + " " + nxt
    return out


def merge_details_line2(details: str, head_before_upi: str) -> str:
    """Append PDF line-2 narration tail so column 2 matches the statement (…-REF-UPI)."""
    head = head_before_upi.strip()
    if not head:
        return details.strip()
    d = details.rstrip()
    if head.startswith("-"):
        return d + head + "-UPI"
    # YESB0YBLUP + (line split) I-503…-UPI fragment
    if d.endswith("BLUP") and re.match(r"^I-\d", head):
        return d + "I" + head[1:] + "-UPI"
    return f"{d} {head}-UPI".strip()


def strip_leading_date(block: str, date: str) -> str:
    block = block.strip()
    if block.startswith(date):
        return block[len(date) :].strip()
    return block


def narration_and_ref_from_prefix(details: str, prefix: str) -> tuple[str, str]:
    """
    PDF column 3 is usually a 10–15 digit ref; column 2 ends with …-REF-UPI.
    Amount line may be only 'REF date w d bal' when narration was fully above.
    """
    details = details.strip()
    prefix = prefix.strip()
    if not prefix:
        m = re.search(r"(\d{10,15})-UPI\s*$", details)
        if m:
            return normalize_bank_text(details), m.group(1)
        # NEFT / some credits: narration ends with …-REF (no trailing -UPI)
        m2 = re.search(r"-(\d{10,15})\s*$", details)
        if m2:
            return normalize_bank_text(details), m2.group(1)
        return normalize_bank_text(details), "—"

    if "-UPI" in prefix:
        head, tail = prefix.rsplit("-UPI", 1)
        tail = tail.strip()
        if REF_DIGITS.fullmatch(tail):
            merged = normalize_bank_text(merge_details_line2(details, head))
            return merged, tail

    if REF_DIGITS.fullmatch(prefix):
        ref = prefix
        if details.endswith(ref + "-UPI"):
            return normalize_bank_text(details), ref
        return normalize_bank_text(details), ref

    nums = re.findall(r"\d{10,15}", prefix)
    if nums:
        ref = nums[-1]
        extra = prefix
        extra = re.sub(rf"{re.escape(ref)}\s*$", "", extra).strip()
        extra = re.sub(r"-UPI\s*$", "", extra).strip()
        merged = normalize_bank_text((details + (" " + extra if extra else "")).strip())
        return merged, ref

    return normalize_bank_text(f"{details} {prefix}".strip()), "—"


def narration_from_single_block(narration_block: str) -> tuple[str, str]:
    """Same line: full 'narration…-UPI REF' before value date (no prior detail_parts)."""
    return narration_and_ref_from_prefix("", narration_block.strip())


def try_parse_shattered_amount_block(
    lines: list[str], start_i: int
) -> Optional[Tuple[str, str, str, str, int, Optional[str]]]:
    """
    pypdf sometimes splits one row into: [optional ref] date w d bal on separate lines.
    Returns (valueDate, withdrawal, deposit, balance, next_index, standalone_ref_or_none).
    """
    i = start_i
    n = len(lines)
    standalone_ref: str | None = None
    if i < n and REF_DIGITS.fullmatch(lines[i]):
        if i + 1 < n and re.fullmatch(r"\d{2}/\d{2}/\d{4}", lines[i + 1]):
            standalone_ref = lines[i]
            i += 1
    if i >= n or not re.fullmatch(r"\d{2}/\d{2}/\d{4}", lines[i]):
        return None
    vd = lines[i]
    i += 1
    if i >= n or not AMT_TOKEN.fullmatch(lines[i]):
        return None
    w = lines[i]
    i += 1
    if i >= n or not AMT_TOKEN.fullmatch(lines[i]):
        return None
    d = lines[i]
    i += 1
    if i >= n or not AMT_TOKEN.fullmatch(lines[i]):
        return None
    bal = lines[i]
    return (vd, w, d, bal, i + 1, standalone_ref)


def extract_rows(text: str) -> list[dict]:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    header_idx = None
    for idx, ln in enumerate(lines):
        if "Withdrawal Amount" in ln and "Date" in ln and "Narration" in ln:
            header_idx = idx
            break
    if header_idx is None:
        return []

    rows: list[dict] = []
    i = header_idx + 1
    while i < len(lines):
        ln = lines[i]
        if (
            ln.startswith("Page ")
            or "STATEMENT SUMMARY" in ln
            or "END OF STATEMENT" in ln
            or "Generation Date" in ln
            or ln.startswith("Opening Balance")
        ):
            i += 1
            continue

        m0 = START_RE.match(ln)
        if not m0:
            i += 1
            continue

        date = m0.group(1)
        rest = (m0.group(2) or "").strip()
        if not rest:
            if i + 1 >= len(lines) or not NARRATION_FOLLOWS_DATE.match(lines[i + 1]):
                i += 1
                continue

        ta_same = TAIL_RE.search(ln)
        if ta_same:
            before = ln[: ta_same.start()].strip()
            narration_block = strip_leading_date(before, date)
            full_details, ref = narration_from_single_block(narration_block)
            rows.append(
                {
                    "date": date,
                    "details": full_details,
                    "ref": ref,
                    "valueDate": ta_same.group(1),
                    "withdrawal": ta_same.group(2),
                    "deposit": ta_same.group(3),
                    "balance": ta_same.group(4),
                }
            )
            i += 1
            continue

        detail_parts: list[str] = [rest] if rest else []
        i += 1

        while i < len(lines):
            ln2 = lines[i]
            shattered = try_parse_shattered_amount_block(lines, i)
            if shattered:
                vd, w, d, bal, next_i, stand_ref = shattered
                detail_line = smart_join_detail_parts(detail_parts)
                last_part = detail_parts[-1].strip() if detail_parts else ""
                plausible_tail = bool(
                    last_part.endswith("-UPI")
                    or re.fullmatch(r"\d+-UPI", last_part)
                    or re.search(r"-\d{10,15}$", last_part)
                    or (
                        REF_DIGITS.fullmatch(last_part)
                        and detail_parts
                        and len(detail_parts) >= 2
                        and re.search(r"-\d{10,15}$", detail_parts[-2].strip())
                    )
                )
                if detail_line and plausible_tail:
                    full_details, ref = narration_and_ref_from_prefix(detail_line, "")
                    if stand_ref:
                        ref = stand_ref
                    rows.append(
                        {
                            "date": date,
                            "details": full_details,
                            "ref": ref,
                            "valueDate": vd,
                            "withdrawal": w,
                            "deposit": d,
                            "balance": bal,
                        }
                    )
                    i = next_i
                    break
            ta = TAIL_RE.search(ln2)
            if ta:
                prefix_raw = ln2[: ta.start()].strip()
                detail_line = smart_join_detail_parts(detail_parts)
                # CRED-style: line1 ends …-503, line2 prefix starts 667057526-PAYMENT…
                m_lead = re.match(r"^(\d{6,})(?=-)", prefix_raw)
                if m_lead and re.search(r"-\d{1,4}$", detail_line.rstrip()):
                    detail_line = detail_line + m_lead.group(1)
                    prefix_use = prefix_raw[m_lead.end() :].lstrip()
                else:
                    prefix_use = prefix_raw
                full_details, ref = narration_and_ref_from_prefix(detail_line, prefix_use)
                ref_tail_m = re.search(r"(\d{10,15})\s*$", prefix_use)
                if ref_tail_m:
                    ref = ref_tail_m.group(1)
                rows.append(
                    {
                        "date": date,
                        "details": full_details,
                        "ref": ref,
                        "valueDate": ta.group(1),
                        "withdrawal": ta.group(2),
                        "deposit": ta.group(3),
                        "balance": ta.group(4),
                    }
                )
                i += 1
                break
            if START_RE.match(ln2):
                break
            detail_parts.append(ln2)
            i += 1

    return rows


def main() -> None:
    if not PDF_PATH.is_file():
        print(f"Missing {PDF_PATH}", file=sys.stderr)
        sys.exit(1)
    reader = PdfReader(str(PDF_PATH))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    rows = extract_rows(text)
    OUT_PATH.write_text(
        json.dumps({"source": "transactions.pdf", "count": len(rows), "rows": rows}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(rows)} rows to {OUT_PATH}")


if __name__ == "__main__":
    main()
