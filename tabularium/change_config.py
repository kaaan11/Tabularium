"""Configuration parser and validator for .ai/change.yaml."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CHANGE_CONFIG_INVALID = "CHANGE_CONFIG_INVALID"
DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 1800
ALLOWED_KEYS = frozenset({"test_command", "timeout_seconds"})
MAX_CONFIG_BYTES = 64 * 1024


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping key given twice: the last value would
    otherwise win silently while a reader trusts the first."""


def _construct_strict_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            raise yaml.constructor.ConstructorError(
                None, None, "mapping keys must be scalars", key_node.start_mark)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_strict_mapping)


class ChangeConfigError(ValueError):
    """Raised when .ai/change.yaml exists but fails validation."""

    def __init__(self, message: str, code: str = CHANGE_CONFIG_INVALID):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ChangeConfig:
    """Validated configuration for isolated test runs under --change."""

    test_command: list[str]
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    # SHA-256 of the exact bytes that were parsed (see config_digest).
    digest: str = ""


def load_change_config(target: Path | str) -> ChangeConfig | None:
    """Load and validate .ai/change.yaml from the target project.

    Returns:
        ChangeConfig instance if .ai/change.yaml exists and is valid.
        None if .ai/change.yaml does not exist.

    Raises:
        ChangeConfigError (code=CHANGE_CONFIG_INVALID) on any validation defect.
    """
    target_path = Path(target)
    ai_dir = target_path / ".ai"
    cfg_path = ai_dir / "change.yaml"

    # Symlink check on the config file path itself (even if dangling)
    if os.path.islink(cfg_path):
        raise ChangeConfigError("change.yaml must not be a symlink")

    if not os.path.lexists(cfg_path):
        return None

    # Check .ai directory itself
    if os.path.islink(ai_dir):
        raise ChangeConfigError(".ai directory must not be a symlink")

    # Regular file check
    try:
        st = os.lstat(cfg_path)
    except OSError as exc:
        raise ChangeConfigError(f"Could not inspect change.yaml: {exc}")

    if not stat.S_ISREG(st.st_mode):
        raise ChangeConfigError("change.yaml must be a regular file")
    if st.st_size > MAX_CONFIG_BYTES:
        raise ChangeConfigError(f"change.yaml is larger than {MAX_CONFIG_BYTES} bytes")

    # Ensure change.yaml is a direct child of target/.ai
    try:
        resolved_cfg_parent = cfg_path.resolve().parent
        resolved_ai_dir = ai_dir.resolve()
        if resolved_cfg_parent != resolved_ai_dir:
            raise ChangeConfigError("change.yaml must be a direct child of .ai/")
    except OSError as exc:
        raise ChangeConfigError(f"Could not resolve change.yaml path: {exc}")

    # Read bytes and check UTF-8
    try:
        raw_bytes = cfg_path.read_bytes()
    except OSError as exc:
        raise ChangeConfigError(f"Could not read change.yaml: {exc}")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChangeConfigError(f"change.yaml content is not valid UTF-8: {exc}")

    # YAML parsing
    try:
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ChangeConfigError(f"change.yaml syntax error: {exc}")
    except RecursionError:
        raise ChangeConfigError("change.yaml is nested too deeply")

    cfg = validate_change_data(data)
    return ChangeConfig(test_command=cfg.test_command, timeout_seconds=cfg.timeout_seconds,
                        digest=hashlib.sha256(raw_bytes).hexdigest())


def config_digest(target: Path | str) -> str | None:
    """SHA-256 of .ai/change.yaml as it is on disk, or None when it is absent.

    Lets a run notice that the configuration changed after it was loaded.
    Never raises: an entry that cannot be read safely yields a marker that
    differs from any digest.
    """
    cfg_path = Path(target) / ".ai" / "change.yaml"
    try:
        info = os.lstat(cfg_path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"
    # Never follow a link or open a FIFO; a non-regular entry simply differs.
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
        return f"unreadable:{info.st_mode:o}:{info.st_size}"
    try:
        fd = os.open(cfg_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"
    return hashlib.sha256(data).hexdigest()

def validate_change_data(data: Any) -> ChangeConfig:
    """Validate an already parsed change.yaml mapping (shared by loading and onboarding)."""
    # Root must be a mapping
    if not isinstance(data, dict):
        raise ChangeConfigError("change.yaml root must be a mapping")

    # Unknown keys check
    unknown_keys = set(data.keys()) - ALLOWED_KEYS
    if unknown_keys:
        raise ChangeConfigError(
            f"change.yaml contains unknown keys: {', '.join(sorted(str(k) for k in unknown_keys))}"
        )

    # test_command validation
    if "test_command" not in data:
        raise ChangeConfigError("change.yaml missing required 'test_command'")

    cmd = data["test_command"]
    if not isinstance(cmd, list) or isinstance(cmd, (str, bytes)):
        raise ChangeConfigError("test_command must be a list of strings")

    if len(cmd) == 0:
        raise ChangeConfigError("test_command cannot be empty")

    for idx, item in enumerate(cmd):
        if not isinstance(item, str):
            raise ChangeConfigError(f"test_command item {idx} must be a string")
        if len(item) == 0:
            raise ChangeConfigError(f"test_command item {idx} cannot be empty")
        # Every character str.splitlines() breaks on (U+2028, NEL, VT, FF…)
        # would split the commit message's "Tests:" line as well.
        if "\0" in item or len(item.splitlines()) != 1 or item.splitlines()[0] != item:
            raise ChangeConfigError(f"test_command item {idx} contains NUL or a line break")
        try:
            item.encode("utf-8")
        except UnicodeEncodeError:
            raise ChangeConfigError(f"test_command item {idx} is not valid Unicode text")

    # timeout_seconds validation
    timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    if "timeout_seconds" in data:
        val = data["timeout_seconds"]
        if isinstance(val, bool) or not isinstance(val, int):
            raise ChangeConfigError("timeout_seconds must be an integer")
        if val < MIN_TIMEOUT_SECONDS or val > MAX_TIMEOUT_SECONDS:
            raise ChangeConfigError(
                f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}"
            )
        timeout_seconds = val

    return ChangeConfig(test_command=list(cmd), timeout_seconds=timeout_seconds)


def render_change_yaml(test_command: list[str] | tuple[str, ...]) -> str:
    """The .ai/change.yaml text for test_command, or ChangeConfigError.

    The text is parsed back with the same loader and rules --change uses and
    held to the same size cap, so a file written from it always loads.
    """
    from tabularium.engine.persistence.model import contains_secret
    if any(contains_secret(str(arg)) for arg in test_command) or contains_secret(" ".join(map(str, test_command))):
        raise ChangeConfigError("test_command looks like it contains a secret; .ai/change.yaml is committed")
    text = yaml.safe_dump({"test_command": list(test_command)}, sort_keys=False)
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ChangeConfigError(f"change.yaml would be larger than {MAX_CONFIG_BYTES} bytes")
    validate_change_data(yaml.load(text, Loader=_StrictLoader))
    return text
