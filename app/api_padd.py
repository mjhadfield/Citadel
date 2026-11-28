"""PADD API routes: Dropzone, Notes & Checklists, Home Assistant Todo sync, and signed transfer links."""
from __future__ import annotations

import asyncio
import html
from typing import Literal
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import auth, config, notify, padd

router = APIRouter(prefix="/api/owner/padd", dependencies=[Depends(auth.require_owner)])
public = APIRouter(include_in_schema=False)


# ---- request schemas ----------------------------------------------------------------------------

class DropTextIn(BaseModel):
    text: str = Field(min_length=1, max_length=300_000)
    title: str = Field("", max_length=160)
    kind: Literal["auto", "text", "link", "code"] = "auto"
    pinned: bool = False
    fetch_preview: bool = True
    device_name: str | None = Field(None, max_length=60)


class DropPatchIn(BaseModel):
    pinned: bool | None = None
    title: str | None = Field(None, max_length=160)
    body: str | None = Field(None, max_length=300_000)
    kind: Literal["text", "link", "code"] | None = None


class DropClearIn(BaseModel):
    include_pinned: bool = False


class TicketIn(BaseModel):
    ttl: int = Field(120, ge=30, le=86400)
    qr: bool = False


class ZebraLabSendIn(BaseModel):
    target: Literal["document", "asset"] = "document"


class NoteCreateIn(BaseModel):
    title: str = Field("", max_length=120)
    body: str = Field("", max_length=300_000)
    kind: Literal["note", "checklist"] = "note"
    tags: list[str] = Field(default_factory=list, max_length=16)
    pinned: bool = False


class NotePatchIn(BaseModel):
    title: str | None = Field(None, max_length=120)
    body: str | None = Field(None, max_length=300_000)
    kind: Literal["note", "checklist"] | None = None
    tags: list[str] | None = Field(None, max_length=16)
    pinned: bool | None = None
    archived: bool | None = None


class ToggleCheckIn(BaseModel):
    line_index: int = Field(ge=0, le=50_000)


class ChecklistActionIn(BaseModel):
    action: Literal["reset", "clear_done"]


class TodoAddIn(BaseModel):
    summary: str = Field(min_length=1, max_length=200)


class TodoUpdateIn(BaseModel):
    item: str = Field(min_length=1, max_length=200)
    status: Literal["needs_action", "completed"] | None = None
    rename: str | None = Field(None, max_length=200)


class TodoRemoveIn(BaseModel):
    item: str = Field(min_length=1, max_length=200)


def _device_info(request: Request) -> tuple[str | None, str]:
    dev = auth.current_device(request) or {}
    return dev.get("id"), str(dev.get("name") or "Owner device")


def _origin(request: Request) -> str:
    host = request.headers.get("host")
    scheme = request.url.scheme or "http"
    return f"{scheme}://{host}" if host else config.public_url().rstrip("/")


def _file_headers(filename: str, *, inline: bool) -> dict[str, str]:
    safe_ascii = filename.encode("ascii", "ignore").decode("ascii").replace('"', "").replace("\r", "").replace("\n", "") or "file"
    disp = "inline" if inline else "attachment"
    return {
        "Content-Disposition": f"{disp}; filename=\"{safe_ascii}\"; filename*=UTF-8''{quote(filename)}",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600" if inline else "no-store",
    }


# ---- Dropzone routes ----------------------------------------------------------------------------

@router.get("/drops")
async def get_drops(request: Request, kind: str = "all", q: str = "", pinned: bool = False, limit: int = 120):
    dev_id, dev_name = _device_info(request)
    return {
        "drops": padd.list_drops(kind=kind, q=q, pinned_only=pinned, limit=limit),
        "storage": padd.storage_stats(),
        "device": {"id": dev_id, "name": dev_name},
        "zebralab_available": bool(config.ZEBRALAB_URL or padd.ZEBRALAB_TRANSPORT is not None),
    }


@router.post("/drops", status_code=201)
async def create_drop(body: DropTextIn, request: Request):
    dev_id, dev_name = _device_info(request)
    label = (body.device_name or dev_name).strip()[:60]
    try:
        drop = padd.create_text_drop(
            body.text,
            title=body.title,
            kind=None if body.kind == "auto" else body.kind,
            pinned=body.pinned,
            device_id=dev_id,
            device_name=label,
        )
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    if drop["kind"] == "link" and drop.get("url") and body.fetch_preview and (not config.NO_FETCH or padd.PREVIEW_TRANSPORT is not None):
        asyncio.create_task(padd.fetch_url_preview(drop["id"], drop["url"]))
    return {"drop": drop, "storage": padd.storage_stats()}


@router.post("/drops/file", status_code=201)
async def upload_drop_file(request: Request):
    """Raw streaming upload (same pattern as appearance backgrounds — no multipart dependency)."""
    dev_id, dev_name = _device_info(request)
    raw_name = unquote(request.headers.get("x-filename", "")).strip() or "upload.bin"
    caption = unquote(request.headers.get("x-caption", "")).strip()
    pinned = request.headers.get("x-pinned") == "1"
    label = (unquote(request.headers.get("x-device-name", "")).strip() or dev_name)[:60]
    ctype = request.headers.get("content-type", "")
    try:
        drop = await padd.save_streamed_file(
            request.stream(),
            filename=raw_name,
            content_type=ctype,
            caption=caption,
            pinned=pinned,
            device_id=dev_id,
            device_name=label,
        )
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return {"drop": drop, "storage": padd.storage_stats()}


@router.patch("/drops/{drop_id}")
async def patch_drop(drop_id: str, body: DropPatchIn):
    try:
        drop = padd.update_drop(drop_id, pinned=body.pinned, title=body.title, body=body.body, kind=body.kind)
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return {"drop": drop, "storage": padd.storage_stats()}


@router.delete("/drops/{drop_id}")
async def remove_drop(drop_id: str):
    if not padd.delete_drop(drop_id):
        raise HTTPException(404, "Drop not found.")
    notify.padd()
    return {"ok": True, "storage": padd.storage_stats()}


@router.post("/drops/clear")
async def clear_drops(body: DropClearIn):
    res = padd.clear_drops(include_pinned=body.include_pinned)
    notify.padd()
    return {**res, "storage": padd.storage_stats()}


@router.post("/drops/{drop_id}/promote", status_code=201)
async def promote_drop(drop_id: str, request: Request):
    _, dev_name = _device_info(request)
    try:
        note = padd.promote_drop_to_note(drop_id, device_name=dev_name)
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return {"note": note}


@router.post("/drops/{drop_id}/preview")
async def refresh_drop_preview(drop_id: str):
    d = padd.get_drop(drop_id)
    if not d or d["kind"] != "link" or not d.get("url"):
        raise HTTPException(404, "Link drop not found.")
    await padd.fetch_url_preview(drop_id, d["url"])
    return {"drop": padd.get_drop(drop_id)}


@router.post("/drops/{drop_id}/ticket")
async def create_drop_ticket(drop_id: str, body: TicketIn, request: Request):
    try:
        ticket = padd.make_ticket(drop_id, ttl=body.ttl)
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    origin = _origin(request)
    share_url = f"{origin}{ticket['share_path']}"
    download_url = f"{origin}{ticket['download_path']}" if ticket["download_path"] else None
    out = {**ticket, "share_url": share_url, "download_url": download_url}
    if body.qr:
        out["qr_svg"] = padd.qr_svg(share_url)
    return out


@router.post("/drops/{drop_id}/revoke-share")
async def revoke_drop_share(drop_id: str):
    if not padd.revoke_share(drop_id):
        raise HTTPException(404, "Drop not found.")
    return {"ok": True}


@router.post("/drops/{drop_id}/send-zebralab")
async def send_drop_zebralab(drop_id: str, body: ZebraLabSendIn):
    try:
        return await padd.send_to_zebralab(drop_id, target=body.target)
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None


@router.get("/files/{drop_id}")
async def get_drop_file(drop_id: str, inline: bool = False):
    d = padd.get_drop(drop_id)
    if not d or not d.get("stored_name"):
        raise HTTPException(404, "File not found.")
    path = padd.safe_file_path(d["stored_name"])
    if not path or not path.is_file():
        raise HTTPException(404, "File missing from disk.")
    allow_inline = inline and d.get("mime_type") in padd.INLINE_IMAGE_MIMES
    return FileResponse(
        path,
        media_type=d["mime_type"] or "application/octet-stream",
        headers=_file_headers(d["file_name"] or path.name, inline=allow_inline),
    )


# ---- Notes & Checklists routes ------------------------------------------------------------------

@router.get("/notes")
async def get_notes(q: str = "", tag: str = "", archived: bool = False):
    return padd.list_notes(q=q, tag=tag, archived=archived)


@router.post("/notes", status_code=201)
async def create_note(body: NoteCreateIn, request: Request):
    _, dev_name = _device_info(request)
    try:
        note = padd.create_note(
            title=body.title, body=body.body, kind=body.kind, tags=body.tags, pinned=body.pinned, device_name=dev_name
        )
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return {"note": note}


@router.patch("/notes/{note_id}")
async def patch_note(note_id: str, body: NotePatchIn, request: Request):
    _, dev_name = _device_info(request)
    try:
        note = padd.update_note(
            note_id,
            title=body.title, body=body.body, kind=body.kind, tags=body.tags,
            pinned=body.pinned, archived=body.archived, device_name=dev_name,
        )
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return {"note": note}


@router.post("/notes/{note_id}/toggle-check")
async def toggle_note_check(note_id: str, body: ToggleCheckIn, request: Request):
    _, dev_name = _device_info(request)
    try:
        note = padd.toggle_checklist_line(note_id, body.line_index, device_name=dev_name)
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return {"note": note}


@router.post("/notes/{note_id}/checklist-action")
async def note_checklist_action(note_id: str, body: ChecklistActionIn, request: Request):
    _, dev_name = _device_info(request)
    try:
        note = padd.checklist_action(note_id, body.action, device_name=dev_name)
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return {"note": note}


@router.delete("/notes/{note_id}")
async def remove_note(note_id: str):
    if not padd.delete_note(note_id):
        raise HTTPException(404, "Note not found.")
    notify.padd()
    return {"ok": True}


# ---- Home Assistant Shopping / Todo routes ------------------------------------------------------

@router.get("/lists")
async def get_lists(request: Request):
    return await padd.fetch_ha_lists(request.app.state.padd_lists)


@router.post("/lists/{entity_id}/items", status_code=201)
async def add_list_item(entity_id: str, body: TodoAddIn, request: Request):
    try:
        data = await padd.add_ha_item(entity_id, body.summary, request.app.state.padd_lists)
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return data


@router.patch("/lists/{entity_id}/items")
async def update_list_item(entity_id: str, body: TodoUpdateIn, request: Request):
    try:
        data = await padd.update_ha_item(
            entity_id, body.item, status=body.status, rename=body.rename, lists_state=request.app.state.padd_lists
        )
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return data


@router.post("/lists/{entity_id}/items/remove")
async def remove_list_item(entity_id: str, body: TodoRemoveIn, request: Request):
    try:
        data = await padd.remove_ha_item(entity_id, body.item, request.app.state.padd_lists)
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return data


@router.post("/lists/{entity_id}/clear-completed")
async def clear_list_completed(entity_id: str, request: Request):
    try:
        data = await padd.clear_ha_completed(entity_id, request.app.state.padd_lists)
    except padd.PaddError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    notify.padd()
    return data


# ---- Public signed transfer endpoints (HMAC + expiry + share_nonce) -----------------------------

@public.get("/padd/dl/{drop_id}/{exp}/{sig}")
async def signed_download(drop_id: str, exp: int, sig: str):
    d = padd.verify_ticket(drop_id, exp, sig)
    if not d or not d.get("stored_name"):
        raise HTTPException(410, "This download link has expired or been revoked.")
    path = padd.safe_file_path(d["stored_name"])
    if not path or not path.is_file():
        raise HTTPException(404, "File missing from disk.")
    return FileResponse(
        path,
        media_type=d["mime_type"] or "application/octet-stream",
        headers=_file_headers(d["file_name"] or path.name, inline=False),
    )


def _fmt_bytes(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1000 or unit == "GB":
            return f"{int(round(val))} {unit}" if unit == "B" or val >= 100 else f"{val:.1f} {unit}"
        val /= 1000
    return f"{n} B"


@public.get("/padd/share/{drop_id}/{exp}/{sig}")
async def signed_share_page(drop_id: str, exp: int, sig: str):
    d = padd.verify_ticket(drop_id, exp, sig)
    if not d:
        return HTMLResponse(
            """<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Citadel PADD</title>
<link rel="stylesheet" href="/static/css/tokens.css"><link rel="stylesheet" href="/static/css/components.css">
<link rel="stylesheet" href="/static/css/guest.css"></head><body><main class="guest"><div class="guest__wrap">
<span class="wordmark">Citadel · PADD</span>
<p class="guest__hello">This share link has expired or been revoked.</p>
</div></main></body></html>""",
            status_code=410,
            headers={"Cache-Control": "no-store"},
        )

    title = html.escape(d["title"] or d["file_name"] or d["url_host"] or "Shared drop")
    device = html.escape(d["device_name"] or "Citadel")
    dl_href = f"/padd/dl/{html.escape(drop_id)}/{exp}/{html.escape(sig)}"

    if d.get("stored_name"):
        size_label = _fmt_bytes(int(d.get("size_bytes") or 0))
        caption = f"<p style='margin:0 0 14px;color:var(--silver);white-space:pre-wrap'>{html.escape(d['body'])}</p>" if d.get("body") else ""
        content = (
            f"<p style='margin:0 0 10px;font-family:var(--font-mono);font-size:13px;color:var(--silver-mute)'>"
            f"{html.escape(d['file_name'] or 'file')} · {size_label} · from {device}</p>"
            f"{caption}"
            f"<a class='btn btn--solid' href='{dl_href}'>Download file</a>"
        )
    elif d["kind"] == "link" and d.get("url"):
        safe_href = html.escape(d["url"])
        desc = f"<p style='margin:0 0 12px;color:var(--silver)'>{html.escape(d['url_description'])}</p>" if d.get("url_description") else ""
        content = (
            f"<p style='margin:0 0 8px;font-family:var(--font-mono);font-size:12px;color:var(--silver-mute)'>Link · from {device}</p>"
            f"{desc}"
            f"<textarea id='share-text' readonly rows='3' style='width:100%;margin-bottom:12px;padding:10px;background:var(--surface-2);color:var(--silver-hi);border:1px solid var(--edge);font-family:var(--font-mono);font-size:13px'>{safe_href}</textarea>"
            f"<div style='display:flex;gap:8px;flex-wrap:wrap'>"
            f"<a class='btn btn--solid' href='{safe_href}' target='_blank' rel='noopener noreferrer'>Open link</a>"
            f"<button class='btn' type='button' id='share-copy'>Copy link</button></div>"
        )
    else:
        body_esc = html.escape(d["body"])
        mono = "font-family:var(--font-mono);font-size:13px;" if d["kind"] == "code" else "font-size:14px;"
        content = (
            f"<p style='margin:0 0 8px;font-family:var(--font-mono);font-size:12px;color:var(--silver-mute)'>From {device}</p>"
            f"<textarea id='share-text' readonly rows='10' style='width:100%;margin-bottom:12px;padding:10px;background:var(--surface-2);color:var(--silver-hi);border:1px solid var(--edge);{mono}'>{body_esc}</textarea>"
            f"<button class='btn btn--solid' type='button' id='share-copy'>Copy text</button>"
        )

    return HTMLResponse(
        f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Citadel PADD · {title}</title>
<link rel="stylesheet" href="/static/css/tokens.css"><link rel="stylesheet" href="/static/css/components.css">
<link rel="stylesheet" href="/static/css/guest.css"></head><body><main class="guest"><div class="guest__wrap" style="max-width:600px;text-align:left">
<span class="wordmark">Citadel · PADD</span>
<h1 class="guest__hello" style="margin:12px 0 10px">{title}</h1>
{content}
</div></main><script src="/static/js/padd-share.js"></script></body></html>""",
        status_code=200,
        headers={"Cache-Control": "no-store"},
    )
