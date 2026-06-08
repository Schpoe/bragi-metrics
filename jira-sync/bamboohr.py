#!/usr/bin/env python3
"""BambooHR API client — read-only.

Pulls the employee directory and the "Who's Out" feed (approved time off +
company holidays) used to compute team capacity. Mirrors the defensive style of
the Jira client in sync.py: only GET requests, exponential backoff, and respect
for BambooHR's 503 + Retry-After throttling.

Auth: HTTP Basic with the API key as the username and the literal "x" as the
password (per BambooHR docs).
"""

import logging
import time

import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger(__name__)


class BambooClient:
    def __init__(self, subdomain, api_key, max_retries=5, timeout=30):
        if not subdomain or not api_key:
            raise ValueError("BambooHR subdomain and API key are required")
        # Tolerate full domain / URL forms — we only want the subdomain label.
        sub = subdomain.strip().replace("https://", "").replace("http://", "").strip("/")
        if sub.endswith(".bamboohr.com"):
            sub = sub[: -len(".bamboohr.com")]
        self.base = f"https://{sub}.bamboohr.com/api/v1"
        self.max_retries = max_retries
        self.timeout = timeout
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(api_key, "x")
        self._session.headers.update({"Accept": "application/json"})

    def _get(self, path, params=None):
        url = f"{self.base}/{path.lstrip('/')}"
        for attempt in range(self.max_retries):
            resp = self._session.get(url, params=params, timeout=self.timeout)
            # BambooHR throttles with 503; honour Retry-After when present.
            if resp.status_code in (429, 503):
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("BambooHR %s → %d, retrying in %ds (attempt %d/%d)",
                            path, resp.status_code, wait, attempt + 1, self.max_retries)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning("BambooHR %s → %d, retrying in %ds (attempt %d/%d)",
                            path, resp.status_code, wait, attempt + 1, self.max_retries)
                time.sleep(wait)
                continue
            if not resp.ok:
                log.error("BambooHR error %d: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"BambooHR request failed after {self.max_retries} attempts: {path}")

    def directory(self):
        """Return the list of employees from /employees/directory.

        Each item has at least: id, displayName, workEmail (may be empty).
        """
        data = self._get("employees/directory")
        return data.get("employees", []) if isinstance(data, dict) else []

    def whos_out(self, start, end):
        """Return Who's Out items between start and end (YYYY-MM-DD, inclusive).

        Each item: id, type ('timeOff' | 'holiday'), employeeId (absent for
        holidays), name, start, end.
        """
        data = self._get("time_off/whos_out", params={"start": start, "end": end})
        return data if isinstance(data, list) else []


def parse_whos_out(items):
    """Normalise raw Who's Out items into absence rows.

    Returns list of dicts: id, employee_id (None for holidays), kind, start_date,
    end_date, label. Items without a usable id or dates are skipped.
    """
    rows = []
    for it in items:
        item_id = it.get("id")
        start = it.get("start")
        end = it.get("end")
        if item_id is None or not start or not end:
            continue
        kind = it.get("type", "timeOff")
        emp = it.get("employeeId")
        rows.append({
            "id": int(item_id),
            "employee_id": int(emp) if emp not in (None, "") else None,
            "kind": "holiday" if kind == "holiday" else "timeOff",
            "start_date": start,
            "end_date": end,
            "label": it.get("name"),
        })
    return rows
