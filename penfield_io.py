"""Shared transport and durable local state for the importer."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


def origin(url: str) -> str:
    parts = urlsplit(url)
    if (not parts.hostname or parts.username is not None or parts.password is not None
            or parts.fragment or parts.scheme not in {"https", "http"}):
        raise ValueError("Expected an absolute HTTPS URL without embedded credentials")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("HTTP is supported only on loopback")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}"


class _SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(req.full_url) != origin(newurl):
            raise HTTPError(req.full_url, code, "Cross-origin redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_request(request, timeout):
    origin(request.full_url)
    return build_opener(_SameOriginRedirect()).open(request, timeout=timeout)


@contextmanager
def checkpoint_lock(path: Path):
    """Keep a persistent lock inode. The OS releases the lock on process exit."""
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError(f"State is busy: {path}") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(f"State is busy: {path}") from exc
        yield


def write_json_atomic(path: Path, data: dict) -> None:
    """Replace state only after the new owner-only file is flushed to disk."""
    tmp = NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False)
    name = Path(tmp.name)
    try:
        with tmp:
            json.dump(data, tmp, ensure_ascii=True, separators=(",", ":"))
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(name, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        name.unlink(missing_ok=True)
