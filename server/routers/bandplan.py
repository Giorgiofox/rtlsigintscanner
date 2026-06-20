"""Band plan: the curated Italian utility bands, enabled by group with one click.

A band is a frequency RANGE the agent search-scans (rtl_power over lo:hi, record any
active bin) — so enabling a band covers it whole without thousands of discrete memories.
"""
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import connect, rows_to_dict

router = APIRouter()


class BandToggle(BaseModel):
    enabled: bool


class GroupToggle(BaseModel):
    group_name: str
    enabled: bool


@router.get("/api/bandplan")
def bandplan() -> dict[str, Any]:
    con = connect()
    rows = rows_to_dict(con.execute(
        "select id, group_name, name, lo_hz, hi_hz, step_hz, mode, enabled, note "
        "from bandplan order by lo_hz"))
    con.close()
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = groups.setdefault(r["group_name"], {"group": r["group_name"], "bands": [],
                                                "enabled": 0, "total": 0})
        g["bands"].append(r)
        g["total"] += 1
        g["enabled"] += 1 if r["enabled"] else 0
    return {"groups": list(groups.values())}


@router.post("/api/bandplan/{band_id}")
def toggle_band(band_id: int, payload: BandToggle) -> dict[str, Any]:
    con = connect()
    cur = con.execute("update bandplan set enabled=? where id=?",
                      (1 if payload.enabled else 0, band_id))
    con.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "band not found")
    return {"ok": True}


@router.post("/api/bandplan/group/toggle")
def toggle_group(payload: GroupToggle) -> dict[str, Any]:
    con = connect()
    cur = con.execute("update bandplan set enabled=? where group_name=?",
                      (1 if payload.enabled else 0, payload.group_name))
    con.close()
    return {"ok": True, "updated": cur.rowcount}
