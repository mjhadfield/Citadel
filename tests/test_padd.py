"""PADD tests: Dropzone CRUD, streaming file uploads, signed tickets/QR, SSRF-safe URL previews,
ZebraLab bridge, Notes & Checklists, Home Assistant Todo sync, role isolation, and CLI helpers."""
from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path

import httpx
import pytest

from app import auth, config, db, homeassistant, padd


@pytest.fixture(autouse=True)
def _clean_padd(monkeypatch):
    """Start each test with empty PADD tables and reset monkeypatched transports/caches."""
    with db.db() as con:
        con.execute("DELETE FROM padd_drops")
        con.execute("DELETE FROM padd_notes")
    pdir = padd.padd_dir()
    for p in pdir.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)
    padd._preview_cache.clear()
    monkeypatch.setattr(padd, "PREVIEW_TRANSPORT", None)
    monkeypatch.setattr(padd, "ZEBRALAB_TRANSPORT", None)
    monkeypatch.setattr(homeassistant, "TRANSPORT", None)
    yield


# ---- Role isolation -----------------------------------------------------------------------------

def test_guest_and_kiosk_cannot_access_padd(guest_client):
    assert guest_client.get("/api/owner/padd/drops").status_code == 403
    assert guest_client.post("/api/owner/padd/drops", json={"text": "hi"}, headers={"X-Hub": "1"}).status_code == 403
    assert guest_client.get("/api/owner/padd/notes").status_code == 403
    assert guest_client.get("/api/owner/padd/lists").status_code == 403


# ---- Dropzone text, link, code, pin, clear, promote ---------------------------------------------

def test_dropzone_classification_and_crud(owner_client):
    # Link auto-detection
    r = owner_client.post("/api/owner/padd/drops", json={"text": "https://example.org/path?q=1", "fetch_preview": False})
    assert r.status_code == 201
    link = r.json()["drop"]
    assert link["kind"] == "link"
    assert link["url"] == "https://example.org/path?q=1"
    assert link["url_host"] == "example.org"
    assert link["device_name"] == "pytest"
    assert "share_nonce" not in link

    # Code auto-detection
    r = owner_client.post("/api/owner/padd/drops", json={"text": "sudo systemctl --user restart hub\njournalctl -n 10"})
    assert r.status_code == 201
    code = r.json()["drop"]
    assert code["kind"] == "code"

    # Plain text auto-detection (including English words like 'from')
    r = owner_client.post("/api/owner/padd/drops", json={"text": "Pick up parcel from shop at 4pm", "title": "Reminder"})
    assert r.status_code == 201
    txt = r.json()["drop"]
    assert txt["kind"] == "text" and txt["title"] == "Reminder"

    # Pin text so it sorts first and survives clear-unpinned
    r = owner_client.patch(f"/api/owner/padd/drops/{txt['id']}", json={"pinned": True})
    assert r.status_code == 200 and r.json()["drop"]["pinned"] is True

    listing = owner_client.get("/api/owner/padd/drops").json()
    assert [d["id"] for d in listing["drops"]] == [txt["id"], code["id"], link["id"]]

    # Filter by kind & search
    assert len(owner_client.get("/api/owner/padd/drops?kind=link").json()["drops"]) == 1
    assert len(owner_client.get("/api/owner/padd/drops?q=parcel").json()["drops"]) == 1

    # Promote link drop to persistent Note
    prom = owner_client.post(f"/api/owner/padd/drops/{link['id']}/promote", json={})
    assert prom.status_code == 201
    note = prom.json()["note"]
    assert "https://example.org/path?q=1" in note["body"]
    assert "drop" in note["tags"]

    # Clear unpinned keeps only the pinned item
    clr = owner_client.post("/api/owner/padd/drops/clear", json={"include_pinned": False})
    assert clr.status_code == 200 and clr.json()["removed"] == 2
    remaining = owner_client.get("/api/owner/padd/drops").json()["drops"]
    assert [d["id"] for d in remaining] == [txt["id"]]

    # Delete pinned item
    assert owner_client.delete(f"/api/owner/padd/drops/{txt['id']}").status_code == 200
    assert owner_client.get("/api/owner/padd/drops").json()["drops"] == []


# ---- Streaming file uploads, caps, inline vs attachment CSP, signed tickets & QR ----------------

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_file_upload_sniffing_headers_and_cleanup(owner_client, guest_client):
    # Upload raster PNG image
    r = owner_client.post(
        "/api/owner/padd/drops/file",
        content=PNG_1X1,
        headers={"X-Filename": "screen%20shot.png", "X-Caption": "Test%20shot", "Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 201
    img = r.json()["drop"]
    assert img["kind"] == "image"
    assert img["mime_type"] == "image/png"
    assert img["file_name"] == "screen shot.png"
    assert img["body"] == "Test shot"
    assert img["sha256"] == hashlib.sha256(PNG_1X1).hexdigest()
    assert img["can_inline"] is True and img["can_zebralab_doc"] is True and img["can_zebralab_asset"] is True
    stored_path = padd.safe_file_path(img["stored_name"])
    assert stored_path and stored_path.is_file()

    # Inline preview serves raster image with strict sandbox CSP
    pv = owner_client.get(f"/api/owner/padd/files/{img['id']}?inline=1")
    assert pv.status_code == 200
    assert pv.content == PNG_1X1
    assert "sandbox" in pv.headers["content-security-policy"]
    assert pv.headers["content-disposition"].startswith("inline;")

    # Upload an HTML file claiming image/png — magic-byte check refuses to trust it as an inline image
    html_bytes = b"<script>alert(1)</script>"
    r2 = owner_client.post(
        "/api/owner/padd/drops/file",
        content=html_bytes,
        headers={"X-Filename": "../evil.html", "Content-Type": "image/png"},
    )
    assert r2.status_code == 201
    fdrop = r2.json()["drop"]
    assert fdrop["kind"] == "file"
    assert fdrop["file_name"] == "evil.html"
    assert fdrop["can_inline"] is False
    # Even with ?inline=1, non-raster file MUST force attachment download + sandbox CSP
    dl = owner_client.get(f"/api/owner/padd/files/{fdrop['id']}?inline=1")
    assert dl.status_code == 200
    assert dl.headers["content-disposition"].startswith("attachment;")
    assert "sandbox" in dl.headers["content-security-policy"]

    # Signed download ticket works without owner cookie (for CitadelAndroid & QR grabs)
    tk = owner_client.post(f"/api/owner/padd/drops/{img['id']}/ticket", json={"ttl": 120, "qr": True})
    assert tk.status_code == 200
    tdata = tk.json()
    assert "<svg" in tdata["qr_svg"]
    pub_dl = guest_client.get(tdata["download_path"])
    assert pub_dl.status_code == 200
    assert pub_dl.content == PNG_1X1
    assert pub_dl.headers["content-disposition"].startswith("attachment;")

    # Public share page works for unenrolled guest
    pub_sh = guest_client.get(tdata["share_path"])
    assert pub_sh.status_code == 200
    assert "screen shot.png" in pub_sh.text

    # Tampered signature fails
    bad_path = tdata["download_path"][:-4] + "0000"
    assert guest_client.get(bad_path).status_code == 410

    # Revoking share nonce invalidates previously issued ticket immediately
    assert owner_client.post(f"/api/owner/padd/drops/{img['id']}/revoke-share", json={}).status_code == 200
    assert guest_client.get(tdata["download_path"]).status_code == 410
    assert guest_client.get(tdata["share_path"]).status_code == 410

    # Deleting drop unlinks disk file
    assert owner_client.delete(f"/api/owner/padd/drops/{img['id']}").status_code == 200
    assert not stored_path.exists()


def test_file_size_and_total_cap_enforcement(owner_client, monkeypatch):
    monkeypatch.setattr(padd, "MAX_FILE_BYTES", 100)
    monkeypatch.setattr(padd, "MAX_TOTAL_BYTES", 160)

    # Exceeds per-file limit
    r = owner_client.post("/api/owner/padd/drops/file", content=b"x" * 101, headers={"X-Filename": "big.bin"})
    assert r.status_code == 413

    # Fits within per-file and total cap
    r1 = owner_client.post("/api/owner/padd/drops/file", content=b"a" * 90, headers={"X-Filename": "one.bin"})
    assert r1.status_code == 201

    # Second 90-byte upload exceeds 160-byte total cap
    r2 = owner_client.post("/api/owner/padd/drops/file", content=b"b" * 90, headers={"X-Filename": "two.bin"})
    assert r2.status_code == 507
    # No leftover .tmp files left on disk
    assert list(padd.padd_dir().glob("*.tmp")) == []


# ---- Polite, SSRF-safe URL preview fetcher ------------------------------------------------------

def test_url_preview_extracts_title_and_blocks_ssrf(owner_client, monkeypatch):
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><head><title>Example Article</title><meta name='description' content='Helpful summary.'></head></html>",
        )

    monkeypatch.setattr(padd, "PREVIEW_TRANSPORT", httpx.MockTransport(handler))

    # Public URL fetches metadata
    r = owner_client.post("/api/owner/padd/drops", json={"text": "https://example.com/post", "fetch_preview": False})
    did = r.json()["drop"]["id"]
    pv = owner_client.post(f"/api/owner/padd/drops/{did}/preview", json={})
    assert pv.status_code == 200
    d = pv.json()["drop"]
    assert d["title"] == "Example Article"
    assert d["url_description"] == "Helpful summary."
    assert seen == ["https://example.com/post"]

    # Private / loopback / Tailscale URLs are refused by SSRF guard (never hit transport)
    for bad_url in ("http://127.0.0.1:8080/secret", "http://192.168.0.66:8080/", "http://100.127.118.96:8080/", "http://homeassistant.local/"):
        r_bad = owner_client.post("/api/owner/padd/drops", json={"text": bad_url, "fetch_preview": False})
        bad_id = r_bad.json()["drop"]["id"]
        owner_client.post(f"/api/owner/padd/drops/{bad_id}/preview", json={})
    assert seen == ["https://example.com/post"]


# ---- ZebraLab bridge ----------------------------------------------------------------------------

def test_send_to_zebralab_bridge(owner_client, monkeypatch):
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("x-zebralab") == "1"
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={"doc_id": "abc123", "filename": "label.pdf"})

    monkeypatch.setattr(padd, "ZEBRALAB_TRANSPORT", httpx.MockTransport(handler))

    pdf_bytes = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF"
    up = owner_client.post("/api/owner/padd/drops/file", content=pdf_bytes, headers={"X-Filename": "postage.pdf"})
    did = up.json()["drop"]["id"]

    res = owner_client.post(f"/api/owner/padd/drops/{did}/send-zebralab", json={"target": "document"})
    assert res.status_code == 200
    assert res.json()["zebralab"]["doc_id"] == "abc123"
    assert calls == [("POST", "/api/documents")]

    # Sending a PDF as an image asset is cleanly rejected
    bad = owner_client.post(f"/api/owner/padd/drops/{did}/send-zebralab", json={"target": "asset"})
    assert bad.status_code == 422


# ---- Notes & Checklists -------------------------------------------------------------------------

def test_notes_and_checklists_atomic_toggle_and_actions(owner_client):
    r = owner_client.post("/api/owner/padd/notes", json={
        "title": "Trip packing",
        "kind": "checklist",
        "body": "- [ ] Passport\n- [ ] Charger\n- [x] Earplugs",
        "tags": ["Travel", "gigs", "travel"],
    })
    assert r.status_code == 201
    note = r.json()["note"]
    assert note["tags"] == ["travel", "gigs"]
    assert note["check_done"] == 1 and note["check_total"] == 3

    # Toggle line 0 ([ ] -> [x])
    t = owner_client.post(f"/api/owner/padd/notes/{note['id']}/toggle-check", json={"line_index": 0})
    assert t.status_code == 200
    assert t.json()["note"]["check_done"] == 2
    assert t.json()["note"]["body"].startswith("- [x] Passport")

    # Reset all items (reusable checklist)
    rst = owner_client.post(f"/api/owner/padd/notes/{note['id']}/checklist-action", json={"action": "reset"})
    assert rst.status_code == 200
    assert rst.json()["note"]["check_done"] == 0 and rst.json()["note"]["check_total"] == 3

    # Check item 1 then clear_done removes only item 1
    owner_client.post(f"/api/owner/padd/notes/{note['id']}/toggle-check", json={"line_index": 1})
    clr = owner_client.post(f"/api/owner/padd/notes/{note['id']}/checklist-action", json={"action": "clear_done"})
    assert clr.status_code == 200
    assert clr.json()["note"]["check_total"] == 2
    assert "Charger" not in clr.json()["note"]["body"]

    # Tag filter & archive
    listed = owner_client.get("/api/owner/padd/notes?tag=gigs").json()
    assert len(listed["notes"]) == 1
    assert owner_client.patch(f"/api/owner/padd/notes/{note['id']}", json={"archived": True}).status_code == 200
    assert owner_client.get("/api/owner/padd/notes").json()["notes"] == []
    assert len(owner_client.get("/api/owner/padd/notes?archived=1").json()["notes"]) == 1


# ---- Home Assistant Shopping / Todo sync --------------------------------------------------------

def test_ha_todo_lists_sync(owner_client, monkeypatch, tmp_path):
    tok = tmp_path / "ha_tok"
    tok.write_text("fake-ha-token")
    monkeypatch.setattr(config, "HA_TOKEN_FILE", tok)

    items = [
        {"uid": "u1", "summary": "Milk", "status": "needs_action"},
        {"uid": "u2", "summary": "Bread", "status": "completed"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/states":
            return httpx.Response(200, json=[
                {"entity_id": "light.office", "attributes": {"friendly_name": "Office"}},
                {"entity_id": "todo.shopping_list", "attributes": {"friendly_name": "Shopping list", "supported_features": 15}},
            ])
        if req.method == "POST" and req.url.path == "/api/services/todo/get_items":
            return httpx.Response(200, json={"service_response": {"todo.shopping_list": {"items": items}}})
        body = json.loads(req.content.decode("utf-8"))
        if req.url.path == "/api/services/todo/add_item":
            items.append({"uid": f"u{len(items)+1}", "summary": body["item"], "status": "needs_action"})
            return httpx.Response(200, json=[])
        if req.url.path == "/api/services/todo/update_item":
            for it in items:
                if it["uid"] == body["item"] or it["summary"] == body["item"]:
                    if "status" in body:
                        it["status"] = body["status"]
                    if "rename" in body:
                        it["summary"] = body["rename"]
            return httpx.Response(200, json=[])
        if req.url.path == "/api/services/todo/remove_item":
            items[:] = [it for it in items if it["uid"] != body["item"] and it["summary"] != body["item"]]
            return httpx.Response(200, json=[])
        if req.url.path == "/api/services/todo/remove_completed_items":
            items[:] = [it for it in items if it["status"] != "completed"]
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    monkeypatch.setattr(homeassistant, "TRANSPORT", httpx.MockTransport(handler))

    # Fetch lists
    res = owner_client.get("/api/owner/padd/lists")
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is True and len(data["lists"]) == 1
    assert data["lists"][0]["open_count"] == 1

    # Add item
    res = owner_client.post("/api/owner/padd/lists/todo.shopping_list/items", json={"summary": "Coffee"})
    assert res.status_code == 201 and res.json()["lists"][0]["open_count"] == 2

    # Toggle & rename
    res = owner_client.patch("/api/owner/padd/lists/todo.shopping_list/items", json={"item": "u1", "status": "completed", "rename": "Oat milk"})
    assert res.status_code == 200
    assert any(i["summary"] == "Oat milk" and i["status"] == "completed" for i in res.json()["lists"][0]["items"])

    # Clear completed
    res = owner_client.post("/api/owner/padd/lists/todo.shopping_list/clear-completed", json={})
    assert res.status_code == 200
    assert [i["summary"] for i in res.json()["lists"][0]["items"]] == ["Coffee"]

    # Entity allow-list rejects arbitrary entity ids
    bad = owner_client.post("/api/owner/padd/lists/light.office/items", json={"summary": "nope"})
    assert bad.status_code == 422
    bad2 = owner_client.post("/api/owner/padd/lists/todo.unknown/items", json={"summary": "nope"})
    assert bad2.status_code == 404


def test_orphan_temp_pruning():
    tmp = padd.padd_dir() / "stale.tmp"
    tmp.write_bytes(b"partial")
    old = time.time() - 7200
    import os
    os.utime(tmp, (old, old))
    assert padd.prune_orphan_temps(3600) == 1
    assert not tmp.exists()
