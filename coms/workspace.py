"""One workspace policy for settings, discovery, execution and review."""
from pathlib import Path
import errno
import os
import re
import string

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


def _folder_directory(path):
    if not isinstance(path, str) or not path.strip():
        raise ValueError("choose an existing folder first")
    current = Path(path).expanduser()
    if not current.is_absolute():
        raise ValueError("folder path must be absolute")
    try:
        current = current.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot open folder: {exc}") from exc
    if not current.is_dir():
        raise ValueError("selected path is not a directory")
    return current


def _folder_name(name):
    if not isinstance(name, str) or not name or name != name.strip() or len(name) > 255:
        raise ValueError("enter a folder name of 1–255 characters without surrounding spaces")
    if name in (".", "..") or name.endswith(".") or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name):
        raise ValueError("folder name must be a single name without slashes or special path characters")
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", name.split(".", 1)[0], re.IGNORECASE):
        raise ValueError("this folder name is reserved by Windows")
    return name


def _folder_scope(path, protected_paths=(), *, creating=False):
    if os.environ.get("COMS_IN_DOCKER") == "1":
        shared = Path("/workspace")
        if path != shared and shared not in path.parents:
            raise ValueError("folder editing is available inside the shared /workspace folder")
    protected = [APP_ROOT, *(Path(item).resolve() for item in protected_paths)]
    for root in protected:
        if path == root or root in path.parents or (not creating and path in root.parents):
            raise ValueError("application and runtime folders cannot be changed")
    if not creating and (path.parent == path or path.is_mount()):
        raise ValueError("drive and mounted workspace roots cannot be renamed or deleted")


def workspace_folder_target(path, *, protected_paths=()):
    """Validate a directory before renaming or removing it; never follow a link."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("choose a folder first")
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError("symbolic-link folders cannot be renamed or deleted here")
    current = _folder_directory(path)
    if candidate.absolute() != current:
        raise ValueError("folders reached through symbolic links or junctions cannot be changed here")
    _folder_scope(current, protected_paths)
    return current


def _folder_os_error(exc):
    if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
        return "the folder is not empty, or another folder already has that name"
    if exc.errno == errno.EROFS:
        return "this workspace is read-only; choose a writable workspace folder"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return "the server does not have permission to change this folder"
    return f"could not change folder: {exc.strerror or str(exc)}"


def workspace_folder_change(action, path, name=None, *, protected_paths=()):
    """Create, rename, or remove one empty directory without recursive deletion."""
    if action == "create":
        parent = _folder_directory(path)
        destination = parent / _folder_name(name)
        _folder_scope(destination.resolve(), protected_paths, creating=True)
        try:
            destination.mkdir()
        except FileExistsError as exc:
            raise ValueError("a file or folder with this name already exists") from exc
        except OSError as exc:
            raise ValueError(_folder_os_error(exc)) from exc
        return {"path": str(destination), "parent": str(parent)}
    current = workspace_folder_target(path, protected_paths=protected_paths)
    if action == "rename":
        destination = current.parent / _folder_name(name)
        _folder_scope(destination.resolve(), protected_paths)
        if str(destination) == str(current):
            return {"path": str(current), "previous_path": str(current), "parent": str(current.parent)}
        try:
            if destination.is_symlink() or (destination.exists() and not destination.samefile(current)):
                raise ValueError("a file or folder with this name already exists")
            current.rename(destination)
        except OSError as exc:
            raise ValueError(_folder_os_error(exc)) from exc
        return {"path": str(destination), "previous_path": str(current), "parent": str(current.parent)}
    if action == "delete":
        try:
            current.rmdir()
        except OSError as exc:
            raise ValueError(_folder_os_error(exc)) from exc
        return {"deleted": str(current), "parent": str(current.parent)}
    raise ValueError("unknown folder action")


def workspace_directory_browser(path=None, *, protected_paths=()):
    """List directories visible to this server for the local folder picker."""
    if not path:
        roots = ([f"{drive}:\\" for drive in string.ascii_uppercase
                  if Path(f"{drive}:\\").is_dir()] if os.name == "nt" else ["/"])
        return {"path": "", "parent": None, "roots": roots, "directories": [], "can_create": False}
    current = _folder_directory(path)
    parent = current.parent
    directories = []
    try:
        for entry in current.iterdir():
            try:
                if entry.is_dir():
                    mutation_error = ""
                    try:
                        workspace_folder_target(str(entry), protected_paths=protected_paths)
                        if not os.access(current, os.W_OK):
                            mutation_error = "this folder is read-only"
                    except ValueError as exc:
                        mutation_error = str(exc)
                    directories.append({"name": entry.name, "path": str(entry), "mutable": not mutation_error, "mutation_error": mutation_error})
            except (OSError, RuntimeError):
                continue
    except OSError as exc:
        raise ValueError(f"cannot list folder: {exc}") from exc
    directories.sort(key=lambda item: item["name"].casefold())
    selection_error = ""
    try:
        workspace_path({"workspace": str(current)}, create_default=False)
    except ValueError as exc:
        selection_error = str(exc)
    mutation_error = ""
    try:
        _folder_scope(current, protected_paths, creating=True)
        if not os.access(current, os.W_OK):
            mutation_error = "this folder is read-only"
    except ValueError as exc:
        mutation_error = str(exc)
    return {
        "path": str(current),
        "parent": None if parent == current else str(parent),
        "roots": [],
        "directories": directories[:500],
        "truncated": len(directories) > 500,
        "selectable": not selection_error,
        "selection_error": selection_error,
        "can_create": not mutation_error,
        "mutation_error": mutation_error,
    }
