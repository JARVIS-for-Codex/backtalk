"""Translate the Creator-owned trusted-realms list into a Codex profile."""

from pathlib import Path


BACKTALK_ROOT = Path(__file__).resolve().parent.parent
AGENT_ROOT = BACKTALK_ROOT.parent
REALMS_FILE = BACKTALK_ROOT / "codex_trusted_realms.txt"
PROFILE_NAME = "jarvis-realms"


def _is_within(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def load_trusted_realms():
    """Read and validate the realms file without ever changing it."""
    if not REALMS_FILE.is_file():
        return []
    realms, seen = [], set()
    for raw_line in REALMS_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        path = Path(line)
        if not path.is_absolute():
            raise ValueError(f"Trusted realm is not an absolute path: {line}")
        resolved = path.resolve()
        if _is_within(resolved, AGENT_ROOT.resolve()):
            raise ValueError(
                "The protected JARVIS machinery cannot be a writable "
                f"realm: {line}")
        if not resolved.is_dir():
            raise FileNotFoundError(f"Trusted realm does not exist: {line}")
        key = str(resolved).casefold()
        if key not in seen:
            realms.append(resolved)
            seen.add(key)
    return realms


def _toml_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_override(realms):
    """Return a read-everywhere, write-only-in-trusted-realms profile."""
    filesystem = {
        ":root": "read",
        str(AGENT_ROOT.resolve()): "read",
        **{str(path): "write" for path in realms},
    }
    rules = ",".join(
        f"{_toml_string(path)}={_toml_string(access)}"
        for path, access in filesystem.items())
    return (
        f"permissions={{{_toml_string(PROFILE_NAME)}={{"
        'description="Creator-defined JARVIS working realms",'
        f"filesystem={{{rules}}},network={{enabled=false}}}}}}")
