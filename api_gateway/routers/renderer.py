"""
api_gateway.routers.renderer (v4.3)
==================================
Proxies program preview assets from program_renderer service so Operator UI
only needs one origin (api_gateway).

Expected renderer base_url in config:
  program_renderer:
    base_url: "http://127.0.0.1:8030"
"""
from __future__ import annotations

import urllib.request
import urllib.error

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router = APIRouter(prefix="/api")

def _base(request: Request) -> str:
    cfg = getattr(request.app.state, "config", {}) or {}
    return (cfg.get("program_renderer") or {}).get("base_url", "http://127.0.0.1:8030").rstrip("/")

@router.get("/renderer/preview/automation_next.jpg")
def automation_next_jpg(request: Request):
    url = _base(request) + "/preview/automation_next.jpg"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as r:
            data = r.read()
        return Response(content=data, media_type="image/jpeg")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Renderer unavailable: {e}")



@router.get("/renderer/preview/automation_next2.jpg")
def automation_next2_jpg(request: Request):
    url = _base(request) + "/preview/automation_next2.jpg"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as r:
            data = r.read()
        return Response(content=data, media_type="image/jpeg")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Renderer unavailable: {e}")


@router.get("/renderer/preview/program.jpg")
def program_jpg(request: Request):
    url = _base(request) + "/preview/program.jpg"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as r:
            data = r.read()
        return Response(content=data, media_type="image/jpeg")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Renderer unavailable: {e}")


@router.get("/renderer/preview/pvw.jpg")
def pvw_jpg(request: Request):
    url = _base(request) + "/preview/pvw.jpg"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as r:
            data = r.read()
        return Response(content=data, media_type="image/jpeg")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Renderer unavailable: {e}")

@router.get("/renderer/status")
def renderer_status(request: Request):
    url = _base(request) + "/status"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as r:
            return Response(content=r.read(), media_type="application/json")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Renderer unavailable: {e}")
