"""Parse the supplier/ingredient inventory the coordinator maintains.

Preferred format, one row per lot (rows with the same name + kind are merged into one item):

    name,kind,lot,sku,upc,qty_received,qty_on_hand,aliases,contact
    Sunrise Foods Inc,supplier,LOT 7710B,SF-TAH-16,012345678905,120,40,Sunrise Foods,purchasing@example.com
    Sunrise Foods Inc,supplier,LOT 7712C,SF-TAH-16,012345678905,200,200,,
    tahini,ingredient,T-2209-14,,,,,sesame paste,warehouse@example.com

Legacy format still works (all lots on one row, no SKU/UPC/quantities):

    name,kind,lots,aliases,contact
    Sunrise Foods Inc,supplier,LOT 7710B;LOT 7712C,Sunrise Foods,purchasing@example.com

Rules:
  - Header names are case-insensitive and spaces are fine ("Qty On Hand" works).
  - Use either a "lot" column or a "lots" column, not both.
  - aliases are semicolon-separated. contact is where the hold notice goes.
  - Quantities are plain numbers in whatever unit the coordinator uses; 1,200 is fine.
  - UPC/GTIN must be 8, 12, 13 or 14 digits (spaces and hyphens allowed). Excel often drops a
    leading zero from a UPC, so an 11-digit value is rejected rather than guessed at.
  - A bad file fails loudly before any check runs: a partial record that silently skips rows is
    worse than no record. Unrecognized extra columns are ignored but reported in the run notes.
"""
from __future__ import annotations

import csv
import io
import math
import re

from .matcher import Item, LotInfo, lot_norm

KINDS = ("supplier", "ingredient")
KNOWN = {"name", "kind", "lots", "lot", "sku", "upc", "qty_received", "qty_on_hand", "aliases", "contact"}
UPC_LENGTHS = (8, 12, 13, 14)


def _header(h: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def _split(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(";") if p.strip()]


def _qty(value: str | None, where: str, problems: list[str]):
    v = (value or "").strip().replace(",", "")
    if not v:
        return None
    try:
        n = float(v)
    except ValueError:
        problems.append(f"{where} is {value!r}, which is not a number")
        return None
    if not math.isfinite(n) or n < 0:
        problems.append(f"{where} must be a non-negative number, got {value!r}")
        return None
    return int(n) if n == int(n) else n


def _upc(value: str | None, where: str, problems: list[str]) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    digits = re.sub(r"[\s\-]", "", v)
    if not digits.isdigit() or len(digits) not in UPC_LENGTHS:
        hint = " Excel may have dropped a leading zero." if digits.isdigit() and len(digits) == 11 else ""
        problems.append(f"{where} {value!r} is not a valid UPC/GTIN (8, 12, 13 or 14 digits).{hint}")
        return ""
    return digits


def parse_inventory_with_notes(text: str) -> tuple[list[Item], list[str]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("inventory CSV is empty")
    reader.fieldnames = [_header(h) for h in reader.fieldnames]
    fields = set(reader.fieldnames)
    missing = {"name", "kind"} - fields
    if missing:
        raise ValueError(f"inventory CSV is missing required column(s): {sorted(missing)}")
    if "lots" in fields and "lot" in fields:
        raise ValueError("inventory CSV has both 'lot' and 'lots' columns; use one or the other")

    notes = []
    unknown = sorted(f for f in fields if f and f not in KNOWN)
    if unknown:
        notes.append(f"Inventory CSV has unrecognized column(s), ignored: {unknown}. "
                     "Check the spelling if you expected them to be used.")

    items: dict[tuple[str, str], Item] = {}
    seen: dict[tuple[str, str], set] = {}
    problems: list[str] = []
    for n, row in enumerate(reader, start=2):  # row 1 is the header
        name = (row.get("name") or "").strip()
        kind = (row.get("kind") or "").strip().lower()
        if not name:
            problems.append(f"row {n}: empty name")
            continue
        if kind not in KINDS:
            problems.append(f"row {n} ({name}): kind must be one of {KINDS}, got {kind!r}")
            continue
        where = f"row {n} ({name})"
        sku = (row.get("sku") or "").strip()
        upc = _upc(row.get("upc"), f"{where}: upc", problems)
        received = _qty(row.get("qty_received"), f"{where}: qty_received", problems)
        on_hand = _qty(row.get("qty_on_hand"), f"{where}: qty_on_hand", problems)
        has_detail = bool(sku or upc or received is not None or on_hand is not None)

        codes = _split(row.get("lots")) if "lots" in fields else [(row.get("lot") or "").strip()]
        codes = [c for c in codes if c]
        if len(codes) > 1 and has_detail:
            problems.append(f"{where}: put one lot per row when adding SKU, UPC or quantities")
            continue
        if not codes and not has_detail:
            infos = []  # a supplier with no lots on hand: nothing to record
        elif not codes:
            infos = [LotInfo("", sku, upc, received, on_hand)]
        else:
            infos = [LotInfo(c, sku, upc, received, on_hand) for c in codes]

        key = (name.lower(), kind)
        item = items.setdefault(key, Item(name, kind))
        seen.setdefault(key, set())
        for info in infos:
            sig = (lot_norm(info.code), info.sku, info.upc)
            if sig in seen[key]:
                problems.append(f"{where}: duplicate row for lot {info.code or '(none)'} "
                                "with the same SKU and UPC")
                continue
            seen[key].add(sig)
            item.lot_info.append(info)
            if info.code and info.code not in item.lots:
                item.lots.append(info.code)
        for a in _split(row.get("aliases")):
            if a not in item.aliases:
                item.aliases.append(a)
        contact = (row.get("contact") or "").strip()
        if contact and item.contact and contact.lower() != item.contact.lower():
            problems.append(f"{where}: contact {contact!r} conflicts with {item.contact!r} on an earlier row")
        elif contact and not item.contact:
            item.contact = contact

    if problems:
        raise ValueError("inventory CSV has problems:\n" + "\n".join(problems))
    if not items:
        raise ValueError("inventory CSV has no items")
    return list(items.values()), notes


def parse_inventory(text: str) -> list[Item]:
    return parse_inventory_with_notes(text)[0]
