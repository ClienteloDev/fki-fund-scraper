"""Batch-06 fetch/read layer.

Wraps scripts/emergency/em.py so that

  * reads walk every cache the programme already holds - the 17 emergency
    caches, every cache/recovery-online/*/manual and 000-shared-hubs/manual -
    so a document already on disk is never fetched again;
  * writes land in the batch-06 workspace only:
        cache/recovery-online/<rank>-<slug>/manual
    never in cache/http, cache/parsed, cache/regen.sqlite3 or the emergency
    caches.

`served_by(url)` answers the acquisition question the batch report has to
report honestly: was this document already on disk, or newly acquired?
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "emergency"))

import em  # noqa: E402

_EMERGENCY = list(em.CACHES)
_MANUAL = [Path(p) for p in sorted(glob.glob(str(ROOT / "cache" / "recovery-online" / "*" / "manual")))]
_PIPELINE_HTTP = [ROOT / "cache" / "http"] + [
    Path(p) for p in sorted(glob.glob(str(ROOT / "cache" / "recovery-online" / "*" / "http")))
]

# pre-existing state, captured before batch 06 writes anything
_PREEXISTING_MANUAL = list(_MANUAL)
_PREEXISTING_EM_KEYS = None


def use(workspace: str):
    """Point writes at cache/recovery-online/<workspace>/manual."""
    out = ROOT / "cache" / "recovery-online" / workspace / "manual"
    out.mkdir(parents=True, exist_ok=True)
    em.OUT = out
    em.CACHES = [out] + _MANUAL + _EMERGENCY
    return out


def get(url, force=False, timeout=45):
    return em.get(url, force=force, timeout=timeout)


def probe(url, force=False):
    return em.probe(url, force=force)


def show(url, grep=None, ctx=3, limit=6000, force=False, encoding=None):
    return em.show(url, grep=grep, ctx=ctx, limit=limit, force=force, encoding=encoding)


def text(url, force=False, encoding=None):
    meta, body = em.get(url, force=force)
    ct = (meta.get("content_type") or "").lower()
    if "pdf" in ct or body[:5] == b"%PDF-":
        t, pages = em.pdf_text(body)
        return meta, t, pages
    return meta, em.html_text(body, encoding), 0


def links(url, encoding=None):
    meta, body = em.get(url)
    return em.links(body, encoding=encoding)


def served_by(url, workspace=None):
    """Which cache already held this URL *before* batch 06 fetched it.

    Returns one of: 'emergency', 'recovery-online', 'pipeline-http',
    'batch-06' (only this batch's workspace has it) or 'absent'.
    """
    k24 = hashlib.sha256(url.encode()).hexdigest()[:24]
    k64 = hashlib.sha256(url.encode()).hexdigest()
    for cache in _EMERGENCY:
        if (cache / f"{k24}.meta.json").exists():
            return "emergency"
    for cache in _PREEXISTING_MANUAL:
        if workspace and cache.parent.name == workspace:
            continue
        if (cache / f"{k24}.meta.json").exists():
            return "recovery-online"
    for cache in _PIPELINE_HTTP:
        if (cache / f"{k64}.json").exists():
            return "pipeline-http"
    if workspace and (ROOT / "cache" / "recovery-online" / workspace / "manual" / f"{k24}.meta.json").exists():
        return "batch-06"
    return "absent"


def probe_quality(t, pages):
    """Text-layer verdict, including the batch-05 broken-font check."""
    n = len(t)
    if pages >= 10 and n < 500:
        return "image_only_or_esef"
    if pages > 0 and n < 200:
        return "no_text_layer"
    latin = sum(1 for c in t if ("a" <= c.lower() <= "z") or c in "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")
    letters = sum(1 for c in t if c.isalpha())
    if letters > 200 and latin / letters < 0.80:
        return "broken_font_encoding"
    return "ok"


if __name__ == "__main__":
    use(sys.argv[1])
    show(sys.argv[2], grep=sys.argv[3:] or None)
