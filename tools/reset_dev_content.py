"""Preview or clear exportable content in an explicitly named Penfield dev tenant."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import penfield_auth
from penfield_import import APIError, PenfieldClient

DEV_ORIGIN = "https://api-dev.penfield.app"
SECTIONS = ("relationships", "documents", "artifacts", "memories")


def _data(reply):
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict) or reply.get("status") == "error" or data.get("success") is False:
        raise ValueError("Invalid inventory response")
    return data


def _record_ids(api, section):
    found = set()
    page = 1
    expected = None
    while True:
        params = {"page": page, "per_page": 100}
        if section == "memories":
            params["include_inactive"] = "true"
        data = _data(api.request("GET", f"/{section}?" + urlencode(params)))
        rows = data.get("items")
        pagination = data.get("pagination")
        if not isinstance(rows, list) or not isinstance(pagination, dict):
            raise ValueError(f"Malformed {section} inventory")
        total = pagination.get("total")
        more = pagination.get("has_next")
        if (type(total) is not int or total < 0 or type(more) is not bool
                or pagination.get("page", page) != page or (expected is not None and total != expected)):
            raise ValueError(f"Unreliable pagination in {section} on page {page}")
        expected = total
        for row in rows:
            key = row.get("id") if isinstance(row, dict) else None
            if not isinstance(key, str) or not key or key in found or "/" in key or key in (".", ".."):
                raise ValueError(f"Missing, invalid or repeated ID in {section}")
            found.add(key)
        if not more:
            if len(found) != expected:
                raise ValueError(f"Incomplete {section} listing: received {len(found)} of {expected}")
            return sorted(found)
        if not rows or len(found) >= expected:
            raise ValueError(f"Inconsistent {section} pagination")
        page += 1


def _child_path(folder, name):
    if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
        raise ValueError(f"Invalid artifact list entry in {folder}")
    return folder.rstrip("/") + "/" + name


def _artifact_paths(api):
    pending = ["/"]
    visited = set()
    files = set()
    while pending:
        folder = pending.pop()
        if folder in visited:
            continue
        visited.add(folder)
        data = _data(api.request("GET", "/artifacts/list?" + urlencode({"prefix": folder})))
        if not isinstance(data.get("folders"), list) or not isinstance(data.get("files"), list):
            raise ValueError(f"Incomplete artifact inventory in {folder}")
        listed = data.get("path")
        if not isinstance(listed, str) or listed.rstrip("/") != folder.rstrip("/"):
            raise ValueError(f"Unexpected folder returned for {folder}")
        for record in data["files"]:
            files.add(_child_path(folder, record.get("name") if isinstance(record, dict) else record))
        for child in data["folders"]:
            path = _child_path(folder, child) + "/"
            pending.append(path)
    return sorted(files)


def _inventory_section(api, section):
    return _artifact_paths(api) if section == "artifacts" else _record_ids(api, section)


def _object_route(section, key):
    if section == "artifacts":
        return "/artifacts?" + urlencode({"path": key})
    return "/" + section + "/" + quote(key, safe="")


def _identity(api, expected_tenant):
    if not isinstance(expected_tenant, str) or not expected_tenant.strip():
        raise ValueError("A specific dev tenant ID is required")
    identity = json.loads(api.destination_identity())
    if not isinstance(identity, list) or len(identity) != 3 or identity[:2] != [DEV_ORIGIN, expected_tenant]:
        raise ValueError("Verified destination does not match the fixed dev origin and requested tenant")
    return identity


def reset_dev_content(api: PenfieldClient, expected_tenant: str, *, apply: bool = False) -> dict:
    """Apply deletes only inventoried objects, verifies each by key, then checks all lists."""
    identity = _identity(api, expected_tenant)
    before = {section: _inventory_section(api, section) for section in SECTIONS}
    result = {"tenant": expected_tenant, "mode": "apply" if apply else "preview",
              "before": {k: len(v) for k, v in before.items()}, "errors": []}
    if not apply:
        return result
    if _identity(api, expected_tenant) != identity:
        raise ValueError("Dev identity changed after preview")
    stop = False
    for section in SECTIONS:
        for key in before[section]:
            try:
                api.request("DELETE", _object_route(section, key))
            except APIError as exc:
                if exc.status_code != 404:
                    result["errors"].append({"operation": "delete", "section": section, "key": key, "status": exc.status_code})
                if exc.status_code in (401, 403):
                    stop = True
                    break
        if stop:
            break
    if _identity(api, expected_tenant) != identity:
        raise ValueError("Dev identity changed during reset")
    confirmed = {}
    remaining = {}
    for section in SECTIONS:
        count = 0
        for key in before[section]:
            try:
                api.request("GET", _object_route(section, key))
            except APIError as exc:
                if exc.status_code == 404:
                    count += 1
                    continue
                outcome = exc.status_code
            else:
                outcome = "still retrievable"
            result["errors"].append({"operation": "readback", "section": section, "key": key, "status": outcome})
        confirmed[section] = count

    for section in SECTIONS:
        try:
            remaining[section] = len(_inventory_section(api, section))
        except (APIError, ValueError, OSError) as exc:
            remaining[section] = None
            status = exc.status_code if isinstance(exc, APIError) else str(exc)
            result["errors"].append({"operation": "final inventory", "section": section, "status": status})

    result["confirmed_404"] = confirmed
    result["remaining"] = remaining
    result["verified_empty"] = (confirmed == result["before"] and all(n == 0 for n in remaining.values())
                                and not result["errors"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="Exact expected dev tenant ID")
    parser.add_argument("--apply", action="store_true", help="Delete inventoried content and verify the result")
    args = parser.parse_args(argv)
    try:
        key = os.environ.get("PENFIELD_API_KEY")
        if key:
            result = reset_dev_content(PenfieldClient(DEV_ORIGIN, api_key=key), args.tenant, apply=args.apply)
        else:
            cache = Path.home() / ".config" / "penfield-dev-content-reset"
            with penfield_auth.oauth_profile("penfield-dev-content-reset", cache, "read delete offline_access"):
                grant = penfield_auth.get_valid_token(DEV_ORIGIN)

                def refresh():
                    new = penfield_auth.refresh_oauth_token(DEV_ORIGIN)
                    return (new.access_token, new.expires_in) if new else None

                api = PenfieldClient(DEV_ORIGIN, access_token=grant.access_token,
                                     token_expiry_seconds=grant.expires_in, on_token_refresh=refresh)
                result = reset_dev_content(api, args.tenant, apply=args.apply)
        print(json.dumps(result, indent=2))
        return int(args.apply and not result["verified_empty"])
    except (APIError, penfield_auth.AuthError, OSError, ValueError) as exc:
        print(f"Dev content reset stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
