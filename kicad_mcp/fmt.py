"""Compact markdown formatting helpers."""
from __future__ import annotations

from typing import Any, Iterable, Sequence


def num(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return {True: "yes", False: "", None: ""}[v]
    if isinstance(v, float):
        s = f"{v:.3f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"
    return str(v)


def table(rows: Iterable[dict], cols: Sequence[str], headers: Sequence[str] | None = None) -> str:
    rows = list(rows)
    if not rows:
        return "(none)"
    headers = headers or cols
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(num(r.get(c, "")).replace("|", "\\|").replace("\n", " ") for c in cols) + " |")
    return "\n".join(out)


def kv(pairs: dict | Sequence[tuple[str, Any]]) -> str:
    items = pairs.items() if isinstance(pairs, dict) else pairs
    return "\n".join(f"- {k}: {num(v)}" for k, v in items if v not in (None, "", [], {}))


def clip(rows: list, limit: int, total: int | None = None) -> str:
    total = len(rows) if total is None else total
    if total > limit:
        return f"\n(showing {limit} of {total}; raise `limit` or narrow the filter)"
    return ""
