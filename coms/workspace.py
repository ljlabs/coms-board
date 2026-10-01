"""One workspace policy for settings, discovery, execution and review."""
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = APP_ROOT.parent / "workspace"


def workspace_path(settings, *, create_default=True):
    raw = settings.get("workspace", str(DEFAULT_WORKSPACE))
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("workspace must be an absolute directory path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("workspace must be an absolute directory path")
    path = path.resolve()
    # Reject symlinks too: neither the app nor a parent containing it is a
    # suitable agent workspace. Keep application source and runtime data apart.
    if path == APP_ROOT or APP_ROOT in path.parents or path in APP_ROOT.parents:
        raise ValueError("workspace must be separate from the application directory and its parents")
    if path == DEFAULT_WORKSPACE.resolve() and create_default:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"workspace directory does not exist: {path}")
    return str(path)
