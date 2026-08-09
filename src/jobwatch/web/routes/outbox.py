"""Outbox — queued, sent, and failed notifications, with replay (§10).

This screen is the channel of last resort. Whatever Discord and email do, a
failed delivery lands here as a `failed` row you can see and re-send, which
costs nothing and has no account to lapse.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..deps import get_db, get_service, partial, render

router = APIRouter()

PAGE_SIZE = 100


@router.get("/outbox", response_class=HTMLResponse)
async def outbox(request: Request) -> HTMLResponse:
    service = get_service(request)
    db = service.db
    status = request.query_params.get("status", "")

    sql = "SELECT * FROM outbox"
    args: list = []
    if status in ("queued", "sent", "failed"):
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(PAGE_SIZE)

    rows = [_decorate(r) for r in db.query(sql, args)]

    return render(
        request,
        "outbox.html",
        {
            "rows": rows,
            "status": status,
            "counts": service.outbox.counts(),
            "channels": service.outbox.channel_status(),
            "any_channel_configured": bool(service.outbox.live_channels),
        },
    )


def _decorate(row) -> dict:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    data = dict(row)
    data["parsed"] = payload if isinstance(payload, dict) else {}
    # Which channels have already taken this row. A partially delivered row is
    # the one case where 'queued' does not mean 'nobody has seen it'.
    try:
        delivered = json.loads(data.get("delivered") or "[]")
    except (TypeError, ValueError):
        delivered = []
    data["delivered_to"] = delivered if isinstance(delivered, list) else []
    return data


@router.post("/outbox/{outbox_id}/replay", response_class=HTMLResponse)
async def replay(request: Request, outbox_id: int) -> HTMLResponse:
    service = get_service(request)
    service.outbox.replay(outbox_id)
    row = get_db(request).one("SELECT * FROM outbox WHERE id = ?", (outbox_id,))
    return partial(request, "_outbox_row.html", row=_decorate(row) if row else None)
