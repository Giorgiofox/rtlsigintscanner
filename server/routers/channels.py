"""Channel memory CRUD and category/bank rename."""
from typing import Any

from fastapi import APIRouter, HTTPException

from db import connect, normalize_mode, rows_to_dict
from models import BulkChannels, CategoryRename, ChannelCreate, ChannelUpdate

router = APIRouter()

EDITABLE = {"enabled", "frequency_mhz", "frequency_hz", "name", "system", "group_name",
            "mode", "priority", "tone_hz", "attenuation", "scan_exclude", "category",
            "transcription_language"}


@router.get("/api/categories")
def categories() -> list[dict[str, Any]]:
    con = connect()
    rows = rows_to_dict(con.execute("""
        select system category, count(*) channels,
               sum(case when enabled=1 then 1 else 0 end) enabled_channels
        from channels group by system order by system"""))
    con.close()
    return rows


@router.get("/api/channels")
def channels(category: str | None = None, enabled: int = 1, q: str | None = None,
             limit: int = 500) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 2000))
    where = ["(? is null or system=?)", "(? < 0 or enabled=?)"]
    params: list[Any] = [category, category, enabled, enabled]
    if q:
        where.append("(name like ? or frequency_mhz like ? or system like ? or group_name like ?)")
        params += [f"%{q}%"] * 4
    params.append(limit)
    con = connect()
    rows = rows_to_dict(con.execute(
        f"select * from channels where {' and '.join(where)} "
        f"order by system, group_name, frequency_hz limit ?", params))
    con.close()
    return rows


@router.post("/api/channels/bulk")
def bulk_channels(payload: BulkChannels) -> dict[str, Any]:
    """Apply enabled / scan_exclude to all channels matching the same (category, q)
    filter the editor is showing. Powers the select/deselect-all buttons."""
    sets, set_params = [], []
    if payload.set_enabled is not None:
        sets.append("enabled=?"); set_params.append(1 if payload.set_enabled else 0)
    if payload.set_scan_exclude is not None:
        sets.append("scan_exclude=?"); set_params.append(1 if payload.set_scan_exclude else 0)
    if not sets:
        raise HTTPException(400, "nothing to set")
    where, where_params = [], []
    if payload.category:
        where.append("system=?"); where_params.append(payload.category)
    if payload.q:
        where.append("(name like ? or frequency_mhz like ? or system like ? or group_name like ?)")
        where_params += [f"%{payload.q}%"] * 4
    sql = f"update channels set {', '.join(sets)}"
    if where:
        sql += " where " + " and ".join(where)
    con = connect()
    cur = con.execute(sql, set_params + where_params)
    con.close()
    return {"ok": True, "updated": cur.rowcount}


@router.post("/api/channels")
def create_channel(payload: ChannelCreate) -> dict[str, Any]:
    try:
        freq_hz = int(round(float(payload.frequency_mhz.replace(",", ".")) * 1_000_000))
    except ValueError:
        raise HTTPException(400, "invalid frequency_mhz")
    category = f"{payload.system} / {payload.group_name}" if payload.group_name else payload.system
    con = connect()
    cur = con.execute(
        """insert into channels(enabled, frequency_hz, frequency_mhz, name, system, group_name,
           category, mode, priority, raw_json) values(?,?,?,?,?,?,?,?,?, '{}')""",
        (1 if payload.enabled else 0, freq_hz, f"{freq_hz/1e6:.4f}", payload.name, payload.system,
         payload.group_name, category, normalize_mode(payload.mode, payload.system), payload.priority))
    row = con.execute("select * from channels where id=?", (cur.lastrowid,)).fetchone()
    con.close()
    return dict(row)


@router.put("/api/channels/{channel_id}")
def update_channel(channel_id: int, update: ChannelUpdate) -> dict[str, Any]:
    patch = update.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(400, "empty update")
    if patch.get("frequency_mhz") is not None:
        try:
            freq_hz = int(round(float(str(patch["frequency_mhz"]).replace(",", ".")) * 1_000_000))
        except ValueError:
            raise HTTPException(400, "invalid frequency_mhz")
        patch["frequency_mhz"] = f"{freq_hz/1e6:.4f}"
        patch["frequency_hz"] = freq_hz
    if "enabled" in patch:
        patch["enabled"] = 1 if patch["enabled"] else 0
    if "scan_exclude" in patch:
        patch["scan_exclude"] = 1 if patch["scan_exclude"] else 0
    if patch.get("mode") is not None:
        patch["mode"] = normalize_mode(patch["mode"], patch.get("system", ""))
    if "system" in patch or "group_name" in patch:
        con = connect()
        row = con.execute("select system, group_name from channels where id=?", (channel_id,)).fetchone()
        con.close()
        if not row:
            raise HTTPException(404, "channel not found")
        system = patch.get("system") if patch.get("system") is not None else row["system"]
        group = patch.get("group_name") if patch.get("group_name") is not None else row["group_name"]
        patch["category"] = f"{system} / {group}" if group else system
    assignments, params = [], []
    for key, value in patch.items():
        if key in EDITABLE:
            assignments.append(f"{key}=?")
            params.append(value)
    if not assignments:
        raise HTTPException(400, "no valid fields")
    params.append(channel_id)
    con = connect()
    cur = con.execute(f"update channels set {', '.join(assignments)} where id=?", params)
    row = con.execute("select * from channels where id=?", (channel_id,)).fetchone()
    con.close()
    if cur.rowcount == 0 or not row:
        raise HTTPException(404, "channel not found")
    return dict(row)


@router.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: int) -> dict[str, Any]:
    con = connect()
    cur = con.execute("delete from channels where id=?", (channel_id,))
    con.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "channel not found")
    return {"ok": True}


@router.post("/api/categories/rename")
def rename_category(payload: CategoryRename) -> dict[str, Any]:
    old_system = payload.old_system.strip()
    new_system = payload.new_system.strip()
    if not old_system or not new_system:
        raise HTTPException(400, "system names required")
    con = connect()
    if payload.old_group_name is not None:
        old_group = payload.old_group_name.strip()
        new_group = (payload.new_group_name or old_group).strip()
        category = f"{new_system} / {new_group}" if new_group else new_system
        cur = con.execute(
            "update channels set system=?, group_name=?, category=? where system=? and group_name=?",
            (new_system, new_group, category, old_system, old_group))
        updated = cur.rowcount
    else:
        rows = rows_to_dict(con.execute(
            "select id, group_name from channels where system=?", (old_system,)))
        for row in rows:
            group = row.get("group_name") or ""
            category = f"{new_system} / {group}" if group else new_system
            con.execute("update channels set system=?, category=? where id=?",
                        (new_system, category, row["id"]))
        updated = len(rows)
    con.close()
    return {"ok": True, "updated": updated}
