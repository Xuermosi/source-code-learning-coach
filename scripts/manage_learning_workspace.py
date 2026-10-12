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


SCHEMA_VERSION = 1
DEFAULT_STAGE_RANGES = [[0, 30], [31, 60], [61, 100]]
STAGE_MARKER_KIND = "source-code-learning-stage"
STAGE_MARKER_VERSION = 1
SOURCE_MARKER_KIND = "source-code-learning-source"
SOURCE_MARKER_VERSION = 1
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

    if "stage_ranges" not in normalized:
        normalized["stage_ranges"] = copy.deepcopy(DEFAULT_STAGE_RANGES)
    ranges = normalized["stage_ranges"]
    if not isinstance(ranges, list) or not ranges:
        raise WorkspaceError("stage_ranges must be a non-empty list")
    previous_end: int | None = None
    for index, interval in enumerate(ranges):
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            raise WorkspaceError("each stage range must contain two bounds")
        start, end = interval
        if type(start) is not int or type(end) is not int:
            raise WorkspaceError("stage range bounds must be integers")
        if not (0 <= start <= end <= 100):
            raise WorkspaceError("stage range bounds must be within 0..100")
        if index == 0 and start != 0:
            raise WorkspaceError("stage ranges must start at 0")
        if previous_end is not None and start != previous_end + 1:
            raise WorkspaceError("stage ranges must be ordered, adjacent, and non-overlapping")
        previous_end = end
        normalized["stage_ranges"][index] = [start, end]
    if previous_end != 100:
        raise WorkspaceError("stage ranges must end at 100")

    return normalized


def render_roadmap(data: dict, project_name: str, repository_identity: str) -> str:
    range_lines = [f"- {start}-{end}%" for start, end in data["stage_ranges"]]
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
            "STAGE_RANGES": "\n".join(range_lines),
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
    filled = min(10, max(0, progress // 10))
    progress_bar = "[" + "#" * filled + "-" * (10 - filled) + "]"
    active_stage = current.get("stage")
    active_stage_text = active_stage.get("path", "未开始") if active_stage else "未开始"
    sealed = [entry["path"] for entry in state.get("stages", []) if entry.get("status") == "sealed"]
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
            "PROGRESS_BAR": progress_bar,
            "CURRENT_PROGRESS": progress,
            "ACTIVE_STAGE": active_stage_text,
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
        "stage_ranges": copy.deepcopy(normalized["stage_ranges"]),
        "days": days,
        "current": {
            "day": None,
            "stage": None,
            "progress": 0,
            "last_source": None,
            "resume_marker": "",
            "closed_flows": [],
        },
        "stages": [],
        "sources": [],
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


def _validate_stage_ranges(ranges: Any) -> list[list[int]]:
    if not isinstance(ranges, list) or not ranges:
        raise WorkspaceError("state stage_ranges must be a non-empty list")
    normalized: list[list[int]] = []
    previous_end: int | None = None
    for index, bounds in enumerate(ranges):
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise WorkspaceError("invalid stage range in state")
        start, end = bounds
        if type(start) is not int or type(end) is not int or not (0 <= start <= end <= 100):
            raise WorkspaceError("invalid stage range in state")
        if index == 0 and start != 0:
            raise WorkspaceError("state stage ranges must start at 0")
        if previous_end is not None and start != previous_end + 1:
            raise WorkspaceError("state stage ranges must be adjacent")
        normalized.append([start, end])
        previous_end = end
    if previous_end != 100:
        raise WorkspaceError("state stage ranges must end at 100")
    return normalized


def stage_range_for(progress: int, ranges: list[list[int]]) -> list[int]:
    if type(progress) is not int or not 0 <= progress <= 100:
        raise WorkspaceError("progress must be an integer from 0 to 100")
    for start, end in _validate_stage_ranges(ranges):
        if start <= progress <= end:
            return [start, end]
    raise WorkspaceError(f"no stage range contains progress {progress}")


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
    for collection_name in ("sources", "artifacts", "history"):
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
    _validate_stage_ranges(state.get("stage_ranges"))

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

    stages = state.get("stages")
    if not isinstance(stages, list):
        raise WorkspaceError("state stages must be a list")
    active_stages = []
    stage_paths: set[str] = set()
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("status") not in {"active", "sealed"}:
            raise WorkspaceError("invalid stage record")
        if type(stage.get("day")) is not int or not 1 <= stage["day"] <= route_days:
            raise WorkspaceError("invalid stage Day")
        bounds = stage.get("range")
        if bounds not in state["stage_ranges"]:
            raise WorkspaceError("stage record uses an unknown range")
        if type(stage.get("revision")) is not int or stage["revision"] < 0:
            raise WorkspaceError("invalid stage revision")
        relative = stage.get("path")
        if not isinstance(relative, str) or not relative or relative in stage_paths:
            raise WorkspaceError("invalid or duplicate stage path")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise WorkspaceError("unsafe stage path")
        stage_paths.add(relative)
        if stage["status"] == "active":
            active_stages.append(stage)
        else:
            expected_stage_path = (
                _day_directory(root, state, stage["day"])
                / _stage_name(stage["range"], stage["revision"])
            )
            actual_stage_path = root / relative_path
            if actual_stage_path != expected_stage_path:
                raise WorkspaceError(
                    "sealed stage path does not match its Day, range, and revision"
                )
            if actual_stage_path.is_symlink() or not actual_stage_path.is_dir():
                raise WorkspaceError("sealed stage directory is missing or invalid")
            try:
                resolved_stage_path = resolved_under(actual_stage_path, root)
            except WorkspaceError as exc:
                raise WorkspaceError("sealed stage directory escapes the learning root") from exc
            if resolved_stage_path != actual_stage_path:
                raise WorkspaceError("sealed stage directory must be canonical")
            expected_marker = _expected_stage_marker(
                root_id,
                stage["day"],
                stage["range"],
                stage["revision"],
            )
            try:
                actual_marker = read_json(actual_stage_path / ".learning-stage.json")
            except WorkspaceError as exc:
                raise WorkspaceError("sealed stage marker is missing or invalid") from exc
            if actual_marker != expected_marker:
                raise WorkspaceError(
                    "sealed stage marker does not match authoritative state"
                )

    source_identities: set[tuple[str, str]] = set()
    source_paths: set[str] = set()
    source_orders: dict[str, set[int]] = {}
    stages_by_id = {stage["path"]: stage for stage in stages}
    for source_record in state["sources"]:
        if not isinstance(source_record, dict):
            raise WorkspaceError("invalid source record")
        stage_id = source_record.get("stage_id")
        canonical_source_text = source_record.get("canonical_source_path")
        relative_source_text = source_record.get("repository_relative_path")
        relative_learning_text = source_record.get("relative_learning_dir")
        order = source_record.get("order")
        label = source_record.get("label")
        created_at = source_record.get("created_at")
        if not isinstance(stage_id, str) or stage_id not in stages_by_id:
            raise WorkspaceError("source record references an unknown stage")
        if not isinstance(canonical_source_text, str):
            raise WorkspaceError("source record canonical path is invalid")
        try:
            canonical_source = resolved_under(Path(canonical_source_text), repository)
        except WorkspaceError as exc:
            raise WorkspaceError("source record must reference a repository file") from exc
        if str(canonical_source) != canonical_source_text or not canonical_source.is_file():
            raise WorkspaceError("source record path must be a canonical regular file")
        expected_repository_relative = canonical_source.relative_to(repository).as_posix()
        if relative_source_text != expected_repository_relative:
            raise WorkspaceError("source record repository-relative path is invalid")
        if type(order) is not int or order <= 0:
            raise WorkspaceError("source record order must be a positive integer")
        if not isinstance(created_at, str) or not created_at:
            raise WorkspaceError("source record created_at must be text")
        try:
            safe_label = _validate_label_component(label)
        except WorkspaceError as exc:
            raise WorkspaceError("source record label is invalid") from exc
        if not isinstance(relative_learning_text, str) or not relative_learning_text:
            raise WorkspaceError("source record learning directory is invalid")
        relative_learning = Path(relative_learning_text)
        if relative_learning.is_absolute() or ".." in relative_learning.parts:
            raise WorkspaceError("source record learning directory is unsafe")
        if relative_learning.parent.as_posix() != stage_id:
            raise WorkspaceError("source record is outside its stage")
        if relative_learning.name != f"{order:02d}-{safe_label}":
            raise WorkspaceError("source record directory name does not match its order and label")
        identity = (stage_id, canonical_source_text)
        if identity in source_identities:
            raise WorkspaceError("duplicate source identity in state")
        source_identities.add(identity)
        if relative_learning_text in source_paths:
            raise WorkspaceError("duplicate source learning directory in state")
        source_paths.add(relative_learning_text)
        orders = source_orders.setdefault(stage_id, set())
        if order in orders:
            raise WorkspaceError("duplicate source order in stage")
        orders.add(order)

        source_directory = root / relative_learning
        if source_directory.is_symlink() or not source_directory.is_dir():
            raise WorkspaceError("source learning directory is missing or invalid")
        try:
            canonical_directory = resolved_under(source_directory, root)
        except WorkspaceError as exc:
            raise WorkspaceError("source learning directory escapes the learning root") from exc
        if canonical_directory != source_directory:
            raise WorkspaceError("source learning directory must be canonical")
        expected_source_marker = _expected_source_marker(
            state["learning_root_id"],
            stage_id,
            canonical_source_text,
            relative_learning_text,
            order,
            safe_label,
        )
        try:
            actual_source_marker = read_json(source_directory / ".learning-source.json")
        except WorkspaceError as exc:
            raise WorkspaceError("source learning marker is missing or invalid") from exc
        if actual_source_marker != expected_source_marker:
            raise WorkspaceError("source learning marker does not match authoritative state")

    for stage_id, orders in source_orders.items():
        if orders != set(range(1, len(orders) + 1)):
            raise WorkspaceError(f"source orders are not consecutive in stage: {stage_id}")

    artifacts_seen: set[str] = set()
    artifact_identities_seen: set[tuple[int, int]] = set()
    source_by_owner_id = {
        f"source:{entry['relative_learning_dir']}": entry
        for entry in state["sources"]
    }
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
        if owner_kind not in {"source", "day-overview"}:
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

        if owner_kind == "source":
            source_owner = source_by_owner_id.get(owner_id)
            if source_owner is None:
                raise WorkspaceError("artifact references an unknown source owner")
            if relative_owner_text != source_owner["relative_learning_dir"]:
                raise WorkspaceError("artifact source owner directory does not match")
            owner_stage = stages_by_id[source_owner["stage_id"]]
            if owner_stage.get("status") not in {"active", "sealed"}:
                raise WorkspaceError("artifact source owner stage status is invalid")
            allowed_directory = root / relative_owner / "产物"
            expected_artifact = root / relative_owner / relative_artifact
            if not relative_artifact.parts or relative_artifact.parts[0] != "产物":
                raise WorkspaceError("source artifact must be inside its 产物 directory")
        else:
            prefix = "day-overview:"
            if not owner_id.startswith(prefix):
                raise WorkspaceError("day overview artifact owner ID is invalid")
            stage_id = owner_id[len(prefix) :]
            owner_stage = stages_by_id.get(stage_id)
            if owner_stage is None:
                raise WorkspaceError("artifact references an unknown day overview owner")
            if owner_stage.get("status") not in {"active", "sealed"}:
                raise WorkspaceError("artifact day owner stage status is invalid")
            expected_owner_text = f"{stage_id}/当日总览产物"
            if relative_owner_text != expected_owner_text:
                raise WorkspaceError("artifact day owner directory does not match")
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
    current_stage = current.get("stage")
    if active_days:
        active_day = active_days[0]
        if current_day != active_day["day"]:
            raise WorkspaceError("current Day does not match active Day")
        if len(active_stages) != 1:
            raise WorkspaceError("an active Day requires exactly one active stage")
        active_stage = active_stages[0]
        if current_stage != active_stage:
            raise WorkspaceError("current stage does not match active stage")
        if current.get("progress") != active_day["progress"]:
            raise WorkspaceError("current progress does not match active Day")
        if active_stage["day"] != active_day["day"]:
            raise WorkspaceError("active stage Day does not match the active Day")
        start, end = active_stage["range"]
        if not start <= active_day["progress"] <= end:
            raise WorkspaceError("active stage range does not contain current progress")
        expected_stage_path = (
            _day_directory(root, state, active_day["day"])
            / _stage_name(active_stage["range"], active_stage["revision"])
        )
        actual_stage_path = root / active_stage["path"]
        if actual_stage_path != expected_stage_path:
            raise WorkspaceError("active stage path does not match its Day, range, and revision")
        if actual_stage_path.is_symlink() or not actual_stage_path.is_dir():
            raise WorkspaceError("active stage directory is missing or invalid")
        try:
            resolved_stage_path = actual_stage_path.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceError("active stage directory is missing or invalid") from exc
        if resolved_stage_path != actual_stage_path or not _is_within(resolved_stage_path, root):
            raise WorkspaceError("active stage directory escapes the learning root")
        try:
            active_marker = read_json(actual_stage_path / ".learning-stage.json")
        except WorkspaceError as exc:
            raise WorkspaceError("active stage marker is missing or invalid") from exc
        expected_active_marker = _expected_stage_marker(
            root_id,
            active_stage["day"],
            active_stage["range"],
            active_stage["revision"],
        )
        if active_marker != expected_active_marker:
            raise WorkspaceError("active stage marker does not match authoritative state")
    else:
        if current_day is not None or current_stage is not None or active_stages:
            raise WorkspaceError("inactive state cannot contain a current Day or active stage")
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


def _stage_name(bounds: list[int], revision: int = 0) -> str:
    name = f"进度条{bounds[0]}-{bounds[1]}%"
    if revision:
        name += f"-修订{revision:02d}"
    return name


def _expected_stage_marker(
    learning_root_id: str, day: int, bounds: list[int], revision: int
) -> dict:
    return {
        "kind": STAGE_MARKER_KIND,
        "marker_version": STAGE_MARKER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "learning_root_id": learning_root_id,
        "day": day,
        "range": list(bounds),
        "revision": revision,
    }


def _expected_source_marker(
    learning_root_id: str,
    stage_id: str,
    canonical_source_path: str,
    relative_learning_dir: str,
    order: int,
    label: str,
) -> dict:
    return {
        "kind": SOURCE_MARKER_KIND,
        "marker_version": SOURCE_MARKER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "learning_root_id": learning_root_id,
        "stage_id": stage_id,
        "canonical_source_path": canonical_source_path,
        "relative_learning_dir": relative_learning_dir,
        "order": order,
        "label": label,
    }


def active_stage_record(state: dict, stage: Path) -> dict:
    root = Path(state["learning_root"])
    try:
        stage_id = Path(stage).relative_to(root).as_posix()
    except ValueError as exc:
        raise WorkspaceError("stage is outside the learning root") from exc
    matches = [entry for entry in state["stages"] if entry.get("path") == stage_id]
    if len(matches) != 1 or matches[0].get("status") != "active":
        raise WorkspaceError("source registration requires the active stage")
    if state["current"].get("stage") != matches[0]:
        raise WorkspaceError("stage does not match the current active stage")
    return matches[0]


def source_label(source: Path, repo: Path, requested: str | None, used: set[str]) -> str:
    label = _validate_label_component(requested if requested is not None else source.name)
    if label not in used:
        return label

    repository_relative = source.relative_to(repo)
    parent_suffix = "-".join(repository_relative.parent.parts) or "root"
    parent_suffix = _validate_label_component(parent_suffix)
    suffix = source.suffix
    stem = label[: -len(suffix)] if suffix and label.endswith(suffix) else label
    candidate = _validate_label_component(f"{stem}-{parent_suffix}{suffix}")
    if candidate not in used:
        return candidate
    stable_id = hashlib.sha256(repository_relative.as_posix().encode("utf-8")).hexdigest()[:8]
    candidate = _validate_label_component(
        f"{stem}-{parent_suffix}-{stable_id}{suffix}"
    )
    if candidate not in used:
        return candidate
    raise WorkspaceError("source label collides with an existing source in this stage")


def _owned_source_directory(path: Path, expected_marker: dict) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        marker = read_json(path / ".learning-source.json")
    except WorkspaceError:
        return False
    return marker == expected_marker


def _remove_owned_source(root: Path, path: Path, expected_marker: dict) -> None:
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
    if _owned_source_directory(path, expected_marker):
        shutil.rmtree(path)


def source_creation_seam(stage: Path) -> None:
    """Test seam immediately before fd-relative source-directory creation."""


def source_temp_creation_seam(stage_fd: int, temp_name: str, temp_fd: int) -> None:
    """Test seam after opening a generated temp directory by stable identity."""


def _require_source_dir_fd_support() -> None:
    required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir)
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        raise WorkspaceError(
            "secure add-source requires dir_fd operations on this platform"
        )
    if os.listdir not in os.supports_fd:
        raise WorkspaceError(
            "secure add-source requires directory-fd listing on this platform"
        )


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


def _write_text_at(directory_fd: int, name: str, content: str) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        payload = content.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_no_replace_at(directory_fd: int, source_name: str, target_name: str) -> None:
    source = os.fsencode(source_name)
    target = os.fsencode(target_name)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename = libc.renameatx_np
        except AttributeError as exc:
            raise WorkspaceError("atomic fd-relative publication is unavailable") from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(directory_fd, source, directory_fd, target, 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise WorkspaceError("atomic fd-relative publication is unavailable") from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(directory_fd, source, directory_fd, target, 1)
    else:
        raise WorkspaceError("atomic fd-relative publication is unavailable")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise WorkspaceError(f"source target already exists: {target_name}")
    if error_number in {errno.ENOSYS, errno.ENOTSUP}:
        raise WorkspaceError("atomic fd-relative publication is unavailable")
    raise OSError(error_number, os.strerror(error_number), target_name)


def _stable_stage_matches(
    directory_fd: int, stage: Path, root: Path, expected_marker: dict
) -> bool:
    try:
        if stage.is_symlink() or stage.resolve(strict=True) != stage:
            return False
        stage.relative_to(root)
        path_stat = os.stat(stage, follow_symlinks=False)
        if not os.path.samestat(os.fstat(directory_fd), path_stat):
            return False
        return _read_json_at(directory_fd, ".learning-stage.json") == expected_marker
    except (OSError, ValueError, WorkspaceError):
        return False


def _source_entry_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _remove_generated_source_at(
    stage_fd: int,
    name: str,
    expected_marker: dict | None,
    *,
    allow_partial: bool = False,
) -> bool:
    try:
        source_fd = _open_directory_at(stage_fd, name)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False
    try:
        return _remove_generated_source_handle_at(
            stage_fd,
            name,
            source_fd,
            expected_marker,
            allow_partial=allow_partial,
        )
    finally:
        os.close(source_fd)


def _entry_matches_fd(parent_fd: int, name: str, child_fd: int) -> bool:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return stat.S_ISDIR(entry_stat.st_mode) and os.path.samestat(
            os.fstat(child_fd), entry_stat
        )
    except OSError:
        return False


def _remove_generated_source_handle_at(
    stage_fd: int,
    name: str,
    source_fd: int,
    expected_marker: dict | None,
    *,
    allow_partial: bool = False,
) -> bool:
    if expected_marker is not None:
        try:
            marker = _read_json_at(source_fd, ".learning-source.json")
        except WorkspaceError:
            return False
        if marker != expected_marker:
            return False
    elif not allow_partial:
        return False
    if not _entry_matches_fd(stage_fd, name, source_fd):
        return False
    _clear_directory_at(source_fd)
    if not _entry_matches_fd(stage_fd, name, source_fd):
        return False
    os.rmdir(name, dir_fd=stage_fd)
    return True


def _clear_directory_at(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory_at(directory_fd, name)
            try:
                if not _entry_matches_fd(directory_fd, name, child_fd):
                    raise WorkspaceError(
                        "generated child directory identity changed during cleanup"
                    )
                _clear_directory_at(child_fd)
                if not _entry_matches_fd(directory_fd, name, child_fd):
                    raise WorkspaceError(
                        "generated child directory identity changed during cleanup"
                    )
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _create_source_directory_at(
    stage_fd: int,
    target_name: str,
    expected_marker: dict,
    guide: str,
) -> int:
    temporary_name = f".{target_name}.tmp-{uuid.uuid4().hex}"
    os.mkdir(temporary_name, 0o700, dir_fd=stage_fd)
    temporary_fd = _open_directory_at(stage_fd, temporary_name)
    try:
        source_temp_creation_seam(stage_fd, temporary_name, temporary_fd)
        _write_text_at(
            temporary_fd,
            ".learning-source.json",
            json.dumps(expected_marker, ensure_ascii=False, indent=2) + "\n",
        )
        _write_text_at(temporary_fd, "源码导读.md", guide)
        os.mkdir("产物", 0o755, dir_fd=temporary_fd)
        os.fsync(temporary_fd)
    except BaseException as exc:
        cleaned = _remove_generated_source_handle_at(
            stage_fd,
            temporary_name,
            temporary_fd,
            None,
            allow_partial=True,
        )
        os.close(temporary_fd)
        if not cleaned:
            raise WorkspaceError(
                "generated temp identity changed; residue was preserved safely"
            ) from exc
        raise
    try:
        _rename_no_replace_at(stage_fd, temporary_name, target_name)
    except BaseException as exc:
        cleaned = _remove_generated_source_handle_at(
            stage_fd,
            temporary_name,
            temporary_fd,
            expected_marker,
            allow_partial=False,
        )
        os.close(temporary_fd)
        if not cleaned:
            raise WorkspaceError(
                "generated temp identity changed; residue was preserved safely"
            ) from exc
        raise
    return temporary_fd


def _owned_stage_directory(path: Path, expected_marker: dict) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        marker = read_json(path / ".learning-stage.json")
    except WorkspaceError:
        return False
    return all(marker.get(key) == value for key, value in expected_marker.items())


def _remove_owned_stage(root: Path, path: Path, expected_marker: dict) -> None:
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
    if _owned_stage_directory(path, expected_marker):
        shutil.rmtree(path)


def _prepare_stage_target(root: Path, state: dict, day: int, bounds: list[int], revision: int) -> Path:
    day_directory = _validated_day_directory(root, state, day)
    target = day_directory / _stage_name(bounds, revision)
    expected_marker = _expected_stage_marker(
        state["learning_root_id"], day, bounds, revision
    )
    registered = {root / stage["path"] for stage in state["stages"]}
    if target in registered:
        raise WorkspaceError(f"stage is already registered: {target}")
    if target.exists() or target.is_symlink():
        if _owned_stage_directory(target, expected_marker):
            _remove_owned_stage(root, target, expected_marker)
            if target.exists() or target.is_symlink():
                raise WorkspaceError(f"stage target could not be safely cleaned: {target}")
        else:
            raise WorkspaceError(f"stage target is occupied by an unknown directory: {target}")
    return target


def create_stage(root: Path, day: int, bounds: list[int], revision: int = 0) -> Path:
    root = Path(root).resolve(strict=True)
    marker = read_json(root / ".learning-root.json")
    root_id = marker.get("learning_root_id")
    if not isinstance(root_id, str):
        raise WorkspaceError("learning-root marker has no valid ID")
    state = read_json(root / "学习状态.json")
    day_directory = _validated_day_directory(root, state, day)
    target = day_directory / _stage_name(bounds, revision)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=day_directory))
    try:
        atomic_write_json(
            temporary / ".learning-stage.json",
            _expected_stage_marker(root_id, day, bounds, revision),
        )
        day_state = state["days"][day - 1]
        checkpoint_text = "、".join(
            f"{checkpoint['id']}:{checkpoint['title']}({checkpoint['weight']}%)"
            for checkpoint in day_state["checkpoints"]
        )
        common = {
            "TIMESTAMP": _utc_timestamp(),
            "DAY_NUMBER": day,
            "STAGE_RANGE": f"{bounds[0]}-{bounds[1]}%",
            "CHECKPOINTS": checkpoint_text,
        }
        materialize_template(
            "questions-and-answers.md",
            temporary / "问题与解答.md",
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
            temporary / "当日总结.md",
            {
                **common,
                "CLOSED_FLOWS": "暂无",
                "KEY_CONCEPTS": "待整理",
                "REMAINING_QUESTIONS": "无",
                "CONTINUATION_POINT": "待设置",
            },
        )
        overview_directory = temporary / "当日总览产物"
        overview_directory.mkdir()
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
        _rename_no_replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def _stage_record(root: Path, path: Path, day: int, bounds: list[int], revision: int) -> dict:
    return {
        "day": day,
        "range": list(bounds),
        "revision": revision,
        "path": path.relative_to(root).as_posix(),
        "status": "active",
        "created_at": _utc_timestamp(),
    }


def _seal_active_stage(state: dict) -> None:
    active = [stage for stage in state["stages"] if stage["status"] == "active"]
    if len(active) != 1:
        raise WorkspaceError("expected exactly one active stage")
    active[0]["status"] = "sealed"
    active[0]["sealed_at"] = _utc_timestamp()


def _state_was_committed(root: Path, state: dict) -> bool:
    try:
        return (root / "学习状态.json").read_text(encoding="utf-8") == _state_text(state)
    except (OSError, UnicodeError):
        return False


def _save_with_created_stage(root: Path, state: dict, created: Path) -> None:
    try:
        save_state(root, state)
    except BaseException:
        if not _state_was_committed(root, state):
            relative_created = created.relative_to(root).as_posix()
            created_record = next(
                (
                    stage
                    for stage in state["stages"]
                    if stage.get("path") == relative_created
                ),
                None,
            )
            if created_record is not None:
                expected_marker = _expected_stage_marker(
                    state["learning_root_id"],
                    created_record["day"],
                    created_record["range"],
                    created_record["revision"],
                )
                _remove_owned_stage(root, created, expected_marker)
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


def _target_stage_revision(state: dict, day: int, bounds: list[int]) -> int:
    revisions = {
        stage["revision"]
        for stage in state["stages"]
        if stage["day"] == day and stage["range"] == bounds
    }
    if 0 not in revisions:
        return 0
    return max(revisions) + 1


def _append_history(state: dict, event: str, **details: Any) -> None:
    state["history"].append({"event": event, "at": _utc_timestamp(), **details})


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

    bounds = stage_range_for(0, state["stage_ranges"])
    revision = _target_stage_revision(state, args.day, bounds)
    target = _prepare_stage_target(root, state, args.day, bounds, revision)
    created = create_stage(root, args.day, bounds, revision)
    if created != target:
        raise WorkspaceError("created stage path does not match target")

    target_day["status"] = "active"
    target_day["progress"] = 0
    stage = _stage_record(root, created, args.day, bounds, revision)
    state["stages"].append(stage)
    state["current"].update(
        {
            "day": args.day,
            "stage": copy.deepcopy(stage),
            "progress": 0,
            "last_source": None,
            "resume_marker": "",
            "closed_flows": [],
        }
    )
    _append_history(state, "start-day", day=args.day)
    _save_with_created_stage(root, state, created)
    return root


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
    target_bounds = stage_range_for(progress, state["stage_ranges"])
    current_bounds = state["current"]["stage"]["range"]
    created: Path | None = None
    if target_bounds != current_bounds:
        revision = _target_stage_revision(state, day["day"], target_bounds)
        target = _prepare_stage_target(
            root, state, day["day"], target_bounds, revision
        )
        created = create_stage(root, day["day"], target_bounds, revision)
        if created != target:
            raise WorkspaceError("created stage path does not match target")
        _seal_active_stage(state)
        stage = _stage_record(root, created, day["day"], target_bounds, revision)
        state["stages"].append(stage)
        state["current"]["stage"] = copy.deepcopy(stage)
    _append_history(
        state,
        "complete-checkpoint",
        day=day["day"],
        checkpoint=args.checkpoint,
        progress=progress,
    )
    if created is None:
        save_state(root, state)
    else:
        _save_with_created_stage(root, state, created)
    return root


def _next_revision(state: dict, day: int, bounds: list[int]) -> int:
    revisions = [
        stage["revision"]
        for stage in state["stages"]
        if stage["day"] == day and stage["range"] == bounds
    ]
    return max(revisions, default=0) + 1


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
    bounds = stage_range_for(progress, state["stage_ranges"])
    revision = _next_revision(state, day["day"], bounds)
    target = _prepare_stage_target(root, state, day["day"], bounds, revision)
    created = create_stage(root, day["day"], bounds, revision)
    if created != target:
        raise WorkspaceError("created stage path does not match target")
    _seal_active_stage(state)
    stage = _stage_record(root, created, day["day"], bounds, revision)
    state["stages"].append(stage)
    state["current"]["stage"] = copy.deepcopy(stage)
    _append_history(
        state,
        "reopen-checkpoint",
        day=day["day"],
        checkpoint=args.checkpoint,
        reason=args.reason.strip(),
        progress=progress,
    )
    _save_with_created_stage(root, state, created)
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


def command_add_source(args: argparse.Namespace) -> Path:
    stage_argument = Path(args.stage).expanduser()
    if not stage_argument.is_absolute():
        stage_argument = Path.cwd() / stage_argument
    stage_argument = Path(os.path.abspath(stage_argument))
    if stage_argument.is_symlink():
        raise WorkspaceError("stage must not be a symlink")
    try:
        stage = stage_argument.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"stage does not exist: {stage_argument}") from exc
    if not stage.is_dir():
        raise WorkspaceError("stage is not a directory")

    try:
        root = stage_argument.parent.parent.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("cannot locate the learning root for stage") from exc
    state = load_state(root)
    stage_record = active_stage_record(state, stage)
    stage_id = stage_record["path"]
    repository = Path(state["repository"])
    try:
        source = resolved_under(Path(args.source), repository)
    except WorkspaceError as exc:
        raise WorkspaceError("source must be a regular file inside the repository") from exc
    if not source.is_file():
        raise WorkspaceError("source must be a regular file inside the repository")

    existing = next(
        (
            entry
            for entry in state["sources"]
            if entry.get("stage_id") == stage_id
            and entry.get("canonical_source_path") == str(source)
        ),
        None,
    )
    if existing is not None:
        return root / existing["relative_learning_dir"]

    _require_source_dir_fd_support()

    stage_sources = [
        entry for entry in state["sources"] if entry.get("stage_id") == stage_id
    ]
    order = len(stage_sources) + 1
    label = source_label(
        source,
        repository,
        getattr(args, "label", None),
        {entry["label"] for entry in stage_sources},
    )
    target = stage / f"{order:02d}-{label}"
    relative_learning_dir = target.relative_to(root).as_posix()
    expected_marker = _expected_source_marker(
        state["learning_root_id"],
        stage_id,
        str(source),
        relative_learning_dir,
        order,
        label,
    )
    guide = render_named_template(
        "source-guide.md",
        {
            "SOURCE_LABEL": label,
            "CANONICAL_SOURCE_PATH": source,
            "REPOSITORY_RELATIVE_PATH": source.relative_to(repository).as_posix(),
            "STAGE_ID": stage_id,
            "SYMBOLS_LINES": "待补充",
            "CALLER": "待确认",
            "CALLEE": "待确认",
            "CURRENT_UNDERSTANDING": "待整理",
            "NEXT_HOP": "待确认",
            "RELATED_SOURCES": "暂无",
            "ARTIFACTS": "- 暂无已登记产物",
        },
    )
    open_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        stage_fd = os.open(stage, open_flags)
    except OSError as exc:
        raise WorkspaceError("active stage changed before source registration") from exc
    expected_stage_marker = _expected_stage_marker(
        state["learning_root_id"],
        stage_record["day"],
        stage_record["range"],
        stage_record["revision"],
    )
    created = False
    published_fd: int | None = None
    try:
        if not _stable_stage_matches(stage_fd, stage, root, expected_stage_marker):
            raise WorkspaceError("active stage changed before source registration")
        if _source_entry_exists_at(stage_fd, target.name):
            if not _remove_generated_source_at(
                stage_fd, target.name, expected_marker, allow_partial=False
            ):
                raise WorkspaceError(
                    f"source target is occupied by an unknown directory: {target}"
                )
        source_creation_seam(stage)
        if not _stable_stage_matches(stage_fd, stage, root, expected_stage_marker):
            raise WorkspaceError("active stage changed before source directory creation")
        published_fd = _create_source_directory_at(
            stage_fd, target.name, expected_marker, guide
        )
        created = True
        if (
            not _stable_stage_matches(stage_fd, stage, root, expected_stage_marker)
            or not _entry_matches_fd(stage_fd, target.name, published_fd)
        ):
            cleaned = _remove_generated_source_handle_at(
                stage_fd,
                target.name,
                published_fd,
                expected_marker,
                allow_partial=False,
            )
            created = False
            if not cleaned:
                raise WorkspaceError(
                    "generated source identity changed; residue was preserved safely"
                )
            raise WorkspaceError("active stage changed during source directory creation")

        record = {
            "stage_id": stage_id,
            "canonical_source_path": str(source),
            "repository_relative_path": source.relative_to(repository).as_posix(),
            "relative_learning_dir": relative_learning_dir,
            "order": order,
            "label": label,
            "created_at": _utc_timestamp(),
        }
        state["sources"].append(record)
        _append_history(
            state,
            "add-source",
            day=stage_record["day"],
            stage_id=stage_id,
            source=str(source),
            directory=relative_learning_dir,
        )
        try:
            save_state(root, state)
        except BaseException as exc:
            if created and not _state_was_committed(root, state):
                cleaned = _remove_generated_source_handle_at(
                    stage_fd,
                    target.name,
                    published_fd,
                    expected_marker,
                    allow_partial=False,
                )
                if not cleaned:
                    raise WorkspaceError(
                        "authority commit failed after generated source identity changed; "
                        "residue was preserved safely"
                    ) from exc
            raise
    finally:
        if published_fd is not None:
            os.close(published_fd)
        os.close(stage_fd)
    return target


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
    owner_stage: dict | None = None
    if owner_kind == "source":
        try:
            owner_relative_text = owner.relative_to(root).as_posix()
        except ValueError as exc:
            raise WorkspaceError(
                "source artifact owner is outside the learning root"
            ) from exc
        matches = [
            entry
            for entry in state["sources"]
            if entry.get("relative_learning_dir")
            == owner_relative_text
        ]
        if len(matches) != 1:
            raise WorkspaceError(
                "source artifact owner must be a registered source"
            )
        relative_owner = matches[0]["relative_learning_dir"]
        owner_stage = next(
            (
                stage
                for stage in state["stages"]
                if stage.get("path") == matches[0].get("stage_id")
            ),
            None,
        )
        if owner_stage is None:
            raise WorkspaceError("source artifact owner references an unknown stage")
        owner_id = f"source:{relative_owner}"
        allowed_directory = owner / "产物"
        owner_markdown = owner / "源码导读.md"
        markdown_name = "源码导读.md"
    elif owner_kind == "day-overview":
        owner_stage_matches = [
            stage
            for stage in state["stages"]
            if owner == root / stage["path"] / "当日总览产物"
        ]
        if len(owner_stage_matches) != 1:
            raise WorkspaceError(
                "day overview owner must belong to a registered stage"
            )
        owner_stage = owner_stage_matches[0]
        owner_stage_id = owner_stage["path"]
        relative_owner = owner.relative_to(root).as_posix()
        owner_id = f"day-overview:{owner_stage_id}"
        allowed_directory = owner
        owner_markdown = owner / "当日统一总结.md"
        markdown_name = "当日统一总结.md"
    else:
        raise WorkspaceError("owner kind must be source or day-overview")

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
        if owner_kind == "source":
            allowed_fd = _open_directory_at(owner_fd, "产物")
        else:
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
        owner_is_active = (
            owner_stage is not None
            and owner_stage.get("status") == "active"
            and state["current"].get("stage") == owner_stage
            and any(
                day.get("day") == owner_stage.get("day")
                and day.get("status") == "active"
                for day in state["days"]
            )
        )
        if is_new and not owner_is_active:
            raise WorkspaceError(
                "new artifact registration requires an owner in the active stage"
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
        if (
            not _stable_path_matches_fd(owner, owner_fd, directory=True)
            or not _entry_matches_metadata(
                owner_fd, markdown_name, markdown_metadata, regular=True
            )
            or not _entry_matches_metadata(
                artifact_parent_fd,
                artifact_name,
                artifact_metadata,
                regular=True,
            )
            or not _stable_path_matches_fd(artifact, artifact_fd, directory=False)
            or _read_utf8_fd(markdown_fd, "owner Markdown") != markdown
        ):
            raise WorkspaceError("artifact registration inputs changed before commit")

        if is_new:
            replace_authority_json(root / "学习状态.json", candidate_json)
        try:
            artifact_post_authority_seam(root, candidate, owner_markdown, artifact)
            if (
                not _stable_path_matches_fd(owner, owner_fd, directory=True)
                or not _entry_matches_metadata(
                    owner_fd, markdown_name, markdown_metadata, regular=True
                )
                or _read_utf8_fd(markdown_fd, "owner Markdown") != markdown
                or not _entry_matches_metadata(
                    artifact_parent_fd,
                    artifact_name,
                    artifact_metadata,
                    regular=True,
                )
                or not _stable_path_matches_fd(
                    artifact, artifact_fd, directory=False
                )
            ):
                raise WorkspaceError(
                    "artifact registration inputs changed after authority commit"
                )
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
    if sum(stage["status"] == "active" for stage in state["stages"]) != 1:
        raise WorkspaceError("completing a Day requires exactly one active stage")
    _seal_active_stage(state)
    day["status"] = "completed"
    day["progress"] = 100
    state["current"].update(
        {
            "day": None,
            "stage": None,
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
            (temporary_root / f"Day{day['day']:0{day_width}d}").mkdir()

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

    source_parser = subparsers.add_parser(
        "add-source", help="register a repository source in the active stage"
    )
    source_parser.add_argument("--stage", required=True)
    source_parser.add_argument("--source", required=True)
    source_parser.add_argument("--label")
    source_parser.set_defaults(handler=command_add_source)

    artifact_parser = subparsers.add_parser(
        "register-artifact", help="register a learning artifact with one active owner"
    )
    artifact_parser.add_argument("--learning-root", required=True)
    artifact_parser.add_argument(
        "--owner-kind", required=True, choices=("source", "day-overview")
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
