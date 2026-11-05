#!/usr/bin/env python3
"""Create and maintain a source-code learning workspace."""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 3
CHAIN_MARKER_KIND = "source-code-learning-chain"
CHAIN_MARKER_VERSION = 1
TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "assets" / "templates"
TEMPLATE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
TEMPLATE_TOKEN_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
TEMPLATE_TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
ARTIFACT_BLOCK_START = "<!-- learning-coach:artifacts:start -->"
ARTIFACT_BLOCK_END = "<!-- learning-coach:artifacts:end -->"


class WorkspaceError(Exception):
    """An expected, user-facing workspace error."""


def template_read_seam(root_fd: int, name: str, template_fd: int) -> None:
    """Test seam after opening a template and before verifying entry identity."""


def _validate_template_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not TEMPLATE_NAME.fullmatch(name)
        or name in {".", ".."}
        or Path(name).name != name
    ):
        raise WorkspaceError("template filename must be one fixed safe component")
    return name


def _require_template_fd_support() -> None:
    if (
        os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
    ):
        raise WorkspaceError(
            "secure template loading requires directory-fd and no-follow support"
        )


def _read_template(name: str) -> str:
    """Read one regular template by stable identity beneath TEMPLATE_ROOT."""
    name = _validate_template_name(name)
    _require_template_fd_support()
    configured_root = Path(TEMPLATE_ROOT).expanduser()
    try:
        configured_metadata = os.stat(configured_root, follow_symlinks=False)
        canonical_root = configured_root.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"cannot access template root {configured_root}: {exc}") from exc
    if not stat.S_ISDIR(configured_metadata.st_mode) or configured_root.is_symlink():
        raise WorkspaceError("template root must be a real directory, not a symlink")
    try:
        canonical_metadata = os.stat(canonical_root, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceError(f"cannot access canonical template root: {exc}") from exc
    if not stat.S_ISDIR(canonical_metadata.st_mode):
        raise WorkspaceError("canonical template root is not a directory")

    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    template_flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        root_fd = os.open(canonical_root, root_flags)
    except OSError as exc:
        raise WorkspaceError(f"cannot securely open template root: {exc}") from exc
    template_fd = -1
    try:
        if not os.path.samestat(os.fstat(root_fd), canonical_metadata):
            raise WorkspaceError("template root identity changed during secure open")
        try:
            template_fd = os.open(name, template_flags, dir_fd=root_fd)
        except OSError as exc:
            raise WorkspaceError(f"cannot securely open template {name}: {exc}") from exc
        template_read_seam(root_fd, name, template_fd)
        try:
            entry_metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceError(f"template entry identity changed: {name}") from exc
        opened_metadata = os.fstat(template_fd)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise WorkspaceError(f"template must be a regular file: {name}")
        if not os.path.samestat(opened_metadata, entry_metadata):
            raise WorkspaceError(f"template entry identity changed: {name}")
        try:
            with os.fdopen(template_fd, "r", encoding="utf-8") as handle:
                template_fd = -1
                return handle.read()
        except (OSError, UnicodeError) as exc:
            raise WorkspaceError(f"cannot read template {name}: {exc}") from exc
    finally:
        if template_fd >= 0:
            os.close(template_fd)
        os.close(root_fd)


def _template_tokens(content: str) -> list[str]:
    """Validate {{UPPER_SNAKE_CASE}} delimiters in the original template."""
    tokens: list[str] = []
    index = 0
    while index < len(content):
        if content.startswith("{{", index):
            close = content.find("}}", index + 2)
            if close < 0:
                raise WorkspaceError(
                    "malformed template token; expected {{UPPER_SNAKE_CASE}}"
                )
            token = content[index + 2 : close]
            if not TEMPLATE_TOKEN_NAME.fullmatch(token):
                raise WorkspaceError(
                    "malformed template token; expected {{UPPER_SNAKE_CASE}}"
                )
            tokens.append(token)
            index = close + 2
            continue
        if content.startswith("}}", index):
            raise WorkspaceError(
                "malformed template token; expected {{UPPER_SNAKE_CASE}}"
            )
        index += 1
    return tokens


def render_template(name: str, replacements: dict[str, Any]) -> str:
    """Securely read and single-pass render a controlled token template."""
    content = _read_template(name)
    tokens = _template_tokens(content)
    unknown = sorted({token for token in tokens if token not in replacements})
    if unknown:
        raise WorkspaceError(
            "unknown template token(s): " + ", ".join(unknown)
        )
    return TEMPLATE_TOKEN.sub(
        lambda match: str(replacements[match.group(1)]), content
    )


def render_named_template(name: str, replacements: dict[str, Any]) -> str:
    return render_template(name, replacements)


def materialize_template(
    template_name: str, output_path: Path, replacements: dict[str, Any]
) -> None:
    """Render completely before atomically creating or replacing an output file."""
    rendered = render_template(template_name, replacements)
    atomic_write_text(Path(output_path), rendered)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceError(f"JSON root must be an object: {path}")
    return data


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: dict) -> None:
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, content)


def commit_state(root: Path, data: dict) -> None:
    atomic_write_json(root / "学习状态.json", data)
    atomic_write_text(root / "学习状态.md", render_state(data))


def replace_authority_json(path: Path, content: str) -> None:
    """Atomically replace the authoritative JSON state (transaction seam)."""
    atomic_write_text(path, content)


def replace_projection_markdown(path: Path, content: str) -> None:
    """Atomically replace the derived Markdown projection (failure seam)."""
    atomic_write_text(path, content)


def artifact_pre_authority_seam(
    root: Path, state: dict, owner_markdown: Path, artifact: Path
) -> None:
    """Test seam after candidate preparation and before authority replacement."""


def artifact_post_authority_seam(
    root: Path, state: dict, owner_markdown: Path, artifact: Path
) -> None:
    """Test seam after authority replacement and before owner projection."""


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _entry_matches_metadata(
    directory_fd: int, name: str, expected: os.stat_result, *, regular: bool = False
) -> bool:
    try:
        actual = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    if regular and not stat.S_ISREG(actual.st_mode):
        return False
    return os.path.samestat(actual, expected)


def owner_projection_exchange_seam(
    directory_fd: int,
    owner_name: str,
    temporary_name: str,
    original_fd: int,
    candidate_fd: int,
) -> None:
    """Test seam immediately before the atomic projection exchange."""


def _exchange_entries_at(directory_fd: int, first_name: str, second_name: str) -> None:
    """Atomically exchange two directory entries, or fail closed."""
    libc = ctypes.CDLL(None, use_errno=True)
    first = os.fsencode(first_name)
    second = os.fsencode(second_name)
    if sys.platform == "darwin":
        try:
            exchange = libc.renameatx_np
        except AttributeError as exc:
            raise WorkspaceError("atomic projection exchange is unavailable") from exc
        exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        exchange.restype = ctypes.c_int
        result = exchange(
            directory_fd, first, directory_fd, second, 0x00000002
        )  # RENAME_SWAP
    elif sys.platform.startswith("linux"):
        try:
            exchange = libc.renameat2
        except AttributeError as exc:
            raise WorkspaceError("atomic projection exchange is unavailable") from exc
        exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        exchange.restype = ctypes.c_int
        result = exchange(
            directory_fd, first, directory_fd, second, 0x00000002
        )  # RENAME_EXCHANGE
    else:
        raise WorkspaceError("atomic projection exchange is unavailable")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
        raise WorkspaceError("atomic projection exchange is unavailable")
    raise OSError(error_number, os.strerror(error_number), first_name)


def _metadata_at(directory_fd: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceError(f"projection entry is missing: {name}") from exc


def _unlink_if_identity_matches(
    directory_fd: int, name: str, expected: os.stat_result
) -> bool:
    try:
        actual = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not os.path.samestat(actual, expected):
        return False
    try:
        os.unlink(name, dir_fd=directory_fd)
    except OSError:
        return False
    return True


def _rollback_projection_exchange(
    directory_fd: int,
    owner_name: str,
    temporary_name: str,
    exchanged_owner: os.stat_result,
    exchanged_temporary: os.stat_result,
) -> None:
    if not _entry_matches_metadata(directory_fd, owner_name, exchanged_owner) or not (
        _entry_matches_metadata(
            directory_fd, temporary_name, exchanged_temporary
        )
    ):
        raise WorkspaceError(
            "projection rollback refused because exchanged entries changed"
        )
    _exchange_entries_at(directory_fd, owner_name, temporary_name)
    if not _entry_matches_metadata(
        directory_fd, owner_name, exchanged_temporary
    ) or not _entry_matches_metadata(
        directory_fd, temporary_name, exchanged_owner
    ):
        raise WorkspaceError("projection rollback did not restore entry identities")
    os.fsync(directory_fd)


def replace_owner_markdown(
    directory_fd: int,
    name: str,
    content: str,
    expected_metadata: os.stat_result,
    expected_content: str,
) -> None:
    """Publish by atomic exchange, validating both stable file identities."""
    if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        raise WorkspaceError(
            "secure artifact projection requires directory-fd operations"
        )
    temporary_name = f".{name}.tmp-{uuid.uuid4().hex}"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    candidate_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
    original_fd = -1
    candidate_metadata = os.fstat(candidate_fd)
    original_metadata: os.stat_result | None = None
    exchanged = False
    try:
        _write_all(candidate_fd, content.encode("utf-8"))
        os.fsync(candidate_fd)
        if not _entry_matches_metadata(
            directory_fd, name, expected_metadata, regular=True
        ):
            raise WorkspaceError(
                "owner Markdown identity changed before projection replacement"
            )
        original_fd = os.open(
            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd
        )
        original_metadata = os.fstat(original_fd)
        if (
            not os.path.samestat(original_metadata, expected_metadata)
            or _read_utf8_fd(original_fd, "owner Markdown") != expected_content
            or _read_utf8_fd(candidate_fd, "candidate Markdown") != content
        ):
            raise WorkspaceError(
                "owner Markdown content changed before projection exchange"
            )
        os.fchmod(candidate_fd, stat.S_IMODE(original_metadata.st_mode))
        owner_projection_exchange_seam(
            directory_fd,
            name,
            temporary_name,
            original_fd,
            candidate_fd,
        )
        _exchange_entries_at(directory_fd, name, temporary_name)
        exchanged = True
        exchanged_owner = _metadata_at(directory_fd, name)
        exchanged_temporary = _metadata_at(directory_fd, temporary_name)
        valid_exchange = (
            stat.S_ISREG(exchanged_owner.st_mode)
            and stat.S_ISREG(exchanged_temporary.st_mode)
            and os.path.samestat(exchanged_owner, candidate_metadata)
            and os.path.samestat(exchanged_temporary, original_metadata)
            and _read_utf8_fd(original_fd, "owner Markdown") == expected_content
            and _read_utf8_fd(candidate_fd, "candidate Markdown") == content
        )
        if not valid_exchange:
            _rollback_projection_exchange(
                directory_fd,
                name,
                temporary_name,
                exchanged_owner,
                exchanged_temporary,
            )
            exchanged = False
            if _entry_matches_metadata(
                directory_fd, temporary_name, candidate_metadata
            ):
                if not _unlink_if_identity_matches(
                    directory_fd, temporary_name, candidate_metadata
                ):
                    raise WorkspaceError(
                        "projection rollback restored content but candidate cleanup failed"
                    )
            raise WorkspaceError(
                "owner Markdown changed during atomic projection exchange"
            )
        if not _unlink_if_identity_matches(
            directory_fd, temporary_name, original_metadata
        ):
            raise WorkspaceError(
                "projection was published but swapped owner cleanup failed"
            )
        os.fsync(directory_fd)
    except BaseException:
        if not exchanged:
            _unlink_if_identity_matches(
                directory_fd, temporary_name, candidate_metadata
            )
        raise
    finally:
        if original_fd >= 0:
            os.close(original_fd)
        os.close(candidate_fd)


def replace_managed_artifact_block(markdown: str, entries: list[str]) -> str:
    """Replace exactly one generated artifact block, preserving all other text."""
    if not isinstance(markdown, str) or any(
        not isinstance(entry, str) for entry in entries
    ):
        raise WorkspaceError("artifact projection must be UTF-8 text")
    if any(
        "\n" in entry
        or "\r" in entry
        or ARTIFACT_BLOCK_START in entry
        or ARTIFACT_BLOCK_END in entry
        for entry in entries
    ):
        raise WorkspaceError("artifact projection entry contains unsafe marker text")
    start_pattern = re.compile(
        rf"(?m)^{re.escape(ARTIFACT_BLOCK_START)}(?P<newline>\r?\n|$)"
    )
    end_pattern = re.compile(
        rf"(?m)^{re.escape(ARTIFACT_BLOCK_END)}(?:\r?\n|$)"
    )
    start_matches = list(start_pattern.finditer(markdown))
    end_matches = list(end_pattern.finditer(markdown))
    if (
        markdown.count(ARTIFACT_BLOCK_START) != 1
        or markdown.count(ARTIFACT_BLOCK_END) != 1
        or len(start_matches) != 1
        or len(end_matches) != 1
    ):
        raise WorkspaceError(
            "owner Markdown must contain exactly one managed artifact block"
        )
    start_match = start_matches[0]
    end_match = end_matches[0]
    if end_match.start() < start_match.end():
        raise WorkspaceError("managed artifact block markers are out of order")
    newline = start_match.group("newline")
    if newline not in {"\n", "\r\n"}:
        raise WorkspaceError("managed artifact start marker must end with LF or CRLF")
    body = newline.join(entries) + newline
    return markdown[: start_match.end()] + body + markdown[end_match.start() :]


def sanitize_component(value: str) -> str:
    if not isinstance(value, str):
        return ""
    sanitized = value.strip()
    sanitized = "".join(
        "-" if character in "/\\" or unicodedata.category(character) == "Cc" else character
        for character in sanitized
    )
    return sanitized.strip(". ")


def _validate_custom_project_name(value: str) -> str:
    if not isinstance(value, str):
        raise WorkspaceError("project name must be text")
    if (
        value in {".", ".."}
        or Path(value).is_absolute()
        or ntpath.isabs(value)
        or ntpath.splitdrive(value)[0]
        or "/" in value
        or "\\" in value
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise WorkspaceError("unsafe project name")
    sanitized = sanitize_component(value)
    if not sanitized or sanitized != value:
        raise WorkspaceError("unsafe project name")
    return value


def validate_roadmap(data: dict) -> dict:
    if not isinstance(data, dict):
        raise WorkspaceError("roadmap must be a JSON object")
    normalized = copy.deepcopy(data)

    title = normalized.get("title")
    if not isinstance(title, str) or not title.strip():
        raise WorkspaceError("roadmap title must be non-empty text")

    days = normalized.get("days")
    if not isinstance(days, list) or not days:
        raise WorkspaceError("roadmap days must be a non-empty list")

    checkpoint_ids: set[str] = set()
    for expected_day, day in enumerate(days, start=1):
        if not isinstance(day, dict) or type(day.get("day")) is not int:
            raise WorkspaceError("each Day must have an integer day number")
        if day["day"] != expected_day:
            raise WorkspaceError("Day numbers must be consecutive and start at 1")
        if not isinstance(day.get("title"), str) or not day["title"].strip():
            raise WorkspaceError(f"Day {expected_day} title must be non-empty text")
        checkpoints = day.get("checkpoints")
        if not isinstance(checkpoints, list) or not checkpoints:
            raise WorkspaceError(f"Day {expected_day} checkpoints must be non-empty")

        total_weight = 0
        for checkpoint in checkpoints:
            if not isinstance(checkpoint, dict):
                raise WorkspaceError("each checkpoint must be an object")
            checkpoint_id = checkpoint.get("id")
            if not isinstance(checkpoint_id, str) or not checkpoint_id.strip():
                raise WorkspaceError("checkpoint ID must be non-empty text")
            if checkpoint_id in checkpoint_ids:
                raise WorkspaceError(f"duplicate checkpoint ID: {checkpoint_id}")
            checkpoint_ids.add(checkpoint_id)
            if not isinstance(checkpoint.get("title"), str) or not checkpoint["title"].strip():
                raise WorkspaceError(f"checkpoint {checkpoint_id} title must be non-empty text")
            weight = checkpoint.get("weight")
            if type(weight) is not int or weight <= 0:
                raise WorkspaceError(f"checkpoint {checkpoint_id} weight must be a positive integer")
            total_weight += weight
        if total_weight != 100:
            raise WorkspaceError(f"Day {expected_day} checkpoint weights must total 100")

    return normalized


def render_roadmap(data: dict, project_name: str, repository_identity: str) -> str:
    day_sections: list[str] = []
    for day in data["days"]:
        lines = [f"## Day {day['day']}｜{day['title']}", ""]
        for checkpoint in day["checkpoints"]:
            lines.append(
                f"- `{checkpoint['id']}`：{checkpoint['title']}（{checkpoint['weight']}%）"
            )
        day_sections.append("\n".join(lines))
    return render_named_template(
        "learning-roadmap.md",
        {
            "ROUTE_TITLE": data["title"],
            "PROJECT_NAME": project_name,
            "REPOSITORY_IDENTITY": repository_identity,
            "DAYS": "\n\n".join(day_sections),
        },
    )


def render_state(state: dict) -> str:
    current = state["current"]
    day_text = "未开始" if current["day"] is None else f"Day {current['day']:02d}"
    checkpoint_lines: list[str] = []
    for day in state["days"]:
        checkpoint_lines.append(f"### Day {day['day']:02d}｜{day['title']}")
        checkpoint_lines.append("")
        for checkpoint in day["checkpoints"]:
            checkpoint_lines.append(
                f"- [{checkpoint['status']}] `{checkpoint['id']}` "
                f"{checkpoint['title']}（{checkpoint['weight']}%）"
            )
        checkpoint_lines.append("")
    progress = current.get("progress", 0)
    active_chain = current.get("chain")
    active_chain_text = (
        active_chain.get("path", "未开始") if active_chain else "未开始"
    )
    sealed = [entry["path"] for entry in state.get("chains", []) if entry.get("status") == "sealed"]
    active_day = next(
        (day for day in state["days"] if day.get("status") == "active"), None
    )
    if active_day is not None:
        current_status = "active"
    elif all(day.get("status") == "completed" for day in state["days"]):
        current_status = "completed"
    else:
        current_status = "pending"
    return render_named_template(
        "learning-state.md",
        {
            "ROUTE_TITLE": state["route_title"],
            "LEARNING_ROOT_ID": state["learning_root_id"],
            "CURRENT_DAY": day_text,
            "CURRENT_STATUS": current_status,
            "CURRENT_PROGRESS": progress,
            "ACTIVE_STAGE": active_chain_text,
            "SEALED_STAGES": "、".join(sealed) if sealed else "无",
            "LAST_SOURCE": current.get("last_source") or "无",
            "RESUME_MARKER": current.get("resume_marker") or "无",
            "CLOSED_FLOWS": "、".join(current.get("closed_flows", [])) or "无",
            "UPDATED_AT": state["updated_at"],
            "CHECKPOINTS": "\n".join(checkpoint_lines).rstrip(),
        },
    )


def _rename_no_replace(temporary_root: Path, final_root: Path) -> None:
    """Publish without replacement; unsupported platforms fail closed."""
    if sys.platform == "win32":
        # MoveFileExW with dwFlags=0 never replaces an existing destination.
        # The temp and final directories share a parent, so no cross-volume
        # copy fallback is needed or permitted.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int
        result = move(str(temporary_root), str(final_root), 0)
        if result:
            return
        error_number = kernel32.GetLastError()
        if error_number in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            raise WorkspaceError(f"learning workspace already exists: {final_root}")
        raise OSError(error_number, os.strerror(error_number), str(final_root))

    if sys.platform not in {"darwin", "linux"} and not sys.platform.startswith("linux"):
        raise WorkspaceError(
            "atomic no-clobber publication is unavailable on this platform; "
            "refusing an unsafe fallback"
        )

    source = os.fsencode(temporary_root)
    destination = os.fsencode(final_root)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as exc:
            raise WorkspaceError("atomic no-clobber publication is unavailable") from exc
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source, destination, 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise WorkspaceError("atomic no-clobber publication is unavailable") from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, source, -100, destination, 1)  # AT_FDCWD, RENAME_NOREPLACE
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise WorkspaceError(f"learning workspace already exists: {final_root}")
    if error_number in {errno.ENOSYS, errno.ENOTSUP}:
        raise WorkspaceError("atomic no-clobber publication is unavailable")
    raise OSError(error_number, os.strerror(error_number), str(final_root))


def finalize_workspace(temporary_root: Path, final_root: Path) -> None:
    _rename_no_replace(temporary_root, final_root)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _initial_state(
    normalized: dict, repository: Path, learning_root: Path, learning_root_id: str
) -> dict:
    days: list[dict[str, Any]] = []
    for source_day in normalized["days"]:
        day = copy.deepcopy(source_day)
        day["status"] = "pending"
        day["progress"] = 0
        for checkpoint in day["checkpoints"]:
            checkpoint["status"] = "pending"
        days.append(day)
    return {
        "schema_version": SCHEMA_VERSION,
        "learning_root_id": learning_root_id,
        "learning_root": str(learning_root.resolve(strict=False)),
        "repository": str(repository),
        "route_title": normalized["title"],
        "route_days": len(days),
        "days": days,
        "current": {
            "day": None,
            "chain": None,
            "progress": 0,
            "last_source": None,
            "resume_marker": "",
            "closed_flows": [],
        },
        "chains": [],
        "artifacts": [],
        "artifacts": [],
        "history": [],
        "updated_at": _utc_timestamp(),
    }


def _state_text(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2) + "\n"


def _day_directory(root: Path, state: dict, day: int) -> Path:
    width = max(2, len(str(state["route_days"])))
    return root / f"Day{day:0{width}d}"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def resolved_under(path: Path, root: Path) -> Path:
    """Resolve an existing path and prove containment using path components."""
    try:
        canonical_root = Path(root).expanduser().resolve(strict=True)
        canonical_path = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"path does not exist: {path}") from exc
    try:
        canonical_path.relative_to(canonical_root)
    except ValueError as exc:
        raise WorkspaceError(f"path is outside the allowed root: {path}") from exc
    return canonical_path


def _validate_label_component(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or Path(value).is_absolute()
        or ntpath.isabs(value)
        or ntpath.splitdrive(value)[0]
        or "/" in value
        or "\\" in value
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise WorkspaceError("source label must be a safe non-empty filename component")
    return value


def _validated_day_directory(root: Path, state: dict, day: int) -> Path:
    """Return a real in-root Day directory without resolving an unsafe child path."""
    day_directory = _day_directory(root, state, day)
    if day_directory.is_symlink():
        raise WorkspaceError(f"Day directory must not be a symlink: {day_directory}")
    try:
        resolved_day = day_directory.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"Day directory is missing: {day_directory}") from exc
    if (
        not day_directory.is_dir()
        or resolved_day != day_directory
        or not _is_within(resolved_day, root)
    ):
        raise WorkspaceError(f"Day directory must be a real directory inside the learning root: {day_directory}")
    return day_directory


def _chain_name(order: int, name: str) -> str:
    return f"{order:02d}-{name}"


def _progress_for(day: dict) -> int:
    return sum(
        checkpoint["weight"]
        for checkpoint in day["checkpoints"]
        if checkpoint.get("status") == "completed"
    )


def _validate_state_structure(root: Path, state: dict, marker: dict) -> None:
    if state.get("schema_version") != SCHEMA_VERSION:
        raise WorkspaceError("unsupported state schema_version")
    if marker.get("schema_version") != SCHEMA_VERSION:
        raise WorkspaceError("unsupported learning-root marker schema_version")
    root_id = state.get("learning_root_id")
    if not isinstance(root_id, str) or marker.get("learning_root_id") != root_id:
        raise WorkspaceError("learning-root marker and state ID do not match")

    stored_root = state.get("learning_root")
    if not isinstance(stored_root, str) or Path(stored_root) != root:
        raise WorkspaceError("canonical learning-root path does not match state")
    if marker.get("learning_root") != str(root):
        raise WorkspaceError("canonical learning-root path does not match marker")

    repository_text = state.get("repository")
    if not isinstance(repository_text, str):
        raise WorkspaceError("state repository path is invalid")
    try:
        repository = Path(repository_text).resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("state repository does not exist") from exc
    if not repository.is_dir() or str(repository) != repository_text:
        raise WorkspaceError("state repository path is not canonical")
    marker_repository = marker.get("repository")
    if not isinstance(marker_repository, str) or marker_repository != repository_text:
        raise WorkspaceError("canonical repository path does not match marker")
    if repository.parent != root.parent or repository == root:
        raise WorkspaceError("learning root must be a sibling of its repository")

    if not isinstance(state.get("route_title"), str) or not state["route_title"].strip():
        raise WorkspaceError("state route_title must be non-empty text")
    if not isinstance(state.get("updated_at"), str) or not state["updated_at"]:
        raise WorkspaceError("state updated_at must be text")
    for collection_name in ("artifacts", "history"):
        if not isinstance(state.get(collection_name), list):
            raise WorkspaceError(f"state {collection_name} must be a list")
    if any(not isinstance(entry, dict) for entry in state["history"]):
        raise WorkspaceError("state history entries must be objects")

    route_days = state.get("route_days")
    days = state.get("days")
    if type(route_days) is not int or route_days <= 0 or not isinstance(days, list):
        raise WorkspaceError("state Day metadata is invalid")
    if len(days) != route_days:
        raise WorkspaceError("state route_days does not match Days")

    active_days: list[dict] = []
    seen_pending = False
    checkpoint_ids: set[str] = set()
    for expected_day, day in enumerate(days, start=1):
        if not isinstance(day, dict) or day.get("day") != expected_day:
            raise WorkspaceError("state Days must be consecutive")
        status = day.get("status")
        if status not in {"pending", "active", "completed"}:
            raise WorkspaceError("invalid Day status")
        if status == "active":
            active_days.append(day)
        if status == "pending":
            seen_pending = True
        elif seen_pending:
            raise WorkspaceError("completed or active Day cannot follow a pending Day")
        checkpoints = day.get("checkpoints")
        if not isinstance(checkpoints, list) or not checkpoints:
            raise WorkspaceError("state Day checkpoints are invalid")
        if not isinstance(day.get("title"), str) or not day["title"].strip():
            raise WorkspaceError("state Day title must be non-empty text")
        total_weight = 0
        for checkpoint in checkpoints:
            if not isinstance(checkpoint, dict):
                raise WorkspaceError("invalid checkpoint state")
            checkpoint_id = checkpoint.get("id")
            if (
                not isinstance(checkpoint_id, str)
                or not checkpoint_id.strip()
                or checkpoint_id in checkpoint_ids
            ):
                raise WorkspaceError("invalid or duplicate checkpoint ID in state")
            checkpoint_ids.add(checkpoint_id)
            if not isinstance(checkpoint.get("title"), str) or not checkpoint["title"].strip():
                raise WorkspaceError("checkpoint title must be non-empty text")
            if type(checkpoint.get("weight")) is not int or checkpoint["weight"] <= 0:
                raise WorkspaceError("invalid checkpoint weight in state")
            total_weight += checkpoint["weight"]
            if checkpoint.get("status") not in {"pending", "completed"}:
                raise WorkspaceError("invalid checkpoint status")
        if total_weight != 100:
            raise WorkspaceError("checkpoint weights for each Day must total 100")
        progress = _progress_for(day)
        if type(day.get("progress")) is not int or day["progress"] != progress:
            raise WorkspaceError("Day progress does not match completed checkpoint weights")
        if status == "pending" and (
            progress != 0
            or any(
                checkpoint["status"] != "pending" for checkpoint in checkpoints
            )
        ):
            raise WorkspaceError(
                "pending Day must have zero progress and only pending checkpoints"
            )
        if status == "completed" and (
            progress != 100
            or any(
                checkpoint["status"] != "completed" for checkpoint in checkpoints
            )
        ):
            raise WorkspaceError(
                "completed Day must have 100 percent progress and all checkpoints completed"
            )
        _validated_day_directory(root, state, expected_day)

    if len(active_days) > 1:
        raise WorkspaceError("state contains more than one active Day")

    chains = state.get("chains")
    if not isinstance(chains, list):
        raise WorkspaceError("state chains must be a list")
    active_chains = []
    chain_paths: set[str] = set()
    chains_by_day: dict[int, list[dict]] = {}
    for chain in chains:
        if not isinstance(chain, dict) or chain.get("status") not in {"active", "sealed"}:
            raise WorkspaceError("invalid chain record")
        if type(chain.get("day")) is not int or not 1 <= chain["day"] <= route_days:
            raise WorkspaceError("invalid chain Day")
        if type(chain.get("order")) is not int or chain["order"] <= 0:
            raise WorkspaceError("invalid chain order")
        if not isinstance(chain.get("name"), str) or not chain["name"].strip():
            raise WorkspaceError("chain name must be non-empty text")
        relative = chain.get("path")
        if not isinstance(relative, str) or not relative or relative in chain_paths:
            raise WorkspaceError("invalid or duplicate chain path")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise WorkspaceError("unsafe chain path")
        expected_relative = (
            _day_directory(root, state, chain["day"])
            / _chain_name(chain["order"], chain["name"])
        ).relative_to(root).as_posix()
        if relative != expected_relative:
            raise WorkspaceError(
                "chain path does not match its Day, order, and name"
            )
        chain_paths.add(relative)
        chains_by_day.setdefault(chain["day"], []).append(chain)
        if chain["status"] == "active":
            active_chains.append(chain)
        else:
            actual_chain_path = root / relative_path
            if actual_chain_path.is_symlink() or not actual_chain_path.is_dir():
                raise WorkspaceError("sealed chain directory is missing or invalid")
            try:
                resolved_chain_path = resolved_under(actual_chain_path, root)
            except WorkspaceError as exc:
                raise WorkspaceError("sealed chain directory escapes the learning root") from exc
            if resolved_chain_path != actual_chain_path:
                raise WorkspaceError("sealed chain directory must be canonical")
            expected_marker = _expected_chain_marker(
                root_id,
                chain["day"],
                chain["order"],
                chain["name"],
            )
            try:
                actual_marker = read_json(actual_chain_path / ".learning-chain.json")
            except WorkspaceError as exc:
                raise WorkspaceError("sealed chain marker is missing or invalid") from exc
            if actual_marker != expected_marker:
                raise WorkspaceError(
                    "sealed chain marker does not match authoritative state"
                )

    for day_number, day_chains in chains_by_day.items():
        orders = sorted(chain["order"] for chain in day_chains)
        if orders != list(range(1, len(orders) + 1)):
            raise WorkspaceError(
                f"chain orders are not consecutive in Day {day_number}"
            )

    artifacts_seen: set[str] = set()
    artifact_identities_seen: set[tuple[int, int]] = set()
    for artifact_record in state["artifacts"]:
        if not isinstance(artifact_record, dict):
            raise WorkspaceError("invalid artifact record")
        owner_kind = artifact_record.get("owner_kind")
        owner_id = artifact_record.get("owner_id")
        relative_owner_text = artifact_record.get("relative_owner_dir")
        canonical_artifact_text = artifact_record.get("canonical_artifact_path")
        relative_artifact_text = artifact_record.get("relative_artifact_path")
        artifact_device = artifact_record.get("artifact_device")
        artifact_inode = artifact_record.get("artifact_inode")
        registered_at = artifact_record.get("registered_at")
        if owner_kind not in {"day-overview", "chain"}:
            raise WorkspaceError("artifact owner kind is invalid")
        if not isinstance(owner_id, str) or not owner_id:
            raise WorkspaceError("artifact owner ID is invalid")
        if not isinstance(relative_owner_text, str) or not relative_owner_text:
            raise WorkspaceError("artifact owner directory is invalid")
        relative_owner = Path(relative_owner_text)
        if relative_owner.is_absolute() or ".." in relative_owner.parts:
            raise WorkspaceError("artifact owner directory is unsafe")
        try:
            safe_relative_artifact_text = _validate_artifact_relative_path(
                relative_artifact_text
            )
        except WorkspaceError as exc:
            raise WorkspaceError("artifact relative path is invalid") from exc
        relative_artifact = Path(safe_relative_artifact_text)
        if not isinstance(canonical_artifact_text, str):
            raise WorkspaceError("artifact canonical path is invalid")
        if (
            type(artifact_device) is not int
            or artifact_device < 0
            or type(artifact_inode) is not int
            or artifact_inode <= 0
        ):
            raise WorkspaceError("artifact physical identity is invalid")
        if not isinstance(registered_at, str) or not registered_at:
            raise WorkspaceError("artifact registered_at must be text")

        if owner_kind == "day-overview":
            prefix = "day-overview:"
            if not owner_id.startswith(prefix):
                raise WorkspaceError("day overview artifact owner ID is invalid")
            day_text = owner_id[len(prefix) :]
            try:
                owner_day_number = int(day_text)
            except ValueError as exc:
                raise WorkspaceError("day overview artifact owner ID is invalid") from exc
            owner_day = next(
                (day for day in state["days"] if day.get("day") == owner_day_number),
                None,
            )
            if owner_day is None:
                raise WorkspaceError("artifact references an unknown day overview owner")
            if owner_day.get("status") not in {"active", "completed"}:
                raise WorkspaceError("artifact day owner Day status is invalid")
            expected_owner_text = (
                _day_directory(root, state, owner_day_number).relative_to(root).as_posix()
                + "/当日总览产物"
            )
            if relative_owner_text != expected_owner_text:
                raise WorkspaceError("artifact day owner directory does not match")
            allowed_directory = root / relative_owner
            expected_artifact = allowed_directory / relative_artifact
        else:
            prefix = "chain:"
            if not owner_id.startswith(prefix):
                raise WorkspaceError("chain artifact owner ID is invalid")
            chain_path = owner_id[len(prefix) :]
            owner_chain = next(
                (chain for chain in chains if chain.get("path") == chain_path),
                None,
            )
            if owner_chain is None:
                raise WorkspaceError("artifact references an unknown chain owner")
            if owner_chain.get("status") not in {"active", "sealed"}:
                raise WorkspaceError("artifact chain owner status is invalid")
            expected_owner_text = f"{chain_path}/产物"
            if relative_owner_text != expected_owner_text:
                raise WorkspaceError("artifact chain owner directory does not match")
            allowed_directory = root / relative_owner
            expected_artifact = allowed_directory / relative_artifact

        owner_directory = root / relative_owner
        if owner_directory.is_symlink() or not owner_directory.is_dir():
            raise WorkspaceError("artifact owner directory is missing or invalid")
        try:
            canonical_owner = resolved_under(owner_directory, root)
            canonical_allowed = resolved_under(allowed_directory, owner_directory)
            canonical_artifact = resolved_under(
                Path(canonical_artifact_text), canonical_allowed
            )
        except WorkspaceError as exc:
            raise WorkspaceError("artifact path escapes its allowed owner directory") from exc
        if canonical_owner != owner_directory or canonical_allowed != allowed_directory:
            raise WorkspaceError("artifact owner path must be canonical")
        if (
            canonical_artifact != expected_artifact
            or str(canonical_artifact) != canonical_artifact_text
            or canonical_artifact.is_symlink()
            or not canonical_artifact.is_file()
        ):
            raise WorkspaceError("artifact must be a canonical regular file")
        expected_relative = canonical_artifact.relative_to(owner_directory).as_posix()
        if relative_artifact_text != expected_relative:
            raise WorkspaceError(
                "artifact relative path is not the canonical owner-relative path"
            )
        artifact_metadata = canonical_artifact.stat()
        physical_identity = (artifact_metadata.st_dev, artifact_metadata.st_ino)
        if physical_identity != (artifact_device, artifact_inode):
            raise WorkspaceError("artifact physical identity changed")
        if canonical_artifact_text in artifacts_seen:
            raise WorkspaceError("artifact canonical path has more than one owner")
        if physical_identity in artifact_identities_seen:
            raise WorkspaceError("artifact physical file has more than one owner")
        artifacts_seen.add(canonical_artifact_text)
        artifact_identities_seen.add(physical_identity)

    current = state.get("current")
    if not isinstance(current, dict):
        raise WorkspaceError("state current section is invalid")
    if type(current.get("progress")) is not int:
        raise WorkspaceError("current progress must be an integer")
    if not isinstance(current.get("resume_marker"), str):
        raise WorkspaceError("current resume_marker must be text")
    if not isinstance(current.get("closed_flows"), list):
        raise WorkspaceError("current closed_flows must be a list")
    if any(not isinstance(flow, str) for flow in current["closed_flows"]):
        raise WorkspaceError("current closed_flows entries must be text")
    if current.get("last_source") is not None and not isinstance(
        current.get("last_source"), str
    ):
        raise WorkspaceError("current last_source must be text or null")
    last_source = current.get("last_source")
    resume_marker = current["resume_marker"]
    has_source = isinstance(last_source, str) and bool(last_source)
    has_marker = bool(resume_marker.strip())
    if has_source != has_marker:
        raise WorkspaceError(
            "resume source and resume marker must both be empty or both be non-empty"
        )
    if has_source:
        resume_path = Path(last_source)
        try:
            canonical_resume = resume_path.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceError("resume source does not exist") from exc
        if (
            str(canonical_resume) != last_source
            or not canonical_resume.is_file()
            or not _is_within(canonical_resume, repository)
        ):
            raise WorkspaceError(
                "resume source must be a canonical regular file inside the repository"
            )
    current_day = current.get("day")
    current_chain = current.get("chain")
    if active_days:
        active_day = active_days[0]
        if current_day != active_day["day"]:
            raise WorkspaceError("current Day does not match active Day")
        if len(active_chains) > 1:
            raise WorkspaceError("an active Day cannot have more than one active chain")
        if len(active_chains) == 1:
            active_chain = active_chains[0]
            if current_chain != active_chain:
                raise WorkspaceError("current chain does not match active chain")
            if current.get("progress") != active_day["progress"]:
                raise WorkspaceError("current progress does not match active Day")
            if active_chain["day"] != active_day["day"]:
                raise WorkspaceError("active chain Day does not match the active Day")
            actual_chain_path = root / active_chain["path"]
            if actual_chain_path.is_symlink() or not actual_chain_path.is_dir():
                raise WorkspaceError("active chain directory is missing or invalid")
            try:
                resolved_chain_path = actual_chain_path.resolve(strict=True)
            except OSError as exc:
                raise WorkspaceError("active chain directory is missing or invalid") from exc
            if resolved_chain_path != actual_chain_path or not _is_within(resolved_chain_path, root):
                raise WorkspaceError("active chain directory escapes the learning root")
            try:
                active_marker = read_json(actual_chain_path / ".learning-chain.json")
            except WorkspaceError as exc:
                raise WorkspaceError("active chain marker is missing or invalid") from exc
            expected_active_marker = _expected_chain_marker(
                root_id,
                active_chain["day"],
                active_chain["order"],
                active_chain["name"],
            )
            if active_marker != expected_active_marker:
                raise WorkspaceError("active chain marker does not match authoritative state")
        else:
            if current_chain is not None:
                raise WorkspaceError("current chain set but no active chain exists")
    else:
        if current_day is not None or current_chain is not None or active_chains:
            raise WorkspaceError("inactive state cannot contain a current Day or active chain")
        if (
            current["progress"] != 0
            or current.get("last_source") is not None
            or current["resume_marker"] != ""
            or current["closed_flows"] != []
        ):
            raise WorkspaceError("inactive current state must be fully reset")


def load_state(root: Path) -> dict:
    try:
        canonical_root = Path(root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"learning root does not exist: {root}") from exc
    if not canonical_root.is_dir():
        raise WorkspaceError("learning root is not a directory")
    marker = read_json(canonical_root / ".learning-root.json")
    state = read_json(canonical_root / "学习状态.json")
    _validate_state_structure(canonical_root, state, marker)
    return state


def _validate_candidate_state(root: Path, state: dict) -> None:
    marker = read_json(root / ".learning-root.json")
    _validate_state_structure(root, state, marker)


def save_state(root: Path, state: dict) -> None:
    root = Path(root).resolve(strict=True)
    state["updated_at"] = _utc_timestamp()
    _validate_candidate_state(root, state)
    replace_authority_json(root / "学习状态.json", _state_text(state))
    try:
        replace_projection_markdown(root / "学习状态.md", render_state(state))
    except OSError as exc:
        raise WorkspaceError(
            "authoritative JSON was committed but the Markdown projection failed; run sync-state"
        ) from exc


def _expected_chain_marker(
    learning_root_id: str, day: int, order: int, name: str
) -> dict:
    return {
        "kind": CHAIN_MARKER_KIND,
        "marker_version": CHAIN_MARKER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "learning_root_id": learning_root_id,
        "day": day,
        "order": order,
        "name": name,
    }


def _open_directory_at(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(name, flags, dir_fd=parent_fd)


def _read_json_at(directory_fd: int, name: str) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"cannot read generated marker {name}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise WorkspaceError(f"generated marker must be an object: {name}")
    return value


def _owned_chain_directory(path: Path, expected_marker: dict) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        marker = read_json(path / ".learning-chain.json")
    except WorkspaceError:
        return False
    return all(marker.get(key) == value for key, value in expected_marker.items())


def _remove_owned_chain(root: Path, path: Path, expected_marker: dict) -> None:
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        return
    if (
        path.parent.is_symlink()
        or parent != path.parent
        or not _is_within(parent, root)
        or path.is_symlink()
    ):
        return
    if _owned_chain_directory(path, expected_marker):
        shutil.rmtree(path)


def _prepare_chain_target(root: Path, state: dict, day: int, order: int, name: str) -> Path:
    day_directory = _validated_day_directory(root, state, day)
    target = day_directory / _chain_name(order, name)
    expected_marker = _expected_chain_marker(
        state["learning_root_id"], day, order, name
    )
    registered = {root / chain["path"] for chain in state["chains"]}
    if target in registered:
        raise WorkspaceError(f"chain is already registered: {target}")
    if target.exists() or target.is_symlink():
        if _owned_chain_directory(target, expected_marker):
            _remove_owned_chain(root, target, expected_marker)
            if target.exists() or target.is_symlink():
                raise WorkspaceError(f"chain target could not be safely cleaned: {target}")
        else:
            raise WorkspaceError(f"chain target is occupied by an unknown directory: {target}")
    return target


def create_chain(root: Path, day: int, order: int, name: str) -> Path:
    root = Path(root).resolve(strict=True)
    marker = read_json(root / ".learning-root.json")
    root_id = marker.get("learning_root_id")
    if not isinstance(root_id, str):
        raise WorkspaceError("learning-root marker has no valid ID")
    state = read_json(root / "学习状态.json")
    day_directory = _validated_day_directory(root, state, day)
    target = day_directory / _chain_name(order, name)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=day_directory))
    try:
        atomic_write_json(
            temporary / ".learning-chain.json",
            _expected_chain_marker(root_id, day, order, name),
        )
        materialize_template(
            "chain-brief.md",
            temporary / "链说明.md",
            {
                "DAY_NUMBER": day,
                "CHAIN_ORDER": order,
                "CHAIN_NAME": name,
                "TIMESTAMP": _utc_timestamp(),
            },
        )
        (temporary / "产物").mkdir()
        _rename_no_replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def _chain_record(root: Path, path: Path, day: int, order: int, name: str) -> dict:
    return {
        "day": day,
        "order": order,
        "name": name,
        "path": path.relative_to(root).as_posix(),
        "status": "active",
        "created_at": _utc_timestamp(),
    }


def _seal_active_chain(state: dict) -> None:
    active = [chain for chain in state["chains"] if chain["status"] == "active"]
    if len(active) != 1:
        raise WorkspaceError("expected exactly one active chain")
    active[0]["status"] = "sealed"
    active[0]["sealed_at"] = _utc_timestamp()
    state["current"]["chain"] = None


def _state_was_committed(root: Path, state: dict) -> bool:
    try:
        return (root / "学习状态.json").read_text(encoding="utf-8") == _state_text(state)
    except (OSError, UnicodeError):
        return False


def _save_with_created_chain(root: Path, state: dict, created: Path) -> None:
    try:
        save_state(root, state)
    except BaseException:
        if not _state_was_committed(root, state):
            relative_created = created.relative_to(root).as_posix()
            created_record = next(
                (
                    chain
                    for chain in state["chains"]
                    if chain.get("path") == relative_created
                ),
                None,
            )
            if created_record is not None:
                expected_marker = _expected_chain_marker(
                    state["learning_root_id"],
                    created_record["day"],
                    created_record["order"],
                    created_record["name"],
                )
                _remove_owned_chain(root, created, expected_marker)
        raise


def _active_day(state: dict) -> dict:
    current_day = state["current"]["day"]
    if current_day is None:
        raise WorkspaceError("no Day is active")
    day = state["days"][current_day - 1]
    if day["status"] != "active":
        raise WorkspaceError("current Day is not active")
    return day


def _checkpoint_in_active_day(state: dict, checkpoint_id: str) -> tuple[dict, dict]:
    day = _active_day(state)
    for checkpoint in day["checkpoints"]:
        if checkpoint["id"] == checkpoint_id:
            return day, checkpoint
    raise WorkspaceError(f"checkpoint is not in the active Day: {checkpoint_id}")


def _append_history(state: dict, event: str, **details: Any) -> None:
    state["history"].append({"event": event, "at": _utc_timestamp(), **details})


def _create_day_level_files(day_directory: Path, day: dict) -> None:
    """Populate a Day directory with its human-authored Day-level files."""
    checkpoint_text = "；".join(
        f"{checkpoint['id']}：{checkpoint['title']}（{checkpoint['weight']}%）"
        for checkpoint in day["checkpoints"]
    )
    common = {
        "TIMESTAMP": _utc_timestamp(),
        "DAY_NUMBER": day["day"],
        "CHECKPOINTS": checkpoint_text,
    }
    materialize_template(
        "questions-and-answers.md",
        day_directory / "问题与解答.md",
        {
            **common,
            "USER_QUESTION": "待记录",
            "CONCISE_ANSWER": "待整理",
            "RELATED_FILE_SYMBOL": "待补充",
            "UNRESOLVED_FOLLOW_UP": "无",
        },
    )
    materialize_template(
        "day-summary.md",
        day_directory / "当日总结.md",
        {
            **common,
            "CLOSED_FLOWS": "暂无",
            "KEY_CONCEPTS": "待整理",
            "REMAINING_QUESTIONS": "无",
            "CONTINUATION_POINT": "待设置",
        },
    )
    overview_directory = day_directory / "当日总览产物"
    overview_directory.mkdir(exist_ok=True)
    materialize_template(
        "day-overview.md",
        overview_directory / "当日统一总结.md",
        {
            **common,
            "LOCAL_FLOW_ARTIFACT_LINKS": "暂无",
            "WHOLE_FRAMEWORK_RELATION": "待整理",
            "CROSS_SOURCE_CONCLUSIONS": "待整理",
            "ARTIFACTS": "- 暂无已登记产物",
        },
    )


def command_start_day(args: argparse.Namespace) -> Path:
    root = Path(args.learning_root).expanduser().resolve(strict=True)
    state = load_state(root)
    if state["current"]["day"] is not None:
        raise WorkspaceError("another Day is already active")
    pending = [day for day in state["days"] if day["status"] == "pending"]
    if not pending:
        raise WorkspaceError("all Days are already completed")
    target_day = pending[0]
    if args.day != target_day["day"]:
        raise WorkspaceError(f"the next Day must be {target_day['day']}")

    day_directory = _validated_day_directory(root, state, args.day)
    _create_day_level_files(day_directory, target_day)

    target_day["status"] = "active"
    target_day["progress"] = 0
    state["current"].update(
        {
            "day": args.day,
            "chain": None,
            "progress": 0,
            "last_source": None,
            "resume_marker": "",
            "closed_flows": [],
        }
    )
    _append_history(state, "start-day", day=args.day)
    save_state(root, state)
    return root


def command_begin_chain(args: argparse.Namespace) -> Path:
    root = Path(args.learning_root).expanduser().resolve(strict=True)
    state = load_state(root)
    day = _active_day(state)
    if args.day != day["day"]:
        raise WorkspaceError("begin-chain must target the active Day")
    if not isinstance(args.name, str) or not args.name.strip():
        raise WorkspaceError("chain name must be non-empty")
    name = _validate_label_component(args.name.strip())

    existing = [chain for chain in state["chains"] if chain["day"] == day["day"]]
    order = len(existing) + 1
    target = _prepare_chain_target(root, state, day["day"], order, name)
    created = create_chain(root, day["day"], order, name)
    if created != target:
        raise WorkspaceError("created chain path does not match target")

    if state["current"].get("chain") is not None:
        _seal_active_chain(state)
    chain = _chain_record(root, created, day["day"], order, name)
    state["chains"].append(chain)
    state["current"]["chain"] = copy.deepcopy(chain)
    _append_history(
        state,
        "begin-chain",
        day=day["day"],
        chain=chain["path"],
        name=name,
    )
    _save_with_created_chain(root, state, created)
    return created


def command_complete_checkpoint(args: argparse.Namespace) -> Path:
    root = Path(args.learning_root).expanduser().resolve(strict=True)
    state = load_state(root)
    day, checkpoint = _checkpoint_in_active_day(state, args.checkpoint)
    if checkpoint["status"] != "pending":
        raise WorkspaceError(f"checkpoint is already completed: {args.checkpoint}")
    checkpoint["status"] = "completed"
    progress = _progress_for(day)
    day["progress"] = progress
    state["current"]["progress"] = progress
    _append_history(
        state,
        "complete-checkpoint",
        day=day["day"],
        checkpoint=args.checkpoint,
        progress=progress,
    )
    save_state(root, state)
    return root


def command_reopen_checkpoint(args: argparse.Namespace) -> Path:
    root = Path(args.learning_root).expanduser().resolve(strict=True)
    state = load_state(root)
    day, checkpoint = _checkpoint_in_active_day(state, args.checkpoint)
    if checkpoint["status"] != "completed":
        raise WorkspaceError(f"checkpoint is not completed: {args.checkpoint}")
    if not isinstance(args.reason, str) or not args.reason.strip():
        raise WorkspaceError("reopen reason must be non-empty")

    checkpoint["status"] = "pending"
    progress = _progress_for(day)
    day["progress"] = progress
    state["current"]["progress"] = progress
    _append_history(
        state,
        "reopen-checkpoint",
        day=day["day"],
        checkpoint=args.checkpoint,
        reason=args.reason.strip(),
        progress=progress,
    )
    save_state(root, state)
    return root


def command_set_resume(args: argparse.Namespace) -> Path:
    root = Path(args.learning_root).expanduser().resolve(strict=True)
    state = load_state(root)
    _active_day(state)
    repository = Path(state["repository"])
    try:
        source = resolved_under(Path(args.source), repository)
    except WorkspaceError as exc:
        raise WorkspaceError("resume source must be a regular file inside the repository") from exc
    if not source.is_file():
        raise WorkspaceError("resume source must be a regular file inside the repository")
    if not isinstance(args.marker, str) or not args.marker.strip():
        raise WorkspaceError("resume marker must be non-empty")
    state["current"]["last_source"] = str(source)
    state["current"]["resume_marker"] = args.marker.strip()
    _append_history(
        state,
        "set-resume",
        day=state["current"]["day"],
        source=str(source),
        marker=args.marker.strip(),
    )
    save_state(root, state)
    return root


def _absolute_input_without_parent_segments(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise WorkspaceError(f"{label} path must be non-empty text")
    expanded = Path(value).expanduser()
    if ".." in expanded.parts:
        raise WorkspaceError(f"{label} path must not contain parent traversal")
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(expanded))


def _validate_artifact_relative_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "`" in value
        or ARTIFACT_BLOCK_START in value
        or ARTIFACT_BLOCK_END in value
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise WorkspaceError("artifact relative path contains unsafe display text")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise WorkspaceError("artifact relative path is unsafe")
    normalized = path.as_posix()
    if value != normalized or any(part in {"", "."} for part in path.parts):
        raise WorkspaceError("artifact relative path is not in canonical lexical form")
    return value


def _require_artifact_fd_support() -> None:
    required_dir_fd = (os.open, os.stat, os.unlink)
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        raise WorkspaceError(
            "secure artifact registration requires directory-fd operations"
        )
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise WorkspaceError(
            "secure artifact registration requires no-follow directory handles"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    exchange_symbol = (
        "renameatx_np"
        if sys.platform == "darwin"
        else "renameat2"
        if sys.platform.startswith("linux")
        else None
    )
    if exchange_symbol is None or not hasattr(libc, exchange_symbol):
        raise WorkspaceError("atomic projection exchange is unavailable")


def _stable_path_matches_fd(path: Path, descriptor: int, *, directory: bool) -> bool:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    return expected_kind(metadata.st_mode) and os.path.samestat(
        metadata, os.fstat(descriptor)
    )


def _open_regular_beneath(
    directory_fd: int, relative: Path
) -> tuple[int, str, int, os.stat_result]:
    parts = relative.parts
    if not parts or relative.is_absolute() or ".." in parts or any(
        part in {"", "."} for part in parts
    ):
        raise WorkspaceError("artifact path must be a safe relative file path")
    current_fd = os.dup(directory_fd)
    try:
        for component in parts[:-1]:
            child_fd = _open_directory_at(current_fd, component)
            os.close(current_fd)
            current_fd = child_fd
        name = parts[-1]
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        artifact_fd = os.open(name, flags, dir_fd=current_fd)
        metadata = os.fstat(artifact_fd)
        if not stat.S_ISREG(metadata.st_mode) or not _entry_matches_metadata(
            current_fd, name, metadata, regular=True
        ):
            os.close(artifact_fd)
            raise WorkspaceError("artifact must be a stable regular file")
        return current_fd, name, artifact_fd, metadata
    except BaseException:
        os.close(current_fd)
        raise


def _read_utf8_fd(descriptor: int, label: str) -> str:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkspaceError(f"cannot read {label} as UTF-8: {exc}") from exc


def command_register_artifact(args: argparse.Namespace) -> Path:
    _require_artifact_fd_support()
    root_argument = _absolute_input_without_parent_segments(
        args.learning_root, "learning root"
    )
    try:
        root = root_argument.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("learning root does not exist") from exc
    state = load_state(root)

    owner_argument = _absolute_input_without_parent_segments(args.owner_dir, "owner")
    if owner_argument.is_symlink():
        raise WorkspaceError("artifact owner directory must not be a symlink")
    try:
        owner = owner_argument.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("artifact owner directory does not exist") from exc
    if owner != owner_argument or not owner.is_dir():
        raise WorkspaceError("artifact owner directory must be canonical")

    owner_kind = args.owner_kind
    owner_active_parent: dict | None = None
    if owner_kind == "day-overview":
        relative_owner_text = owner.relative_to(root).as_posix()
        if not relative_owner_text.endswith("/当日总览产物"):
            raise WorkspaceError(
                "day overview owner must be a Day's 当日总览产物 directory"
            )
        day_dir_text = relative_owner_text[: -len("/当日总览产物")]
        owner_day = next(
            (
                day
                for day in state["days"]
                if _day_directory(root, state, day["day"]).relative_to(root).as_posix()
                == day_dir_text
            ),
            None,
        )
        if owner_day is None:
            raise WorkspaceError("artifact references an unknown day overview owner")
        owner_active_parent = owner_day
        relative_owner = relative_owner_text
        owner_id = f"day-overview:{owner_day['day']}"
        allowed_directory = owner
        owner_markdown = owner / "当日统一总结.md"
        markdown_name = "当日统一总结.md"
    elif owner_kind == "chain":
        relative_owner_text = owner.relative_to(root).as_posix()
        if not relative_owner_text.endswith("/产物"):
            raise WorkspaceError("chain owner must be a chain's 产物 directory")
        chain_dir_text = relative_owner_text[: -len("/产物")]
        owner_chain = next(
            (
                chain
                for chain in state["chains"]
                if chain.get("path") == chain_dir_text
            ),
            None,
        )
        if owner_chain is None:
            raise WorkspaceError("artifact references an unknown chain owner")
        owner_active_parent = owner_chain
        relative_owner = relative_owner_text
        owner_id = f"chain:{chain_dir_text}"
        allowed_directory = owner
        owner_markdown = owner.parent / "链说明.md"
        markdown_name = "链说明.md"
    else:
        raise WorkspaceError("owner kind must be day-overview or chain")

    if allowed_directory.is_symlink():
        raise WorkspaceError("artifact directory must not be a symlink")
    try:
        canonical_allowed = allowed_directory.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("artifact directory is missing") from exc
    if canonical_allowed != allowed_directory or not canonical_allowed.is_dir():
        raise WorkspaceError("artifact directory must be canonical")

    artifact_argument = _absolute_input_without_parent_segments(
        args.artifact, "artifact"
    )
    if artifact_argument.is_symlink():
        raise WorkspaceError("artifact must not be a symlink")
    try:
        artifact = artifact_argument.resolve(strict=True)
        artifact_relative_allowed = artifact.relative_to(canonical_allowed)
    except (OSError, ValueError) as exc:
        raise WorkspaceError(
            "artifact must be an existing regular file inside the owner artifact directory"
        ) from exc
    if artifact != artifact_argument or not artifact.is_file():
        raise WorkspaceError("artifact must be a canonical regular file")
    if artifact == owner_markdown:
        raise WorkspaceError("owner Markdown cannot register itself as an artifact")
    relative_artifact = _validate_artifact_relative_path(
        artifact.relative_to(owner).as_posix()
    )

    open_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    owner_fd = os.open(owner, open_flags)
    allowed_fd: int | None = None
    artifact_parent_fd: int | None = None
    artifact_fd: int | None = None
    markdown_fd: int | None = None
    try:
        if not _stable_path_matches_fd(owner, owner_fd, directory=True):
            raise WorkspaceError("artifact owner identity changed")
        allowed_fd = os.dup(owner_fd)
        if not _stable_path_matches_fd(allowed_directory, allowed_fd, directory=True):
            raise WorkspaceError("artifact directory identity changed")
        (
            artifact_parent_fd,
            artifact_name,
            artifact_fd,
            artifact_metadata,
        ) = _open_regular_beneath(allowed_fd, artifact_relative_allowed)
        if not _stable_path_matches_fd(artifact, artifact_fd, directory=False):
            raise WorkspaceError("artifact identity changed")

        markdown = None
        markdown_metadata = None
        if owner_kind != "chain":
            markdown_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            markdown_fd = os.open(markdown_name, markdown_flags, dir_fd=owner_fd)
            markdown_metadata = os.fstat(markdown_fd)
            if not stat.S_ISREG(markdown_metadata.st_mode) or not _entry_matches_metadata(
                owner_fd, markdown_name, markdown_metadata, regular=True
            ):
                raise WorkspaceError("owner Markdown must be a stable regular file")
            markdown = _read_utf8_fd(markdown_fd, "owner Markdown")

        physical_identity = (artifact_metadata.st_dev, artifact_metadata.st_ino)
        existing_matches = [
            entry
            for entry in state["artifacts"]
            if entry.get("canonical_artifact_path") == str(artifact)
            or (
                entry.get("artifact_device"), entry.get("artifact_inode")
            )
            == physical_identity
        ]
        if len(existing_matches) > 1:
            raise WorkspaceError("artifact has duplicate authoritative ownership records")
        existing = existing_matches[0] if existing_matches else None
        is_new = existing is None
        if existing is not None and existing.get("owner_id") != owner_id:
            raise WorkspaceError("artifact already has a different owner")
        if existing is not None and (
            existing.get("owner_kind") != owner_kind
            or existing.get("relative_owner_dir") != relative_owner
        ):
            raise WorkspaceError("artifact ownership record is inconsistent")
        if (
            existing is not None
            and existing.get("canonical_artifact_path") == str(artifact)
            and existing.get("relative_artifact_path") != relative_artifact
        ):
            raise WorkspaceError("artifact ownership record is inconsistent")
        if owner_kind == "day-overview":
            owner_is_active = (
                owner_active_parent is not None
                and owner_active_parent.get("status") == "active"
            )
        else:
            owner_is_active = (
                owner_active_parent is not None
                and owner_active_parent.get("status") == "active"
                and state["current"].get("chain") == owner_active_parent
                and any(
                    day.get("day") == owner_active_parent.get("day")
                    and day.get("status") == "active"
                    for day in state["days"]
                )
            )
        if is_new and not owner_is_active:
            raise WorkspaceError(
                "new artifact registration requires an active owner"
            )

        candidate = copy.deepcopy(state)
        if is_new:
            candidate["artifacts"].append(
                {
                    "canonical_artifact_path": str(artifact),
                    "owner_id": owner_id,
                    "owner_kind": owner_kind,
                    "relative_owner_dir": relative_owner,
                    "relative_artifact_path": relative_artifact,
                    "artifact_device": artifact_metadata.st_dev,
                    "artifact_inode": artifact_metadata.st_ino,
                    "registered_at": _utc_timestamp(),
                }
            )
            candidate["updated_at"] = _utc_timestamp()
            _validate_candidate_state(root, candidate)
        candidate_markdown = None
        if markdown is not None:
            owner_entries = sorted(
                (
                    f"- `{entry['relative_artifact_path']}`"
                    for entry in candidate["artifacts"]
                    if entry.get("owner_id") == owner_id
                ),
                key=str.casefold,
            )
            candidate_markdown = replace_managed_artifact_block(
                markdown, owner_entries
            )
        candidate_json = _state_text(candidate)

        artifact_pre_authority_seam(root, candidate, owner_markdown, artifact)
        inputs_stable = _stable_path_matches_fd(owner, owner_fd, directory=True) and _entry_matches_metadata(
            artifact_parent_fd,
            artifact_name,
            artifact_metadata,
            regular=True,
        ) and _stable_path_matches_fd(artifact, artifact_fd, directory=False)
        if markdown is not None:
            inputs_stable = (
                inputs_stable
                and _entry_matches_metadata(
                    owner_fd, markdown_name, markdown_metadata, regular=True
                )
                and _read_utf8_fd(markdown_fd, "owner Markdown") == markdown
            )
        if not inputs_stable:
            raise WorkspaceError("artifact registration inputs changed before commit")

        if is_new:
            replace_authority_json(root / "学习状态.json", candidate_json)
        try:
            artifact_post_authority_seam(root, candidate, owner_markdown, artifact)
            inputs_stable = _stable_path_matches_fd(owner, owner_fd, directory=True) and _entry_matches_metadata(
                artifact_parent_fd,
                artifact_name,
                artifact_metadata,
                regular=True,
            ) and _stable_path_matches_fd(artifact, artifact_fd, directory=False)
            if markdown is not None:
                inputs_stable = (
                    inputs_stable
                    and _entry_matches_metadata(
                        owner_fd, markdown_name, markdown_metadata, regular=True
                    )
                    and _read_utf8_fd(markdown_fd, "owner Markdown") == markdown
                )
            if not inputs_stable:
                raise WorkspaceError(
                    "artifact registration inputs changed after authority commit"
                )
            if candidate_markdown is not None:
                replace_owner_markdown(
                    owner_fd,
                    markdown_name,
                    candidate_markdown,
                    markdown_metadata,
                    markdown,
                )
        except (OSError, UnicodeError, WorkspaceError) as exc:
            if is_new or _state_was_committed(root, candidate):
                raise WorkspaceError(
                    "authoritative JSON owns the artifact, but owner Markdown projection "
                    f"failed ({exc}); retry register-artifact or repair the managed block"
                ) from exc
            raise
        if is_new:
            try:
                replace_projection_markdown(
                    root / "学习状态.md", render_state(candidate)
                )
            except OSError as exc:
                raise WorkspaceError(
                    "authoritative JSON was committed but the state Markdown projection "
                    "failed; run sync-state"
                ) from exc
    finally:
        for descriptor in (
            markdown_fd,
            artifact_fd,
            artifact_parent_fd,
            allowed_fd,
            owner_fd,
        ):
            if descriptor is not None:
                os.close(descriptor)
    if existing is not None:
        return Path(existing["canonical_artifact_path"])
    return artifact


def command_complete_day(args: argparse.Namespace) -> Path:
    root = Path(args.learning_root).expanduser().resolve(strict=True)
    state = load_state(root)
    day = _active_day(state)
    if args.day != day["day"]:
        raise WorkspaceError("only the active Day can be completed")
    progress = _progress_for(day)
    if progress != 100 or any(
        checkpoint["status"] != "completed" for checkpoint in day["checkpoints"]
    ):
        raise WorkspaceError("all checkpoints must be completed before completing the Day")
    if sum(chain["status"] == "active" for chain in state["chains"]) > 1:
        raise WorkspaceError("an active Day cannot have more than one active chain")
    if any(chain["status"] == "active" for chain in state["chains"]):
        _seal_active_chain(state)
    day["status"] = "completed"
    day["progress"] = 100
    state["current"].update(
        {
            "day": None,
            "chain": None,
            "progress": 0,
            "last_source": None,
            "resume_marker": "",
            "closed_flows": [],
        }
    )
    _append_history(state, "complete-day", day=args.day)
    save_state(root, state)
    return root


def command_sync_state(args: argparse.Namespace) -> Path:
    root = Path(args.learning_root).expanduser().resolve(strict=True)
    state = load_state(root)
    replace_projection_markdown(root / "学习状态.md", render_state(state))
    return root


def command_init(args: argparse.Namespace) -> Path:
    repo_argument = Path(args.repo).expanduser()
    roadmap_argument = Path(args.roadmap).expanduser()
    try:
        repository = repo_argument.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"repository does not exist: {repo_argument}") from exc
    if not repository.is_dir():
        raise WorkspaceError(f"repository is not a directory: {repository}")
    try:
        roadmap_path = roadmap_argument.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"roadmap does not exist: {roadmap_argument}") from exc
    if not roadmap_path.is_file():
        raise WorkspaceError(f"roadmap is not a file: {roadmap_path}")

    normalized = validate_roadmap(read_json(roadmap_path))
    if args.project_name is None:
        project_name = sanitize_component(repository.name)
        if not project_name:
            raise WorkspaceError("repository name cannot form a safe project name")
    else:
        project_name = _validate_custom_project_name(args.project_name)

    route_days = len(normalized["days"])
    final_root = repository.parent / f"{project_name}-{route_days}天学习路线"
    if final_root.exists() or final_root.is_symlink():
        raise WorkspaceError(f"learning workspace already exists: {final_root}")

    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{final_root.name}.tmp-", dir=final_root.parent)
    )
    try:
        day_width = max(2, len(str(route_days)))
        for day in normalized["days"]:
            day_directory = temporary_root / f"Day{day['day']:0{day_width}d}"
            day_directory.mkdir()
            materialize_template(
                "oral-answer-archive.md",
                day_directory / "口述回答归档.md",
                {
                    "DAY_NUMBER": day["day"],
                    "DAY_TITLE": day["title"],
                },
            )

        learning_root_id = str(uuid.uuid4())
        state = _initial_state(normalized, repository, final_root, learning_root_id)
        marker = {
            "schema_version": SCHEMA_VERSION,
            "learning_root_id": learning_root_id,
            "learning_root": str(final_root.resolve(strict=False)),
            "repository": str(repository),
        }
        atomic_write_text(
            temporary_root / "学习路线.md",
            render_roadmap(normalized, project_name, repository.name),
        )
        atomic_write_json(temporary_root / ".learning-root.json", marker)
        commit_state(temporary_root, state)
        finalize_workspace(temporary_root, final_root)
    except BaseException:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    return final_root.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create a learning workspace")
    init_parser.add_argument("--repo", required=True)
    init_parser.add_argument("--roadmap", required=True)
    init_parser.add_argument("--project-name")
    init_parser.set_defaults(handler=command_init)

    start_day_parser = subparsers.add_parser("start-day", help="start the next Day")
    start_day_parser.add_argument("--learning-root", required=True)
    start_day_parser.add_argument("--day", required=True, type=int)
    start_day_parser.set_defaults(handler=command_start_day)

    complete_checkpoint_parser = subparsers.add_parser(
        "complete-checkpoint", help="complete one checkpoint in the active Day"
    )
    complete_checkpoint_parser.add_argument("--learning-root", required=True)
    complete_checkpoint_parser.add_argument("--checkpoint", required=True)
    complete_checkpoint_parser.set_defaults(handler=command_complete_checkpoint)

    reopen_checkpoint_parser = subparsers.add_parser(
        "reopen-checkpoint", help="reopen a completed checkpoint"
    )
    reopen_checkpoint_parser.add_argument("--learning-root", required=True)
    reopen_checkpoint_parser.add_argument("--checkpoint", required=True)
    reopen_checkpoint_parser.add_argument("--reason", required=True)
    reopen_checkpoint_parser.set_defaults(handler=command_reopen_checkpoint)

    resume_parser = subparsers.add_parser("set-resume", help="set the source resume point")
    resume_parser.add_argument("--learning-root", required=True)
    resume_parser.add_argument("--source", required=True)
    resume_parser.add_argument("--marker", required=True)
    resume_parser.set_defaults(handler=command_set_resume)

    chain_parser = subparsers.add_parser(
        "begin-chain", help="begin a new learning chain in the active Day"
    )
    chain_parser.add_argument("--learning-root", required=True)
    chain_parser.add_argument("--day", required=True, type=int)
    chain_parser.add_argument("--name", required=True)
    chain_parser.set_defaults(handler=command_begin_chain)

    artifact_parser = subparsers.add_parser(
        "register-artifact", help="register a learning artifact with one active owner"
    )
    artifact_parser.add_argument("--learning-root", required=True)
    artifact_parser.add_argument(
        "--owner-kind", required=True, choices=("day-overview", "chain")
    )
    artifact_parser.add_argument("--owner-dir", required=True)
    artifact_parser.add_argument("--artifact", required=True)
    artifact_parser.set_defaults(handler=command_register_artifact)

    complete_day_parser = subparsers.add_parser("complete-day", help="complete the active Day")
    complete_day_parser.add_argument("--learning-root", required=True)
    complete_day_parser.add_argument("--day", required=True, type=int)
    complete_day_parser.set_defaults(handler=command_complete_day)

    sync_parser = subparsers.add_parser(
        "sync-state", help="regenerate the Markdown state projection"
    )
    sync_parser.add_argument("--learning-root", required=True)
    sync_parser.set_defaults(handler=command_sync_state)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except (WorkspaceError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
