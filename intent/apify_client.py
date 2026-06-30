"""Apify client for the intent miner.

Two call styles:
  - run_sync : run-sync-get-dataset-items for actors with small responses (post-search,
               google-search, post-detail). Field-filtered + robust to mid-stream
               truncation through the egress proxy (IncompleteRead).
  - run_async: start a run, poll to completion, page the dataset. Required for the
               comments actor, which times out under run-sync (transfer-brief gotcha).

Reuses APIFY_API_TOKEN from the environment (same token as the scrape).
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request

APIFY_BASE = "https://api.apify.com/v2"
_TRANSIENT = (http.client.IncompleteRead, ConnectionError, urllib.error.URLError)


def _token() -> str:
    t = os.environ.get("APIFY_API_TOKEN")
    if not t:
        raise SystemExit("ERROR: APIFY_API_TOKEN not set (see .env).")
    return t


def _post(url: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url: str, timeout: int = 120, attempts: int = 4):
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except _TRANSIENT:
            if i < attempts - 1:
                time.sleep(3 * (i + 1))
                continue
            raise


def run_sync(actor: str, payload: dict, attempts: int = 3, fields=None):
    """run-sync-get-dataset-items. Returns the dataset items (list)."""
    url = f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items?token={_token()}"
    if fields:
        url += "&fields=" + ",".join(fields)
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < attempts - 1:
                time.sleep(5 * (i + 1))
                continue
            raise
        except _TRANSIENT:
            if i < attempts - 1:
                time.sleep(5 * (i + 1))
                continue
            raise
    return []


def run_async(actor: str, payload: dict, poll_every: int = 8, timeout: int = 900) -> list:
    """Start an actor run, poll to completion, then page the default dataset. Used for
    the comments actor (run-sync times out on it)."""
    token = _token()
    run = _post(f"{APIFY_BASE}/acts/{actor}/runs?token={token}", payload)["data"]
    run_id = run["id"]
    ds = run.get("defaultDatasetId")
    waited = 0
    while True:
        st = _get(f"{APIFY_BASE}/actor-runs/{run_id}?token={token}")["data"]
        status = st.get("status")
        ds = ds or st.get("defaultDatasetId")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"actor {actor} run {run_id} ended {status}")
        if waited >= timeout:
            raise TimeoutError(f"actor {actor} run {run_id} still {status} after {timeout}s")
        time.sleep(poll_every)
        waited += poll_every

    items, offset = [], 0
    while True:
        page = _get(f"{APIFY_BASE}/datasets/{ds}/items?token={token}&clean=true"
                    f"&offset={offset}&limit=1000")
        if not page:
            break
        items.extend(page)
        offset += len(page)
        if len(page) < 1000:
            break
    return items
