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
* the file fence for ``attach_file`` / ``download_attachment`` and the
  no-overwrite write (the filename sanitiser is the SDK's).
"""
from __future__ import annotations

import datetime as _dt
import functools
import json
import logging
import os
import re
import sys
import tempfile
import threading
import uuid as _uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from meshbook import Client, ItemList, MeshbookError
from meshbook import config as mb_config
from meshbook import safe_filename as _sdk_safe_filename

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
_cache: dict[str, Any] = {"key": None, "client": None}


def _client_key() -> tuple[Any, ...]:
    path = mb_config.config_path()
    cfg = mb_config.load_config(path)
    return (
        str(path),
        json.dumps(cfg, sort_keys=True, default=str),
        tuple(os.environ.get(k) for k in _ENV_KEYS),
    )


def _new_client() -> Client:
    c = Client(user_agent=USER_AGENT)
    c.agent.kid_prefix = KID_PREFIX
    return c


def client() -> Client:
    """The process's SDK client (token cache, mesh-name cache and your own
    user id live on it).

    Rebuilt whenever the config file or a meshbook env var changes. Every
    tool passes ``mesh=`` to the SDK call for a per-call mesh (since 0.8.1
    that includes downloads, so no per-mesh client is kept any more).
    """
    key = _client_key()
    with _lock:
        if _cache["key"] != key:
            _cache.update(key=key, client=None)
        if _cache["client"] is None:
            _cache["client"] = _new_client()
        return _cache["client"]


def reset_client() -> None:
    """Forget the cached client (tests; also harmless at runtime)."""
    with _lock:
        _cache.update(key=None, client=None)


# ─── errors, written for a model ───────────────────────────────────────
#
# Since 0.8.1 the mapping reads only the SDK's stable machine channel: the
# error's ``code``, its ``hint`` (meshbook-sdk 0.4.1) and its ``status``.
# The SDK's ``message`` is for a person and its wording may change in any
# release, so it is never parsed: either it is passed on with a sentence
# appended, or (where it gives advice in Python, e.g. ``token=`` or
# ``force=True``) it is replaced whole. 0.8.0 split and searched message
# text, so an SDK rewording silently changed what a model was told.


class ToolError(MeshbookError):
    """An error this package raised itself, already worded for a model.

    :func:`hinted` passes it through untouched. A subclass, so callers that
    catch :class:`MeshbookError` see no difference.
    """


_OFFSET_HINT = (
    "Include a UTC offset, e.g. '2026-10-20T09:00:00+11:00' or '2026-10-19T22:00:00Z'. "
    "meshbook never guesses a time zone."
)


def _setup_tail() -> str:
    return (
        f"One-time setup: `pip install meshbook-cli` then `mesh login` (writes "
        f"{mb_config.config_path()}), or for a key-holding seat `mesh login --agent` or the "
        "register_agent tool. On a machine shared by several minds, set MESHBOOK_CONFIG_DIR "
        "in this server's environment."
    )


def _not_authenticated(e: MeshbookError) -> str:
    # status 0: refused locally, before the wire, for want of any credential
    # (the SDK's wording there is advice to a Python caller). Otherwise the
    # server (or the agent mint) rejected one, and what it said is worth keeping.
    if e.status == 0:
        return "No meshbook credentials: no token and no usable agent key. " + _setup_tail()
    return f"{e.message} {_setup_tail()}"


#: Codes whose SDK message is REPLACED (it speaks to a Python caller).
_REPLACE: dict[str, Callable[[MeshbookError], str]] = {
    "not_authenticated": _not_authenticated,
    "config_unreadable": lambda e: (
        f"The meshbook config file ({mb_config.config_path()}) exists but cannot be read as "
        "JSON. It was left untouched because it may still hold your token: fix it by hand, or "
        "move it aside and run `mesh login` to write a fresh one."
    ),
    "no_active_mesh": lambda e: (
        "No active mesh. Call set_active_mesh, or pass mesh=<name or id> to this tool "
        "(list_my_meshes shows your meshes); MESHBOOK_MESH in this server's environment "
        "also works."
    ),
    "no_agent_key": lambda e: (
        "auth_mode is 'agent' but there is no usable agent key or mint bundle beside the "
        "meshbook config. Run enroll_agent_credential (force=true to rebuild an existing "
        "key) or `mesh agent enroll`, or sign in with a bearer (`mesh login`)."
    ),
    "key_exists": lambda e: (
        "A local agent key already exists, so enrolling would replace it. The server keeps "
        "one key per seat: the old key stops working the instant the new one lands (it is "
        "kept locally as .bak). Call again with force=true only if that is what you mean."
    ),
    "pending_key": lambda e: (
        "An earlier enroll or register was interrupted and left an unconfirmed agent key "
        "beside the meshbook config. Tool calls try that key first and keep it if the server "
        "accepted it; agent_credential_status shows whether it matches. Until it is settled "
        "(meshbook-sdk: agent.recover_pending() or agent.discard_pending(); this server "
        "has no tool for that yet), a new enroll or register is refused."
    ),
    "duplicate_event": lambda e: (
        "An event with that title already starts at that time in this mesh (list_events "
        "shows it). Call create_event again with force=true to create it anyway."
    ),
    "missing_dependency": lambda e: (
        "The agent lane needs the `cryptography` package. Reinstall meshbook-mcp, or "
        "pip install 'meshbook-sdk[agent]'."
    ),
    "naive_datetime": lambda e: f"A date-time argument has no UTC offset. {_OFFSET_HINT}",
    "bad_datetime": lambda e: f"A date-time argument is not ISO 8601 with an offset. {_OFFSET_HINT}",
}

#: Codes whose SDK message is kept, with a sentence for a model appended.
_APPEND_BY_CODE: dict[str, str] = {
    "mesh_not_found": "list_my_meshes shows the meshes you can use.",
    "mesh_ambiguous": "Pass the mesh id instead (list_my_meshes).",
}

#: Otherwise, by the SDK's stable hint (meshbook.errors.HINT_MEANINGS).
_APPEND_BY_HINT: dict[str, str] = {
    "use_before_cursor": (
        "This tool pages with limit and before=<the oldest message's createdAt, as "
        "nextBefore gives it>; there is no offset."
    ),
    "check_before_retry": (
        "The write may already have been applied: check with a list or get tool before "
        "calling it again."
    ),
    "retry_later": "Try again in a moment.",
    "reauthenticate": (
        "The credential was rejected: sign in again (`mesh login`) or re-enroll the agent "
        "key, then retry."
    ),
    "choose_permitted_mesh": (
        "This credential is scoped to other meshes: pass mesh= with one it may use "
        "(list_my_meshes)."
    ),
    "allow_overwrite": "Pass overwrite=true to replace it, or choose another path.",
    "use_id": "Pass the id instead.",
    "use_username": "Pass the @username instead.",
    "create_in_web_app": "Create it in the meshbook web app first.",
    "fix_base_url": "Set MESHBOOK_BASE to the https apex (https://meshbook.org) or unset it.",
    # meshbook-sdk 0.4.2: a registry route answered with FastAPI's bare 404
    # (or a 405). The code stays http_error and the message "Not Found".
    "server_too_old": (
        "The meshbook server does not have this route: it is older than this client, so "
        "this is not a missing record. The tool cannot work until the server is updated; "
        "say so rather than retrying."
    ),
}

#: A per-tool replacement, by code (see ``tool(hints=...)`` in server.py).
Overrides = dict[str, Callable[[MeshbookError], str]]


def hinted(e: MeshbookError, overrides: Overrides | None = None) -> MeshbookError:
    """*e* re-worded for a model, by code and hint, or *e* itself.

    The result keeps *e*'s code, status and hint; only the message changes.
    """
    if isinstance(e, ToolError):
        return e
    fix = (overrides or {}).get(e.code) or _REPLACE.get(e.code)
    if fix is not None:
        message = fix(e)
    else:
        extra = _APPEND_BY_CODE.get(e.code) or _APPEND_BY_HINT.get(e.hint or "")
        if extra is None:
            return e
        message = f"{e.message.rstrip()} {extra}"
    return ToolError(e.code, message, e.status, hint=e.hint)


# ─── stderr: one line per expected error ───────────────────────────────
#
# On a stdio server stderr is the only log, and a client that does not
# drain it can stall once the pipe fills (field note 8, Alder 2026-10-19:
# 0.8.1 let FastMCP print every MeshbookError as a ~40-line rich traceback).
# A MeshbookError is an ordinary, classified outcome (a refused argument, a
# 404, a revoked token): it is logged here as ONE line and handed to FastMCP
# below its log level. Anything else is a bug, and FastMCP still logs its
# full traceback.

#: The logger the expected-error lines go to (configure it like any other).
log = logging.getLogger("meshbook_mcp")


class _StderrLine(logging.Handler):
    """Writes to whatever ``sys.stderr`` is at emit time (so a test's capture
    or a later redirect sees it), one record per line."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = sys.stderr
            if stream is None:
                return
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


if not log.handlers:
    _handler = _StderrLine()
    _handler.setFormatter(logging.Formatter("meshbook-mcp %(levelname)s: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def one_line(text: Any, limit: int = 500) -> str:
    """*text* with every run of whitespace (newlines included) collapsed to
    one space, cut at *limit* characters."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def log_expected(where: str, e: MeshbookError) -> None:
    """The one stderr line for an expected error in tool or resource *where*."""
    log.warning("%s: %s", where, one_line(f"{e.code} ({e.status}): {e.message}"))


def with_hints(fn: F, overrides: Overrides | None = None) -> F:
    """Re-raise any MeshbookError from *fn* with its model-facing wording."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except MeshbookError as e:
            better = hinted(e, overrides)
            if better is e:
                raise
            raise better from e

    return wrapper  # type: ignore[return-value]


# ─── listings ──────────────────────────────────────────────────────────


PAGING_MODES = ("offset", "before", "limit", "none")


def listing(
    items: Iterable[Any],
    *,
    rows: Sequence[Any] | None = None,
    paging: str = "offset",
    next_before: str | None = None,
    **extra: Any,
) -> dict:
    """The one envelope every list-returning tool answers with.

    Always, in this order (0.8.3: the same paging fields as meshbook-cli's
    ``--json`` envelope, Alder's re-test): ``count``, ``total``,
    ``truncated``, ``limit``, ``offset``, ``hasMore``, ``paging``; then
    ``nextOffset`` / ``nextBefore`` when there is a next page; then
    ``items``; then the tool's own *extra* keys.

    ``count`` is what is in ``items``; ``total`` is the server's TRUE total
    when the SDK says it is one (``ItemList.total_exact``), else ``None``;
    ``hasMore`` says whether another page can be fetched the way *paging*
    names; ``truncated`` says whether there is more than this answer for
    any reason (another page, or a server cap such as the 500-occurrence
    calendar window). ``limit`` is the page size (null when the call has
    none); ``offset`` is this page's offset under offset paging, else null.

    *paging* (the CLI's vocabulary) says how to continue:

    * ``"offset"``: ``nextOffset`` (the tool takes ``offset=``);
    * ``"before"``: ``nextBefore`` = *next_before*, a cursor (chat,
      channel and DM reads: their routes ignore an offset, and since
      meshbook-sdk 0.4.1 the SDK refuses one, so 0.8.0's ``nextOffset``
      there pointed at an argument that could never work);
    * ``"limit"``: no next page; only a larger ``limit`` reaches more
      (``search_chat``);
    * ``"none"``: the whole set in one answer (meshes, members, channels,
      exports); ``hasMore`` is false, ``truncated`` still tells the truth.

    B6 (Wren, 2026-08-20) made ``count`` explicit because a bare ``[]``
    serialises to nothing. It was the page length, though, so a model with
    80 contacts saw ``count: 50`` and could not tell it had been cut short.
    That is the bug this envelope closes.

    *rows* is the server page when *items* was filtered client-side (the
    paging maths needs the page as the server sent it).
    """
    if paging not in PAGING_MODES:
        raise ValueError(f"paging is one of {', '.join(PAGING_MODES)}, not {paging!r}")
    shown = list(items)
    src: Sequence[Any] = rows if rows is not None else shown
    page_len = len(src)
    if isinstance(src, ItemList):
        meta = src.meta
        offset = src.offset or 0
        limit = src.limit if src.limit is not None else meta.get("pageSize")
        # Whether the server's total is the full count is the SDK's call
        # (ItemList.total_exact, meshbook-sdk 0.4.1): true for the
        # 2026-10-19 ok_page shape and for a page the SDK cut itself. 0.8.0
        # guessed from which meta keys were present ("offset" alone was
        # enough), so a page-length total could be reported as the total.
        if src.total_exact:
            total = src.total
            more = total > src.next_offset
        elif src.total is None and limit is None:
            # A bare list for a call that asked for no paging: these rows
            # are all there is, and there is no server figure to distrust.
            total, more = page_len, False
        else:
            total = None
            more = src.has_more
        next_offset = src.next_offset  # the rows the SERVER sent, not len(shown)
        if isinstance(meta.get("hasMore"), bool):
            # The chat and channel routes say it outright (they peek one row
            # past the page); that beats any arithmetic on a page length.
            more = meta["hasMore"]
        truncated = bool(more or src.truncated)
    else:
        offset, limit, total, more, truncated = 0, None, page_len, False, False
        next_offset = page_len
    if paging == "before":
        more = bool(more and next_before)   # no time to page from: nothing to offer
    elif paging == "none":
        more = False                        # the whole set, in one answer
        limit = None                        # a server pageSize is not a limit asked for
    out: dict[str, Any] = {
        "count": len(shown),
        "total": total,
        "truncated": truncated,
        "limit": limit,
        "offset": offset if paging == "offset" else None,
        "hasMore": bool(more),
        "paging": paging,
    }
    if more and paging == "offset":
        out["nextOffset"] = next_offset
    elif more and paging == "before":
        out["nextBefore"] = next_before
    out["items"] = shown
    out.update(extra)
    return out


def page(limit: int | None, offset: int | None, *, maximum: int = 500) -> tuple[int | None, int | None]:
    """Validate limit/offset arguments; offset 0 is not sent."""
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ToolError("bad_argument", f"limit must be a positive integer, got {limit!r}.")
    if limit is not None and limit > maximum:
        raise ToolError("bad_argument", f"limit is at most {maximum}, got {limit}.")
    if offset is not None and (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0):
        raise ToolError("bad_argument", f"offset must be an integer >= 0, got {offset!r}.")
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
        raise ToolError("bad_argument", f"{what} must be an id (UUID), got {value!r}. {hint}")
    return v


def aware_iso(value: str, field: str) -> str:
    """An ISO 8601 date-time WITH an offset → normalised ISO string.

    Refused here, before the SDK, so the message talks to a model: a naive
    time is exactly the 09:00-Sydney-stored-as-20:00 bug the server packet
    found in the web page (CALENDAR-CONTRACT D6).
    """
    text = str(value or "").strip()
    if not text:
        raise ToolError("bad_argument", f"{field} is empty.")
    parse = text[:-1] + "+00:00" if text[-1] in "Zz" else text
    try:
        dt = _dt.datetime.fromisoformat(parse)
    except ValueError:
        raise ToolError(
            "bad_datetime", f"{field}={value!r} is not an ISO 8601 date-time. {_OFFSET_HINT}"
        ) from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ToolError("naive_datetime", f"{field}={value!r} has no UTC offset. {_OFFSET_HINT}")
    return dt.isoformat()


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def iso_date(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not _DATE.fullmatch(text):
        raise ToolError("bad_date", f"{field} must be a date YYYY-MM-DD, got {value!r}.")
    try:
        return _dt.date.fromisoformat(text).isoformat()
    except ValueError:
        raise ToolError("bad_date", f"{field}={value!r} is not a real date.") from None


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
        raise ToolError(
            "bad_argument", f"{field} must be one of {', '.join(choices)}, got {value!r}."
        )
    return hit


def clear_set(clear: Sequence[str] | None, allowed: Sequence[str]) -> set[str]:
    """Validate a tool's ``clear=[...]`` list (fields to set to null)."""
    out: set[str] = set()
    for name in clear or []:
        key = str(name).strip().lower()
        if key not in allowed:
            raise ToolError(
                "bad_argument",
                f"clear: {name!r} cannot be cleared here. Clearable: {', '.join(allowed)}.",
            )
        out.add(key)
    return out


def both_given(cleared: set[str], given: dict[str, Any]) -> None:
    clash = sorted(k for k in cleared if given.get(k) is not None)
    if clash:
        raise ToolError(
            "bad_argument", f"{', '.join(clash)}: pass a new value or clear it, not both."
        )


def user_ref(c: Client, ref: str | None) -> str | None:
    """``me`` / ``@me`` → your own user id; anything else passes through
    to the SDK's resolver (UUID, @username or display name)."""
    if ref is None:
        return None
    if ref.strip().lower() in ("me", "@me"):
        return c.me_id()
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
        raise ToolError("bad_argument", f"{purpose}: path is empty.")
    p = Path(text).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    resolved = p.resolve()
    cfg_dir = mb_config.config_dir().expanduser().resolve()
    if _within(resolved, cfg_dir):
        raise ToolError(
            "path_not_allowed",
            f"{purpose}: {resolved} is inside the meshbook config dir ({cfg_dir}), which holds "
            "your token and agent key. The file tools never touch it.",
        )
    if _truthy(os.environ.get(ALLOW_ANY_PATH_ENV)):
        return resolved
    roots = [Path.home().resolve(), Path.cwd().resolve()]
    if any(_within(resolved, r) for r in roots):
        return resolved
    raise ToolError(
        "path_not_allowed",
        f"{purpose}: {resolved} is outside your home directory and the current directory. "
        f"Use a path under one of those, or set {ALLOW_ANY_PATH_ENV}=1 in this server's "
        "environment to allow any path.",
    )


#: The ONE filename sanitiser: the SDK's (0.8.0 carried its own copy here).
#: Re-exported for callers of 0.8.0's ``_core.safe_filename``.
safe_filename = _sdk_safe_filename


def refuse_existing(target: Path, *, overwrite: bool) -> None:
    """``file_exists`` for a directory, or for an existing file unless *overwrite*."""
    if target.is_dir():
        raise ToolError("file_exists", f"{target} is a directory.")
    if os.path.lexists(target) and not overwrite:
        raise ToolError(
            "file_exists",
            f"{target} already exists. Pass overwrite=true to replace it, or choose another path.",
        )


def download_target(out: Path, filename: str, *, purpose: str) -> Path:
    """Where a download whose server name is *filename* lands, given the
    fenced *out* (a directory or a file path).

    For a directory, *filename* (already reduced by the SDK's
    ``safe_filename``) must resolve DIRECTLY inside it: a symlink planted
    under that name would otherwise carry the write anywhere the fence
    allows (``~/.bashrc``, with overwrite=true). The fence is applied to the
    resolved target too.
    """
    if not out.is_dir():
        return out
    target = fenced(str(out / filename), purpose=purpose)
    if target.parent != out:
        raise ToolError(
            "unsafe_path",
            f"{purpose}: {out / filename} resolves to {target}, outside {out} (a link?). "
            "Choose another directory or give a full file path in out_path.",
        )
    return target


def write_new(target: Path, content: bytes, *, overwrite: bool) -> None:
    """Write *content* to *target*; refuse an existing file unless *overwrite*.

    Written to a temp file in the same directory first, then linked into
    place (an atomic no-clobber where hard links work), so a refused or
    failed download never leaves a half-written file behind.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    refuse_existing(target, overwrite=overwrite)
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
            raise ToolError(
                "file_exists",
                f"{target} already exists. Pass overwrite=true to replace it.",
            ) from None
        except OSError:
            # No hard links here (FAT, some network shares): best effort.
            if target.exists():
                raise ToolError(
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
    "download_target",
    "due_value",
    "fenced",
    "Overrides",
    "ToolError",
    "hinted",
    "is_uuid",
    "iso_date",
    "listing",
    "log",
    "log_expected",
    "one_line",
    "page",
    "refuse_existing",
    "require_uuid",
    "reset_client",
    "safe_filename",
    "user_ref",
    "with_hints",
    "write_new",
]
