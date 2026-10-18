"""Shared plumbing for the meshbook MCP tools.

Everything that talks to meshbook.org goes through ``meshbook`` (the SDK):
config, HTTP, envelopes, errors, the agent lane and name resolution all
live there. What is left here is MCP-specific:

* one SDK client per process, rebuilt when the config file or the
  meshbook environment changes (so a ``mesh login`` or ``mesh agent
  enroll`` in a terminal reaches a running Claude session);
* error hints written for a model rather than a Python caller;
* the listing envelope (count, TRUE total, truncated);
* argument checks that must happen before the SDK sees a value (naive
  datetimes, ``clear=`` lists, vocabularies);
* the file fence for ``attach_file`` / ``download_attachment``.
"""
from __future__ import annotations

import datetime as _dt
import functools
import json
import os
import re
import tempfile
import threading
import uuid as _uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any, TypeVar

from meshbook import Client, ItemList, MeshbookError
from meshbook import config as mb_config

from meshbook_mcp import __version__

VERSION = __version__
USER_AGENT = f"meshbook-mcp/{VERSION}"
#: Forensic label on agent keys this server enrolls (the CLI uses mesh-agent-).
KID_PREFIX = "mcp-agent-"
#: Set to 1/true/yes to let the file tools touch paths outside home and cwd.
ALLOW_ANY_PATH_ENV = "MESHBOOK_MCP_ALLOW_ANY_PATH"

# Everything that changes which client we should be holding.
_ENV_KEYS = (
    "MESHBOOK_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "MESHBOOK_TOKEN",
    "MESHBOOK_BASE",
    "MESHBOOK_MESH",
)

F = TypeVar("F", bound=Callable[..., Any])


# ─── the SDK client ────────────────────────────────────────────────────

_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "client": None, "by_mesh": {}}


def _client_key() -> tuple[Any, ...]:
    path = mb_config.config_path()
    cfg = mb_config.load_config(path)
    return (
        str(path),
        json.dumps(cfg, sort_keys=True, default=str),
        tuple(os.environ.get(k) for k in _ENV_KEYS),
    )


def _new_client(mesh: str | None = None) -> Client:
    c = Client(mesh=mesh, user_agent=USER_AGENT)
    c.agent.kid_prefix = KID_PREFIX
    return c


def client(mesh: str | None = None) -> Client:
    """The process's SDK client (token cache and mesh-name cache live on it).

    Rebuilt whenever the config file or a meshbook env var changes. With
    *mesh*, a client whose OWN mesh is that one: only needed by the raw
    download path, which has no per-call ``mesh=``. Every other tool passes
    ``mesh=`` to the SDK call instead.
    """
    key = _client_key()
    with _lock:
        if _cache["key"] != key:
            _cache.update(key=key, client=None, by_mesh={})
        if mesh is None:
            if _cache["client"] is None:
                _cache["client"] = _new_client()
            return _cache["client"]
        by_mesh: dict[str, Client] = _cache["by_mesh"]
        if mesh not in by_mesh:
            by_mesh[mesh] = _new_client(mesh)
        return by_mesh[mesh]


def reset_client() -> None:
    """Forget the cached client (tests; also harmless at runtime)."""
    with _lock:
        _cache.update(key=None, client=None, by_mesh={})


# ─── errors, written for a model ───────────────────────────────────────

_OFFSET_HINT = (
    "Include a UTC offset, e.g. '2026-10-20T09:00:00+11:00' or '2026-10-19T22:00:00Z'. "
    "meshbook never guesses a time zone."
)


def _setup_hint(_e: MeshbookError) -> str:
    return (
        f"{_e.message} One-time setup: `pip install meshbook-cli` then `mesh login` "
        f"(writes {mb_config.config_path()}), or for a key-holding seat `mesh login --agent` "
        "or the register_agent tool. On a machine shared by several minds, set "
        "MESHBOOK_CONFIG_DIR in this server's environment."
    )


_HINTS: dict[str, str | Callable[[MeshbookError], str]] = {
    "not_authenticated": _setup_hint,
    "no_active_mesh": (
        " Call set_active_mesh, or pass mesh=<name or id> to this tool "
        "(list_my_meshes shows your meshes)."
    ),
    "mesh_not_found": " list_my_meshes shows the meshes you can use.",
    "naive_datetime": lambda e: (
        e.message
        if "Include a UTC offset" in e.message
        else e.message.split(" meshbook never guesses")[0].rstrip(". ") + ". " + _OFFSET_HINT
    ),
    "key_exists": lambda e: (
        "A local agent key already exists, so enrolling would replace it. The server keeps "
        "one key per seat: the old key stops working the instant the new one lands (it is "
        "kept locally as .bak). Call again with force=true only if that is what you mean."
    ),
    "duplicate_event": lambda e: (
        e.message.split(" Pass dedupe=False")[0]
        + " Call create_event again with force=true to create it anyway."
    ),
    "missing_dependency": lambda e: (
        "The agent lane needs the `cryptography` package. Reinstall meshbook-mcp, or "
        "pip install 'meshbook-sdk[agent]'."
    ),
}


def hinted(e: MeshbookError) -> MeshbookError:
    """*e* with a model-facing hint, or *e* itself when there is none."""
    hint = _HINTS.get(e.code)
    if hint is None:
        return e
    message = hint(e) if callable(hint) else e.message + hint
    return MeshbookError(e.code, message, e.status)


def with_hints(fn: F) -> F:
    """Re-raise any MeshbookError from *fn* with its model-facing hint."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except MeshbookError as e:
            better = hinted(e)
            if better is e:
                raise
            raise better from e

    return wrapper  # type: ignore[return-value]


# ─── listings ──────────────────────────────────────────────────────────


def listing(items: Iterable[Any], *, rows: Sequence[Any] | None = None, **extra: Any) -> dict:
    """The one envelope every list-returning tool answers with.

    ``count`` is what is in ``items``; ``total`` is the server's TRUE total
    when the server reports one it can be trusted with (``None`` when it
    only told us the page length); ``truncated`` says whether there is more
    than this page, and ``nextOffset`` where to continue.

    B6 (Wren, 2026-08-20) made ``count`` explicit because a bare ``[]``
    serialises to nothing. It was the page length, though, so a model with
    80 contacts saw ``count: 50`` and could not tell it had been cut short.
    That is the bug this envelope closes.

    *rows* is the server page when *items* was filtered client-side (the
    paging maths needs the page as the server sent it).
    """
    shown = list(items)
    src: Sequence[Any] = rows if rows is not None else shown
    page_len = len(src)
    if isinstance(src, ItemList):
        meta = src.meta
        offset = src.offset or 0
        limit = src.limit if src.limit is not None else meta.get("pageSize")
        total = src.total
        # The 2026-10-19 server sends limit/offset beside a TRUE total; an
        # older one sends only page/pageSize and reports the PAGE LENGTH as
        # total. The SDK's client-side cut also yields a real total.
        trusted = total is not None and (
            "offset" in meta or "limit" in meta or "client_paginated" in meta or limit is None
        )
        if total is None:
            more = src.has_more if limit is not None else False
            total = page_len if limit is None else None
        elif trusted:
            more = total > offset + page_len
        else:
            more = total > offset + page_len or (limit is not None and page_len >= limit)
            total = None
        truncated = bool(more or src.truncated)
    else:
        offset, limit, total, more, truncated = 0, None, page_len, False, False
    out: dict[str, Any] = {"count": len(shown), "total": total, "truncated": truncated}
    if limit is not None:
        out["limit"] = limit
    if offset:
        out["offset"] = offset
    if more:
        out["nextOffset"] = offset + page_len
    out["items"] = shown
    out.update(extra)
    return out


def page(limit: int | None, offset: int | None, *, maximum: int = 500) -> tuple[int | None, int | None]:
    """Validate limit/offset arguments; offset 0 is not sent."""
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise MeshbookError("bad_argument", f"limit must be a positive integer, got {limit!r}.")
    if limit is not None and limit > maximum:
        raise MeshbookError("bad_argument", f"limit is at most {maximum}, got {limit}.")
    if offset is not None and (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0):
        raise MeshbookError("bad_argument", f"offset must be an integer >= 0, got {offset!r}.")
    return limit, (offset or None)


# ─── argument checks ───────────────────────────────────────────────────


def is_uuid(value: Any) -> bool:
    try:
        _uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def require_uuid(value: str, what: str, hint: str) -> str:
    """Destructive tools act on ids only: a name search that happens to
    return one row must never be enough to delete something."""
    v = (value or "").strip()
    if not is_uuid(v):
        raise MeshbookError("bad_argument", f"{what} must be an id (UUID), got {value!r}. {hint}")
    return v


def aware_iso(value: str, field: str) -> str:
    """An ISO 8601 date-time WITH an offset → normalised ISO string.

    Refused here, before the SDK, so the message talks to a model: a naive
    time is exactly the 09:00-Sydney-stored-as-20:00 bug the server packet
    found in the web page (CALENDAR-CONTRACT D6).
    """
    text = str(value or "").strip()
    if not text:
        raise MeshbookError("bad_argument", f"{field} is empty.")
    parse = text[:-1] + "+00:00" if text[-1] in "Zz" else text
    try:
        dt = _dt.datetime.fromisoformat(parse)
    except ValueError:
        raise MeshbookError(
            "bad_datetime", f"{field}={value!r} is not an ISO 8601 date-time. {_OFFSET_HINT}"
        ) from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise MeshbookError("naive_datetime", f"{field}={value!r} has no UTC offset. {_OFFSET_HINT}")
    return dt.isoformat()


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def iso_date(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not _DATE.fullmatch(text):
        raise MeshbookError("bad_date", f"{field} must be a date YYYY-MM-DD, got {value!r}.")
    try:
        return _dt.date.fromisoformat(text).isoformat()
    except ValueError:
        raise MeshbookError("bad_date", f"{field}={value!r} is not a real date.") from None


def due_value(value: str) -> str:
    """A task due: ``YYYY-MM-DD`` (a day) or a date-time with an offset."""
    text = str(value or "").strip()
    if _DATE.fullmatch(text):
        return iso_date(text, "due")
    return aware_iso(text, "due")


def canon(value: str | None, choices: Sequence[str], field: str) -> str | None:
    """Case/space/underscore-insensitive match into a server vocabulary."""
    if value is None:
        return None

    def fold(s: str) -> str:
        return re.sub(r"[\s_-]+", "", s).casefold()

    hit = {fold(c): c for c in choices}.get(fold(str(value)))
    if hit is None:
        raise MeshbookError(
            "bad_argument", f"{field} must be one of {', '.join(choices)}, got {value!r}."
        )
    return hit


def clear_set(clear: Sequence[str] | None, allowed: Sequence[str]) -> set[str]:
    """Validate a tool's ``clear=[...]`` list (fields to set to null)."""
    out: set[str] = set()
    for name in clear or []:
        key = str(name).strip().lower()
        if key not in allowed:
            raise MeshbookError(
                "bad_argument",
                f"clear: {name!r} cannot be cleared here. Clearable: {', '.join(allowed)}.",
            )
        out.add(key)
    return out


def both_given(cleared: set[str], given: dict[str, Any]) -> None:
    clash = sorted(k for k in cleared if given.get(k) is not None)
    if clash:
        raise MeshbookError(
            "bad_argument", f"{', '.join(clash)}: pass a new value or clear it, not both."
        )


def user_ref(c: Client, ref: str | None) -> str | None:
    """``me`` / ``@me`` → your own user id; anything else passes through
    to the SDK's resolver (UUID, @username or display name)."""
    if ref is None:
        return None
    if ref.strip().lower() in ("me", "@me"):
        return c.whoami().id
    return ref


# ─── the file fence ────────────────────────────────────────────────────


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def fenced(path_str: str, *, purpose: str) -> Path:
    """Resolve a path the model handed us and refuse the dangerous ones.

    * Never inside the meshbook config dir: it holds the bearer token and
      the agent private key, and ``attach_file`` would upload them to the
      server (``download_attachment`` could overwrite them). No opt-out.
    * Otherwise inside the user's home or the current directory, unless
      ``MESHBOOK_MCP_ALLOW_ANY_PATH=1``.

    Stdio with a trusted local client is the only transport; this fence is
    the first half of what an HTTP transport would need (README).
    """
    text = str(path_str or "").strip()
    if not text:
        raise MeshbookError("bad_argument", f"{purpose}: path is empty.")
    p = Path(text).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    resolved = p.resolve()
    cfg_dir = mb_config.config_dir().expanduser().resolve()
    if _within(resolved, cfg_dir):
        raise MeshbookError(
            "path_not_allowed",
            f"{purpose}: {resolved} is inside the meshbook config dir ({cfg_dir}), which holds "
            "your token and agent key. The file tools never touch it.",
        )
    if _truthy(os.environ.get(ALLOW_ANY_PATH_ENV)):
        return resolved
    roots = [Path.home().resolve(), Path.cwd().resolve()]
    if any(_within(resolved, r) for r in roots):
        return resolved
    raise MeshbookError(
        "path_not_allowed",
        f"{purpose}: {resolved} is outside your home directory and the current directory. "
        f"Use a path under one of those, or set {ALLOW_ANY_PATH_ENV}=1 in this server's "
        "environment to allow any path.",
    )


_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')


def safe_filename(name: str | None) -> str:
    """A server-supplied filename reduced to a bare, harmless basename.

    Content-Disposition is uploader-controlled: ``../../.bashrc`` or
    ``C:evil`` must never steer where a download lands.
    """
    raw = str(name or "")
    base = PureWindowsPath(raw.replace("\\", "/").split("/")[-1]).name
    base = _UNSAFE.sub("_", base).strip().strip(".")
    return base or "attachment"


def write_new(target: Path, content: bytes, *, overwrite: bool) -> None:
    """Write *content* to *target*; refuse an existing file unless *overwrite*.

    Written to a temp file in the same directory first, then linked into
    place (an atomic no-clobber where hard links work), so a refused or
    failed download never leaves a half-written file behind.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        raise MeshbookError("file_exists", f"{target} is a directory.")
    if target.exists() and not overwrite:
        raise MeshbookError(
            "file_exists",
            f"{target} already exists. Pass overwrite=true to replace it, or choose another path.",
        )
    fd, tmp_name = tempfile.mkstemp(prefix=".meshbook-dl-", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        if overwrite:
            os.replace(tmp, target)
            return
        try:
            os.link(tmp, target)
        except FileExistsError:
            raise MeshbookError(
                "file_exists",
                f"{target} already exists. Pass overwrite=true to replace it.",
            ) from None
        except OSError:
            # No hard links here (FAT, some network shares): best effort.
            if target.exists():
                raise MeshbookError(
                    "file_exists", f"{target} already exists. Pass overwrite=true to replace it."
                ) from None
            os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


__all__ = [
    "ALLOW_ANY_PATH_ENV",
    "KID_PREFIX",
    "USER_AGENT",
    "VERSION",
    "aware_iso",
    "both_given",
    "canon",
    "clear_set",
    "client",
    "due_value",
    "fenced",
    "hinted",
    "is_uuid",
    "iso_date",
    "listing",
    "page",
    "require_uuid",
    "reset_client",
    "safe_filename",
    "user_ref",
    "with_hints",
    "write_new",
]
