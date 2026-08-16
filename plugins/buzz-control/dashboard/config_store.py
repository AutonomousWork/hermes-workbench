"""Protected, FastAPI-free storage for the Buzz production environment.

This module is deliberately unaware of HTTP and Docker.  It owns the bounded
dotenv grammar, disclosure policy, protected snapshots, opaque revisions, and
the cross-process configuration lock shared by every caller.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, NamedTuple
from urllib.parse import urlsplit


MAX_CONFIG_BYTES = 128 * 1024
MAX_FIELD_BYTES = 16 * 1024
MAX_JOURNAL_BYTES = 32 * 1024
STALE_TEMP_SECONDS = 5 * 60


class Disclosure(str, Enum):
    PLAIN = "plain"
    WRITE_ONLY = "write_only"
    SERVER_MANAGED = "server_managed"


class ImpactClass(str, Enum):
    RELAY_ELIGIBLE = "relay_eligible"
    RELAY_HIGH_IMPACT = "relay_high_impact"
    MANUAL_MAINTENANCE = "manual_maintenance"
    SERVER_MANAGED = "server_managed"
    UNKNOWN_MANUAL_ONLY = "unknown_manual_only"


class FieldSpec(NamedTuple):
    label: str
    group: str
    disclosure: Disclosure
    impact: ImpactClass
    editable: bool
    value_kind: str


def _field(
    label: str,
    *,
    group: str = "public_address",
    kind: str = "text",
    disclosure: Disclosure = Disclosure.PLAIN,
    impact: ImpactClass = ImpactClass.RELAY_ELIGIBLE,
    editable: bool = True,
) -> FieldSpec:
    return FieldSpec(label, group, disclosure, impact, editable, kind)


_MANUAL = ImpactClass.MANUAL_MAINTENANCE

# This catalog is intentionally small and stable. The dashboard manages only
# operator-facing endpoint and access settings; every other assignment remains
# byte-preserved in the protected file and must be maintained outside Hermes.
FIELD_CATALOG: dict[str, FieldSpec] = {
    "BUZZ_ALLOW_NIP_OA_AUTH": _field(
        "Allow NIP-98 owner attestation",
        group="access_policy",
        kind="bool",
        impact=ImpactClass.RELAY_HIGH_IMPACT,
    ),
    "BUZZ_CORS_ORIGINS": _field("CORS origins"),
    "BUZZ_DOMAIN": _field("Buzz domain", kind="host"),
    "BUZZ_MEDIA_BASE_URL": _field(
        "Media base URL", kind="url_http_optional"
    ),
    "BUZZ_MEDIA_SERVER_DOMAIN": _field("Media server domain", kind="host_optional"),
    "BUZZ_REQUIRE_AUTH_TOKEN": _field(
        "Require authentication token",
        group="access_policy",
        kind="bool",
        impact=ImpactClass.RELAY_HIGH_IMPACT,
    ),
    "BUZZ_REQUIRE_RELAY_MEMBERSHIP": _field(
        "Require relay membership",
        group="access_policy",
        kind="bool",
        impact=ImpactClass.RELAY_HIGH_IMPACT,
    ),
    "RELAY_OWNER_PUBKEY": _field(
        "Relay owner public key",
        group="owner_identity",
        impact=_MANUAL,
        editable=False,
    ),
    "RELAY_URL": _field("Relay URL", kind="url_ws"),
}

# Runtime probes need to read these deployment-owned values without exposing or
# accepting them through the browser configuration contract.
_RUNTIME_FIELD_CATALOG: dict[str, FieldSpec] = {
    "BUZZ_HTTP_PORT": _field("Buzz HTTP port", kind="port", editable=False),
}


_SAFE_MESSAGES = {
    "baseline_missing": "An applied configuration baseline is required.",
    "busy": "Another Buzz operation is in progress.",
    "document_too_large": "The configuration document exceeds the supported size.",
    "field_not_editable": "The requested field cannot be edited here.",
    "invalid_document": "The configuration requires manual repair.",
    "invalid_journal": "The operation state is not valid.",
    "invalid_intent": "The operation confirmation is not valid.",
    "invalid_value": "A configuration value is not valid.",
    "stale_revision": "The configuration changed; reload before saving.",
    "recovery_required": "An interrupted Buzz operation requires recovery.",
    "unsafe_storage": "The protected configuration storage is not safe.",
}


class ConfigStoreError(RuntimeError):
    """A stable, non-secret failure suitable for translation at the API edge."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_SAFE_MESSAGES.get(code, "The configuration operation failed."))


class _Line(NamedTuple):
    raw: bytes
    key: str | None
    prefix: bytes
    separator: bytes
    value: bytes | None
    ending: bytes


_ASSIGNMENT = re.compile(
    rb"(?P<prefix>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<separator>[ \t]*=[ \t]*)(?P<value>[^\r\n]*)\Z"
)
_COMMENT_OR_BLANK = re.compile(rb"[ \t]*(?:#[^\r\n]*)?\Z")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_OPAQUE_REVISION = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")


def _value_has_unsupported_continuation(value: bytes) -> bool:
    stripped = value.rstrip(b" \t")
    if stripped.endswith(b"\\"):
        return True
    quote: int | None = None
    escaped = False
    for character in value:
        if escaped:
            escaped = False
            continue
        if character == ord("\\"):
            escaped = True
        elif quote is None and character in (ord("'"), ord('"')):
            quote = character
        elif quote == character:
            quote = None
    return quote is not None


def _logical_dotenv_bytes(value: bytes | None) -> bytes:
    """Return Compose's logical value while retaining the raw line elsewhere."""

    raw = value or b""
    if not raw:
        return raw
    if raw[:1] in {b"'", b'"'}:
        quote = raw[0]
        escaped = False
        closing: int | None = None
        for index in range(1, len(raw)):
            character = raw[index]
            if escaped:
                escaped = False
                continue
            if character == ord("\\"):
                escaped = True
                continue
            if character == quote:
                closing = index
                break
        if closing is None:
            raise ConfigStoreError("invalid_document")
        remainder = raw[closing + 1 :]
        if remainder and not re.fullmatch(rb"[ \t]+(?:#[^\r\n]*)?", remainder):
            raise ConfigStoreError("invalid_document")
        return raw[: closing + 1]

    comment = re.search(rb"[ \t]+#", raw)
    if comment is not None:
        raw = raw[: comment.start()].rstrip(b" \t")
    if b"'" in raw or b'"' in raw:
        # Compose supports additional concatenation forms, but accepting one
        # with different semantics would be worse than requiring repair.
        raise ConfigStoreError("invalid_document")
    return raw


def _decode_dotenv_value(value: bytes | None) -> str:
    raw = _logical_dotenv_bytes(value)
    if len(raw) >= 2 and raw.startswith(b"'") and raw.endswith(b"'"):
        inner = raw[1:-1]
        decoded = bytearray()
        index = 0
        while index < len(inner):
            if (
                inner[index] == ord("\\")
                and index + 1 < len(inner)
                and inner[index + 1] == ord("'")
            ):
                decoded.append(ord("'"))
                index += 2
                continue
            decoded.append(inner[index])
            index += 1
        raw = bytes(decoded)
    elif len(raw) >= 2 and raw.startswith(b'"') and raw.endswith(b'"'):
        inner = raw[1:-1]
        decoded = bytearray()
        index = 0
        while index < len(inner):
            if inner[index : index + 2] == b"$$":
                decoded.append(ord("$"))
                index += 2
                continue
            if (
                inner[index] == ord("\\")
                and index + 1 < len(inner)
                and inner[index + 1] in (ord("\\"), ord('"'))
            ):
                decoded.append(inner[index + 1])
                index += 2
                continue
            decoded.append(inner[index])
            index += 1
        raw = bytes(decoded)
    return raw.decode("utf-8", errors="strict")


def _encode_dotenv_value(value: str) -> bytes:
    encoded = value.encode("utf-8", errors="strict")
    if encoded.endswith(b"\\"):
        escaped = (
            encoded.replace(b"\\", b"\\\\")
            .replace(b'"', b'\\"')
            .replace(b"$", b"$$")
        )
        return b'"' + escaped + b'"'
    return b"'" + encoded.replace(b"'", b"\\'") + b"'"


def _validate_value(value: str, spec: FieldSpec) -> None:
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ConfigStoreError("invalid_value") from exc
    if (
        len(encoded) > MAX_FIELD_BYTES
        or b"\x00" in encoded
        or b"\n" in encoded
        or b"\r" in encoded
    ):
        raise ConfigStoreError("invalid_value")
    if not value and spec.value_kind in {"host_optional", "url_http_optional"}:
        return
    if spec.value_kind == "bool" and value not in {"true", "false"}:
        raise ConfigStoreError("invalid_value")
    if spec.value_kind == "port":
        try:
            port = int(value)
        except ValueError as exc:
            raise ConfigStoreError("invalid_value") from exc
        if str(port) != value or not 1 <= port <= 65535:
            raise ConfigStoreError("invalid_value")
    if spec.value_kind in {"url_ws", "url_http_optional"}:
        parsed = urlsplit(value)
        allowed = {"ws", "wss"} if spec.value_kind == "url_ws" else {"http", "https"}
        if (
            parsed.scheme not in allowed
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ConfigStoreError("invalid_value")
    if spec.value_kind in {"host", "host_optional"}:
        if value and (
            len(value) > 253
            or any(character.isspace() for character in value)
            or "/" in value
        ):
            raise ConfigStoreError("invalid_value")


class DotenvDocument:
    """A conservative, byte-preserving representation of supported dotenv."""

    def __init__(self, lines: tuple[_Line, ...]):
        self._lines = lines
        self._by_key = {
            line.key: index
            for index, line in enumerate(lines)
            if line.key is not None
        }

    @classmethod
    def parse(cls, raw: bytes) -> "DotenvDocument":
        if len(raw) > MAX_CONFIG_BYTES:
            raise ConfigStoreError("document_too_large")
        if b"\x00" in raw:
            raise ConfigStoreError("invalid_document")
        try:
            raw.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ConfigStoreError("invalid_document") from exc
        if re.search(rb"\r(?!\n)", raw):
            raise ConfigStoreError("invalid_document")

        lines: list[_Line] = []
        seen: set[str] = set()
        for complete in raw.splitlines(keepends=True):
            if complete.endswith(b"\r\n"):
                body, ending = complete[:-2], b"\r\n"
            elif complete.endswith(b"\n"):
                body, ending = complete[:-1], b"\n"
            else:
                body, ending = complete, b""
            if _COMMENT_OR_BLANK.fullmatch(body):
                lines.append(_Line(complete, None, b"", b"", None, ending))
                continue
            match = _ASSIGNMENT.fullmatch(body)
            if match is None:
                raise ConfigStoreError("invalid_document")
            key = match.group("key").decode("ascii")
            value = match.group("value")
            logical_value = _logical_dotenv_bytes(value)
            if key in seen or _value_has_unsupported_continuation(logical_value):
                raise ConfigStoreError("invalid_document")
            seen.add(key)
            lines.append(
                _Line(
                    complete,
                    key,
                    match.group("prefix"),
                    match.group("separator"),
                    value,
                    ending,
                )
            )
        return cls(tuple(lines))

    def render(self) -> bytes:
        return b"".join(line.raw for line in self._lines)

    def value(self, name: str) -> str | None:
        index = self._by_key.get(name)
        if index is None:
            return None
        return _decode_dotenv_value(self._lines[index].value)

    def project(self) -> tuple[dict[str, Any], ...]:
        fields: list[dict[str, Any]] = []
        present = set(self._by_key)
        for name, spec in FIELD_CATALOG.items():
            line = self._lines[self._by_key[name]] if name in present else None
            projection: dict[str, Any] = {
                "name": name,
                "label": spec.label,
                "group": spec.group,
                "configured": bool(line is not None and self.value(name)),
                "disclosure": spec.disclosure.value,
                "impact": spec.impact.value,
                "editable": spec.editable,
                "kind": spec.value_kind,
            }
            logical_value = self.value(name) if line is not None else None
            contains_url_secrets = False
            if logical_value and spec.value_kind in {"url_ws", "url_http_optional"}:
                parsed = urlsplit(logical_value)
                contains_url_secrets = bool(
                    parsed.username is not None
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
                )
            if contains_url_secrets:
                projection["disclosure"] = Disclosure.WRITE_ONLY.value
                projection["editable"] = False
            elif line is not None and spec.disclosure != Disclosure.WRITE_ONLY:
                projection["value"] = logical_value or ""
            fields.append(projection)
        return tuple(fields)

    def changed_keys_from(self, other: "DotenvDocument") -> tuple[str, ...]:
        current = {
            key: self._lines[index].value for key, index in self._by_key.items()
        }
        baseline = {
            key: other._lines[index].value for key, index in other._by_key.items()
        }
        return tuple(
            sorted(
                key
                for key in set(current) | set(baseline)
                if current.get(key) != baseline.get(key)
            )
        )

    def apply(
        self, replacements: Mapping[str, str | None]
    ) -> tuple["DotenvDocument", tuple[str, ...]]:
        if len(replacements) > len(FIELD_CATALOG):
            raise ConfigStoreError("invalid_value")
        lines = list(self._lines)
        changed: list[str] = []
        for key, replacement in replacements.items():
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                raise ConfigStoreError("field_not_editable")
            if replacement is None:
                continue
            if not isinstance(replacement, str):
                raise ConfigStoreError("invalid_value")
            spec = FIELD_CATALOG.get(key)
            if spec is None or not spec.editable:
                raise ConfigStoreError("field_not_editable")
            current_value = self.value(key)
            if current_value and spec.value_kind in {"url_ws", "url_http_optional"}:
                parsed = urlsplit(current_value)
                if (
                    parsed.username is not None
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ConfigStoreError("field_not_editable")
            _validate_value(replacement, spec)
            encoded = _encode_dotenv_value(replacement)
            if key in self._by_key:
                index = self._by_key[key]
                current = lines[index]
                if _decode_dotenv_value(current.value) == replacement:
                    continue
                raw = current.prefix + key.encode("ascii") + current.separator + encoded + current.ending
                lines[index] = _Line(
                    raw,
                    key,
                    current.prefix,
                    current.separator,
                    encoded,
                    current.ending,
                )
            else:
                ending = self._preferred_newline()
                prefix = b""
                if lines and not lines[-1].ending:
                    previous = lines[-1]
                    lines[-1] = previous._replace(
                        raw=previous.raw + ending, ending=ending
                    )
                raw = prefix + key.encode("ascii") + b"=" + encoded + ending
                lines.append(_Line(raw, key, prefix, b"=", encoded, ending))
            changed.append(key)
        return DotenvDocument(tuple(lines)), tuple(changed)

    def _preferred_newline(self) -> bytes:
        for line in self._lines:
            if line.ending:
                return line.ending
        return b"\n"


class ConfigPaths(NamedTuple):
    desired: Path
    applied: Path
    operation: Path
    recovery: Path
    revision: Path
    journal: Path
    intents: Path
    lock: Path

    @classmethod
    def for_desired(cls, desired: Path, state_dir: Path) -> "ConfigPaths":
        return cls(
            desired=Path(desired),
            applied=Path(state_dir) / "applied.env",
            operation=Path(state_dir) / "operation.env",
            recovery=Path(state_dir) / "pre-save.env",
            revision=Path(state_dir) / "revision.json",
            journal=Path(state_dir) / "operation.json",
            intents=Path(state_dir) / "operation-intents.json",
            lock=Path(state_dir) / "operation.lock",
        )


class _ProtectedContent(NamedTuple):
    data: bytes
    device: int
    inode: int


class OperationSnapshot(NamedTuple):
    revision: str
    desired: _ProtectedContent
    operation: _ProtectedContent


class ConfigView(NamedTuple):
    revision: str
    fields: tuple[dict[str, Any], ...]
    baseline_state: str
    pending: bool
    journal: dict[str, Any]
    changed_keys: tuple[str, ...]
    impact_classes: tuple[str, ...]
    automatic_apply_allowed: bool


class SaveResult(NamedTuple):
    view: ConfigView
    wrote: bool
    changed_keys: tuple[str, ...]

    @property
    def revision(self) -> str:
        return self.view.revision

    @property
    def pending(self) -> bool:
        return self.view.pending


def _safe_parent(path: Path, uid: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConfigStoreError("unsafe_storage") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_mode & 0o022
    ):
        raise ConfigStoreError("unsafe_storage")
    return metadata


def _open_parent(path: Path, uid: int) -> int:
    expected = _safe_parent(path, uid)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ConfigStoreError("unsafe_storage") from exc
    actual = os.fstat(descriptor)
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(descriptor)
        raise ConfigStoreError("unsafe_storage")
    return descriptor


def _validate_file_metadata(metadata: os.stat_result, uid: int) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ConfigStoreError("unsafe_storage")


def _read_protected(path: Path, uid: int, max_bytes: int) -> _ProtectedContent:
    directory = _open_parent(path.parent, uid)
    descriptor: int | None = None
    try:
        try:
            expected = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            _validate_file_metadata(expected, uid)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path.name, flags, dir_fd=directory)
            actual = os.fstat(descriptor)
            _validate_file_metadata(actual, uid)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ConfigStoreError("unsafe_storage")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise ConfigStoreError("document_too_large")
            after = os.fstat(descriptor)
            if (
                after.st_size != actual.st_size
                or after.st_mtime_ns != actual.st_mtime_ns
                or after.st_ctime_ns != actual.st_ctime_ns
            ):
                raise ConfigStoreError("unsafe_storage")
            return _ProtectedContent(data, actual.st_dev, actual.st_ino)
        except ConfigStoreError:
            raise
        except OSError as exc:
            raise ConfigStoreError("unsafe_storage") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _read_optional_protected(
    path: Path, uid: int, max_bytes: int
) -> _ProtectedContent | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigStoreError("unsafe_storage") from exc
    return _read_protected(path, uid, max_bytes)


def _atomic_write(
    path: Path,
    data: bytes,
    uid: int,
    *,
    expected: _ProtectedContent | None = None,
    expected_max_bytes: int | None = None,
) -> None:
    directory = _open_parent(path.parent, uid)
    temp_name = f".buzz-control.tmp-{secrets.token_urlsafe(18)}"
    descriptor: int | None = None
    created = False
    try:
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise ConfigStoreError("unsafe_storage") from exc
        if existing is not None:
            _validate_file_metadata(existing, uid)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temp_name, flags, 0o600, dir_fd=directory)
            created = True
            os.fchmod(descriptor, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short protected write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if expected is not None:
                current = _read_protected(
                    path,
                    uid,
                    expected_max_bytes or max(len(expected.data), 1),
                )
                if current != expected:
                    raise ConfigStoreError("stale_revision")
            os.replace(
                temp_name,
                path.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            created = False
            os.fsync(directory)
        except ConfigStoreError:
            raise
        except OSError as exc:
            raise ConfigStoreError("unsafe_storage") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                os.unlink(temp_name, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)
    _read_protected(path, uid, max(len(data), 1))


def _unlink_protected(
    path: Path,
    uid: int,
    *,
    expected: _ProtectedContent,
    expected_max_bytes: int,
) -> None:
    current = _read_protected(path, uid, expected_max_bytes)
    if current != expected:
        raise ConfigStoreError("unsafe_storage")
    directory = _open_parent(path.parent, uid)
    try:
        try:
            metadata = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            _validate_file_metadata(metadata, uid)
            if (metadata.st_dev, metadata.st_ino) != (
                expected.device,
                expected.inode,
            ):
                raise ConfigStoreError("unsafe_storage")
            os.unlink(path.name, dir_fd=directory)
            os.fsync(directory)
        except ConfigStoreError:
            raise
        except OSError as exc:
            raise ConfigStoreError("unsafe_storage") from exc
    finally:
        os.close(directory)


def _restore_protected(
    path: Path,
    previous: _ProtectedContent | None,
    installed: _ProtectedContent,
    uid: int,
    max_bytes: int,
) -> None:
    if previous is None:
        _unlink_protected(
            path,
            uid,
            expected=installed,
            expected_max_bytes=max_bytes,
        )
        return
    _atomic_write(
        path,
        previous.data,
        uid,
        expected=installed,
        expected_max_bytes=max_bytes,
    )


_JOURNAL_KEYS = {
    "schema_version",
    "phase",
    "action",
    "revision",
    "prior_revision",
    "changed_keys",
    "impact_classes",
    "started_at",
    "completed_at",
    "outcome",
    "runtime_generation",
    "correlation_id",
}
_JOURNAL_PHASES = {
    "saved",
    "pending",
    "applying",
    "verifying",
    "promoting",
    "applied",
    "rolling_back",
    "rolled_back",
    "blocked",
    "degraded",
    "saving",
    "restoring",
    "adopting",
    "recovering",
}
BLOCKING_JOURNAL_PHASES = frozenset(
    {
        "saving",
        "restoring",
        "adopting",
        "recovering",
        "applying",
        "verifying",
        "promoting",
        "rolling_back",
        "degraded",
        "blocked",
    }
)
RECOVERY_ADOPTION_PHASES = BLOCKING_JOURNAL_PHASES


def _validate_journal(entry: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, Mapping) or set(entry) - _JOURNAL_KEYS:
        raise ConfigStoreError("invalid_journal")
    normalized = dict(entry)
    if normalized.get("schema_version", 1) != 1:
        raise ConfigStoreError("invalid_journal")
    normalized["schema_version"] = 1
    phase = normalized.get("phase")
    if phase not in _JOURNAL_PHASES:
        raise ConfigStoreError("invalid_journal")
    for key in ("revision", "prior_revision", "correlation_id"):
        value = normalized.get(key)
        if value is not None and (
            not isinstance(value, str) or not _OPAQUE_REVISION.fullmatch(value)
        ):
            raise ConfigStoreError("invalid_journal")
    changed = normalized.get("changed_keys", [])
    if (
        not isinstance(changed, list)
        or len(changed) > 64
        or len(set(changed)) != len(changed)
        or any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in changed)
    ):
        raise ConfigStoreError("invalid_journal")
    impacts = normalized.get("impact_classes", [])
    if not isinstance(impacts, list) or any(
        impact not in {item.value for item in ImpactClass} for impact in impacts
    ):
        raise ConfigStoreError("invalid_journal")
    for key in ("started_at", "completed_at", "action", "outcome", "runtime_generation"):
        value = normalized.get(key)
        if value is not None and (
            not isinstance(value, str)
            or len(value) > 128
            or not re.fullmatch(r"[A-Za-z0-9_.:+-]+", value)
        ):
            raise ConfigStoreError("invalid_journal")
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_JOURNAL_BYTES:
        raise ConfigStoreError("invalid_journal")
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ConfigStore:
    def __init__(self, paths: ConfigPaths, *, uid: int | None = None):
        self.paths = paths
        self.uid = os.getuid() if uid is None else uid

    @contextmanager
    def lock(self) -> Iterator[None]:
        directory = _open_parent(self.paths.lock.parent, self.uid)
        descriptor: int | None = None
        try:
            try:
                try:
                    expected = os.stat(
                        self.paths.lock.name,
                        dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    expected = None
                if expected is None:
                    flags = (
                        os.O_RDWR
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    descriptor = os.open(
                        self.paths.lock.name, flags, 0o600, dir_fd=directory
                    )
                else:
                    _validate_file_metadata(expected, self.uid)
                    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(
                        self.paths.lock.name, flags, dir_fd=directory
                    )
                    actual = os.fstat(descriptor)
                    _validate_file_metadata(actual, self.uid)
                    if (actual.st_dev, actual.st_ino) != (
                        expected.st_dev,
                        expected.st_ino,
                    ):
                        raise ConfigStoreError("unsafe_storage")
                _validate_file_metadata(os.fstat(descriptor), self.uid)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConfigStoreError("busy") from exc
            except ConfigStoreError:
                raise
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ConfigStoreError("busy") from exc
                raise ConfigStoreError("unsafe_storage") from exc
            self._scavenge_locked()
            yield
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            os.close(directory)

    def _scavenge_locked(self) -> None:
        parents = {self.paths.desired.parent, self.paths.lock.parent}
        cutoff = time.time() - STALE_TEMP_SECONDS
        for parent in parents:
            directory = _open_parent(parent, self.uid)
            try:
                try:
                    names = os.listdir(directory)
                except OSError as exc:
                    raise ConfigStoreError("unsafe_storage") from exc
                for name in names:
                    if not name.startswith(".buzz-control.tmp-"):
                        continue
                    try:
                        before = os.stat(
                            name, dir_fd=directory, follow_symlinks=False
                        )
                        _validate_file_metadata(before, self.uid)
                        if before.st_mtime > cutoff:
                            continue
                        after = os.stat(
                            name, dir_fd=directory, follow_symlinks=False
                        )
                        _validate_file_metadata(after, self.uid)
                        if (after.st_dev, after.st_ino) != (
                            before.st_dev,
                            before.st_ino,
                        ):
                            raise ConfigStoreError("unsafe_storage")
                        os.unlink(name, dir_fd=directory)
                    except ConfigStoreError:
                        raise
                    except OSError as exc:
                        raise ConfigStoreError("unsafe_storage") from exc
            finally:
                os.close(directory)

    def describe(self) -> ConfigView:
        with self.lock():
            return self._describe_locked()

    def runtime_value(self, name: str, *, desired: bool = False) -> str | None:
        spec = FIELD_CATALOG.get(name) or _RUNTIME_FIELD_CATALOG.get(name)
        if spec is None or spec.disclosure == Disclosure.WRITE_ONLY:
            raise ConfigStoreError("field_not_editable")
        with self.lock():
            desired_content = _read_protected(
                self.paths.desired, self.uid, MAX_CONFIG_BYTES
            )
            selected = desired_content
            if not desired:
                applied = _read_optional_protected(
                    self.paths.applied, self.uid, MAX_CONFIG_BYTES
                )
                if applied is not None and applied.data != desired_content.data:
                    selected = applied
            return DotenvDocument.parse(selected.data).value(name)

    def _describe_locked(self) -> ConfigView:
        desired = _read_protected(
            self.paths.desired, self.uid, MAX_CONFIG_BYTES
        )
        document = DotenvDocument.parse(desired.data)
        revision = self._revision_for_locked(desired)
        applied = _read_optional_protected(
            self.paths.applied, self.uid, MAX_CONFIG_BYTES
        )
        if applied is None:
            baseline_state = "baseline_missing"
            pending = True
            changed_keys: tuple[str, ...] = ()
        else:
            applied_document = DotenvDocument.parse(applied.data)
            baseline_state = "established"
            pending = applied.data != desired.data
            changed_keys = document.changed_keys_from(applied_document)
        impact_classes = tuple(
            sorted(
                {
                    (
                        FIELD_CATALOG[key].impact
                        if key in FIELD_CATALOG
                        else ImpactClass.UNKNOWN_MANUAL_ONLY
                    ).value
                    for key in changed_keys
                }
            )
        )
        blocked_impacts = {
            ImpactClass.MANUAL_MAINTENANCE.value,
            ImpactClass.SERVER_MANAGED.value,
            ImpactClass.UNKNOWN_MANUAL_ONLY.value,
        }
        return ConfigView(
            revision=revision,
            fields=document.project(),
            baseline_state=baseline_state,
            pending=pending,
            journal=self._read_journal_locked(),
            changed_keys=changed_keys,
            impact_classes=impact_classes,
            automatic_apply_allowed=bool(
                pending
                and baseline_state == "established"
                and not blocked_impacts.intersection(impact_classes)
            ),
        )

    def save(
        self, base_revision: str, replacements: Mapping[str, str | None]
    ) -> SaveResult:
        with self.lock():
            current = self._describe_locked()
            self._ensure_mutation_allowed_locked()
            if current.revision != base_revision:
                raise ConfigStoreError("stale_revision")
            if current.baseline_state == "baseline_missing":
                raise ConfigStoreError("baseline_missing")
            before = _read_protected(
                self.paths.desired, self.uid, MAX_CONFIG_BYTES
            )
            if self._revision_for_locked(before) != base_revision:
                raise ConfigStoreError("stale_revision")
            document = DotenvDocument.parse(before.data)
            updated, changed = document.apply(replacements)
            if not changed:
                return SaveResult(current, False, ())
            rendered = updated.render()
            if len(rendered) > MAX_CONFIG_BYTES:
                raise ConfigStoreError("document_too_large")
            changed_impacts = sorted(
                {
                    (
                        FIELD_CATALOG[key].impact
                        if key in FIELD_CATALOG
                        else ImpactClass.UNKNOWN_MANUAL_ONLY
                    ).value
                    for key in changed
                }
            )
            previous_journal = _read_optional_protected(
                self.paths.journal, self.uid, MAX_JOURNAL_BYTES
            )
            previous_recovery = _read_optional_protected(
                self.paths.recovery, self.uid, MAX_CONFIG_BYTES
            )
            installed_journal: _ProtectedContent | None = None
            installed_recovery: _ProtectedContent | None = None
            try:
                self._write_journal_locked(
                    {
                        "phase": "saving",
                        "action": "save",
                        "revision": current.revision,
                        "changed_keys": list(changed),
                        "impact_classes": changed_impacts,
                        "started_at": _utc_now(),
                    }
                )
                installed_journal = _read_protected(
                    self.paths.journal, self.uid, MAX_JOURNAL_BYTES
                )
                _atomic_write(self.paths.recovery, before.data, self.uid)
                installed_recovery = _read_protected(
                    self.paths.recovery, self.uid, MAX_CONFIG_BYTES
                )
                _atomic_write(
                    self.paths.desired,
                    rendered,
                    self.uid,
                    expected=before,
                    expected_max_bytes=MAX_CONFIG_BYTES,
                )
            except ConfigStoreError as exc:
                if exc.code == "stale_revision":
                    if installed_recovery is not None:
                        _restore_protected(
                            self.paths.recovery,
                            previous_recovery,
                            installed_recovery,
                            self.uid,
                            MAX_CONFIG_BYTES,
                        )
                    if installed_journal is not None:
                        _restore_protected(
                            self.paths.journal,
                            previous_journal,
                            installed_journal,
                            self.uid,
                            MAX_JOURNAL_BYTES,
                        )
                raise
            after = _read_protected(
                self.paths.desired, self.uid, MAX_CONFIG_BYTES
            )
            revision = self._new_revision_locked(after)
            self._write_journal_locked(
                {
                    "phase": "saved",
                    "action": "save",
                    "revision": revision,
                    "prior_revision": current.revision,
                    "changed_keys": list(changed),
                    "impact_classes": changed_impacts,
                    "completed_at": _utc_now(),
                    "outcome": "pending",
                }
            )
            return SaveResult(self._describe_locked(), True, changed)

    def promote(self, expected_revision: str) -> ConfigView:
        with self.lock():
            current = self._describe_locked()
            self._ensure_mutation_allowed_locked()
            if current.revision != expected_revision:
                raise ConfigStoreError("stale_revision")
            snapshot = self.operation_snapshot_locked(expected_revision)
            return self.promote_operation_locked(
                snapshot,
                phase="applied",
                action="promote",
            )

    def operation_snapshot_locked(self, expected_revision: str) -> OperationSnapshot:
        desired = _read_protected(
            self.paths.desired, self.uid, MAX_CONFIG_BYTES
        )
        if self._revision_for_locked(desired) != expected_revision:
            raise ConfigStoreError("stale_revision")
        _atomic_write(self.paths.operation, desired.data, self.uid)
        operation = _read_protected(
            self.paths.operation, self.uid, MAX_CONFIG_BYTES
        )
        current = _read_protected(
            self.paths.desired, self.uid, MAX_CONFIG_BYTES
        )
        if current != desired:
            raise ConfigStoreError("stale_revision")
        return OperationSnapshot(expected_revision, desired, operation)

    def assert_operation_current_locked(self, snapshot: OperationSnapshot) -> None:
        desired = _read_protected(
            self.paths.desired, self.uid, MAX_CONFIG_BYTES
        )
        operation = _read_protected(
            self.paths.operation, self.uid, MAX_CONFIG_BYTES
        )
        if desired != snapshot.desired or operation != snapshot.operation:
            raise ConfigStoreError("stale_revision")

    def promote_operation_locked(
        self,
        snapshot: OperationSnapshot,
        *,
        phase: str = "applied",
        action: str = "promote",
        runtime_generation: str | None = None,
        outcome: str = "applied",
    ) -> ConfigView:
        self.assert_operation_current_locked(snapshot)
        current = self._describe_locked()
        if current.revision != snapshot.revision:
            raise ConfigStoreError("stale_revision")
        previous_applied = _read_optional_protected(
            self.paths.applied, self.uid, MAX_CONFIG_BYTES
        )
        journal = {
            "phase": phase,
            "action": action,
            "revision": current.revision,
            "changed_keys": list(current.changed_keys),
            "impact_classes": list(current.impact_classes),
            "completed_at": _utc_now(),
            "outcome": outcome,
        }
        if runtime_generation is not None:
            journal["runtime_generation"] = runtime_generation
        promoted: _ProtectedContent | None = None
        try:
            _atomic_write(self.paths.applied, snapshot.operation.data, self.uid)
            promoted = _read_protected(
                self.paths.applied, self.uid, MAX_CONFIG_BYTES
            )
            self._write_journal_locked(journal)
        except ConfigStoreError:
            if promoted is not None:
                _restore_protected(
                    self.paths.applied,
                    previous_applied,
                    promoted,
                    self.uid,
                    MAX_CONFIG_BYTES,
                )
            raise
        return self._describe_locked()

    def restore(self, expected_revision: str) -> ConfigView:
        with self.lock():
            current = self._describe_locked()
            self._ensure_mutation_allowed_locked()
            if current.revision != expected_revision:
                raise ConfigStoreError("stale_revision")
            applied = _read_optional_protected(
                self.paths.applied, self.uid, MAX_CONFIG_BYTES
            )
            if applied is None:
                raise ConfigStoreError("baseline_missing")
            desired = _read_protected(
                self.paths.desired, self.uid, MAX_CONFIG_BYTES
            )
            if self._revision_for_locked(desired) != expected_revision:
                raise ConfigStoreError("stale_revision")
            _atomic_write(self.paths.recovery, desired.data, self.uid)
            _atomic_write(
                self.paths.desired,
                applied.data,
                self.uid,
                expected=desired,
                expected_max_bytes=MAX_CONFIG_BYTES,
            )
            after = _read_protected(
                self.paths.desired, self.uid, MAX_CONFIG_BYTES
            )
            revision = self._new_revision_locked(after)
            self._write_journal_locked(
                {
                    "phase": "applied",
                    "action": "restore",
                    "revision": revision,
                    "prior_revision": current.revision,
                    "changed_keys": [],
                    "impact_classes": [],
                    "completed_at": _utc_now(),
                    "outcome": "restored",
                }
            )
            return self._describe_locked()

    def write_journal(self, entry: Mapping[str, Any]) -> None:
        with self.lock():
            self._write_journal_locked(entry)

    def record_operation_intent(
        self,
        *,
        digest: str,
        action: str,
        revision: str,
        expires_at: float,
    ) -> None:
        if (
            not re.fullmatch(r"[a-f0-9]{64}", digest)
            or action not in {"apply", "apply_start", "adopt", "recover_adopt"}
            or not _OPAQUE_REVISION.fullmatch(revision)
            or not isinstance(expires_at, (int, float))
            or expires_at <= time.time()
            or expires_at > time.time() + 10 * 60
        ):
            raise ConfigStoreError("invalid_intent")
        with self.lock():
            current = self._describe_locked()
            if current.revision != revision:
                raise ConfigStoreError("stale_revision")
            if action == "recover_adopt":
                if current.journal.get("phase") not in RECOVERY_ADOPTION_PHASES:
                    raise ConfigStoreError("recovery_required")
            else:
                self._ensure_mutation_allowed_locked()
            intents = self._read_intents_locked()
            now = time.time()
            intents = [item for item in intents if item["expires_at"] > now]
            intents = [item for item in intents if item["digest"] != digest]
            intents.append(
                {
                    "digest": digest,
                    "action": action,
                    "revision": revision,
                    "expires_at": float(expires_at),
                }
            )
            self._write_intents_locked(intents[-32:])

    def consume_operation_intent_locked(
        self,
        *,
        token: str,
        action: str,
        revision: str,
    ) -> None:
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token)
            or action not in {"apply", "apply_start", "adopt", "recover_adopt"}
            or not _OPAQUE_REVISION.fullmatch(revision)
        ):
            raise ConfigStoreError("invalid_intent")
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        intents = self._read_intents_locked()
        now = time.time()
        matched = None
        remaining: list[dict[str, Any]] = []
        for item in intents:
            if item["expires_at"] <= now:
                continue
            if item["digest"] == digest and matched is None:
                matched = item
                continue
            remaining.append(item)
        self._write_intents_locked(remaining)
        if (
            matched is None
            or matched["action"] != action
            or matched["revision"] != revision
        ):
            raise ConfigStoreError("invalid_intent")

    def _read_intents_locked(self) -> list[dict[str, Any]]:
        content = _read_optional_protected(
            self.paths.intents, self.uid, MAX_JOURNAL_BYTES
        )
        if content is None:
            return []
        try:
            payload = json.loads(content.data)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigStoreError("invalid_intent") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "intents"}
            or payload["schema_version"] != 1
            or not isinstance(payload["intents"], list)
            or len(payload["intents"]) > 32
        ):
            raise ConfigStoreError("invalid_intent")
        validated: list[dict[str, Any]] = []
        for item in payload["intents"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"digest", "action", "revision", "expires_at"}
                or not isinstance(item["digest"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["digest"])
                or item["action"] not in {"apply", "apply_start", "adopt", "recover_adopt"}
                or not isinstance(item["revision"], str)
                or not _OPAQUE_REVISION.fullmatch(item["revision"])
                or not isinstance(item["expires_at"], (int, float))
            ):
                raise ConfigStoreError("invalid_intent")
            validated.append(item)
        return validated

    def _write_intents_locked(self, intents: list[dict[str, Any]]) -> None:
        payload = {"schema_version": 1, "intents": intents}
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode() + b"\n"
        if len(encoded) > MAX_JOURNAL_BYTES:
            raise ConfigStoreError("invalid_intent")
        _atomic_write(self.paths.intents, encoded, self.uid)

    def _ensure_mutation_allowed_locked(self) -> None:
        phase = self._read_journal_locked().get("phase")
        if phase in BLOCKING_JOURNAL_PHASES:
            raise ConfigStoreError("recovery_required")

    def _write_journal_locked(self, entry: Mapping[str, Any]) -> None:
        normalized = _validate_journal(entry)
        encoded = json.dumps(
            normalized, sort_keys=True, separators=(",", ":")
        ).encode() + b"\n"
        _atomic_write(self.paths.journal, encoded, self.uid)

    def _read_journal_locked(self) -> dict[str, Any]:
        content = _read_optional_protected(
            self.paths.journal, self.uid, MAX_JOURNAL_BYTES
        )
        if content is None:
            return {}
        try:
            decoded = json.loads(content.data)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigStoreError("invalid_journal") from exc
        return _validate_journal(decoded)

    def _revision_for_locked(self, desired: _ProtectedContent) -> str:
        fingerprint = hashlib.sha256(desired.data).hexdigest()
        content = _read_optional_protected(
            self.paths.revision, self.uid, MAX_JOURNAL_BYTES
        )
        if content is not None:
            try:
                record = json.loads(content.data)
            except (UnicodeError, json.JSONDecodeError):
                record = None
            if (
                isinstance(record, dict)
                and record.get("schema_version") == 1
                and record.get("fingerprint") == fingerprint
                and record.get("device") == desired.device
                and record.get("inode") == desired.inode
                and isinstance(record.get("revision"), str)
                and _OPAQUE_REVISION.fullmatch(record["revision"])
            ):
                return record["revision"]
        return self._new_revision_locked(desired)

    def _new_revision_locked(self, desired: _ProtectedContent) -> str:
        revision = secrets.token_urlsafe(24)
        record = {
            "schema_version": 1,
            "revision": revision,
            "fingerprint": hashlib.sha256(desired.data).hexdigest(),
            "device": desired.device,
            "inode": desired.inode,
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        _atomic_write(self.paths.revision, encoded, self.uid)
        return revision
