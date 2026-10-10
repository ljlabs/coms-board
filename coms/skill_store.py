"""Safe filesystem storage for repository-root agent skills."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

APP_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = APP_ROOT / "skills"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
FILE_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,127}\Z")
MAX_FILE_BYTES = 1024 * 1024
MAX_SKILL_BYTES = 8 * 1024 * 1024
MAX_SKILL_FILES = 100
MAX_SKILL_DIRECTORIES = 100
WINDOWS_RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{prefix}{n}' for prefix in ('COM', 'LPT') for n in range(1, 10)}


def _root() -> Path:
    if SKILLS_ROOT.is_symlink():
        raise ValueError("the root skills directory cannot be a symbolic link")
    try:
        SKILLS_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"cannot access the root skills directory: {exc}") from exc
    return SKILLS_ROOT.resolve()


def _slug_dir(slug: str) -> Path:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError("skill slug must use lowercase letters, numbers and hyphens")
    root = _root()
    folder = root / slug
    if folder.is_symlink():
        raise ValueError("skill folders cannot be symbolic links")
    resolved = folder.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("skill path must stay inside the root skills directory")
    return folder


@contextmanager
def _skill_lock(slug: str):
    root = _root()
    _slug_dir(slug)
    lock_dir = root / ".locks"
    if lock_dir.is_symlink():
        raise ValueError("skill lock directory cannot be a symbolic link")
    lock_dir.mkdir(exist_ok=True)
    path = lock_dir / (slug + ".lock")
    if path.is_symlink():
        raise ValueError("skill lock file cannot be a symbolic link")
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise ValueError("skill file path must be a relative slash-separated path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in value.split('/')):
        raise ValueError("skill file path contains an invalid directory")
    if any(part.startswith(".") or not FILE_PART_RE.fullmatch(part) for part in path.parts):
        raise ValueError("skill file path contains a hidden or invalid name")
    if any(part.endswith(('.', ' ')) or part.split('.')[0].upper() in WINDOWS_RESERVED_NAMES for part in path.parts):
        raise ValueError("skill file path contains a name that is invalid on Windows")
    if path.as_posix() == "SKILL.md.disabled":
        raise ValueError("reserved skill metadata file")
    return path


def _file_path(slug: str, relative: str) -> Path:
    folder = _slug_dir(slug)
    if not folder.is_dir():
        raise FileNotFoundError(f"skill {slug!r} not found")
    path = folder
    for part in _safe_relative(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("skill files cannot be symbolic links")
    resolved = path.resolve()
    if not resolved.is_relative_to(folder.resolve()):
        raise ValueError("skill file must stay inside its skill folder")
    return path


def _frontmatter(content: str) -> dict[str, str]:
    match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", content, re.S)
    if not match:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("''", "'")
        values[key] = value
    return values


def _metadata(content: str, slug: str) -> tuple[str, str]:
    fields = _frontmatter(content)
    heading = re.search(r"(?m)^#\s+(.+?)\s*#*\s*$", content)
    title = fields.get("title") or (heading.group(1).strip() if heading else fields.get("name", slug.replace("-", " ").title()))
    description = fields.get("description", "").strip()
    if not description:
        text = content.split("---", 2)[-1] if content.startswith("---") else content
        text = re.sub(r"(?m)^#{1,6}\s+.*$", "", text)
        text = re.sub(r"(?m)^[-*+]\s+", "", text)
        description = " ".join(text.split())[:280]
    return title, description


def _version(content: bytes) -> int:
    return int(hashlib.sha256(content).hexdigest()[:12], 16)


def _stamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _skill_files(folder: Path) -> list[dict]:
    files = []
    for current, dirs, names in os.walk(folder, followlinks=False):
        here = Path(current)
        dirs[:] = sorted(name for name in dirs if not (here / name).is_symlink() and not name.startswith("."))
        for name in sorted(names):
            path = here / name
            if name == ".disabled" or path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(folder).as_posix()
            try:
                _safe_relative(relative)
                size = path.stat().st_size
            except (ValueError, OSError):
                continue
            files.append({"path": relative, "size": size, "binary": path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".bin"}})
    return files


def _skill_directories(folder: Path) -> list[str]:
    directories = []
    for current, dirs, _ in os.walk(folder, followlinks=False):
        here = Path(current)
        allowed = []
        for name in sorted(dirs):
            path = here / name
            if path.is_symlink():
                continue
            try:
                relative = _safe_relative(path.relative_to(folder).as_posix()).as_posix()
            except ValueError:
                continue
            allowed.append(name)
            directories.append(relative)
        dirs[:] = allowed
    return sorted(directories)


def _tree_version(folder: Path, files: list[dict], directories: list[str]) -> int:
    entries = [('directory', name) for name in directories]
    for item in files:
        stat = folder.joinpath(*PurePosixPath(item['path']).parts).stat()
        entries.append(('file', item['path'], stat.st_size, stat.st_mtime_ns, stat.st_ino))
    return _version(json.dumps(entries, separators=(',', ':')).encode('utf-8'))


def _check_tree_version(slug: str, expected_version) -> dict:
    skill = get_skill(slug)
    if type(expected_version) is not int or expected_version != skill['tree_version']:
        raise ValueError("Skill files changed; refresh the file tree before trying again.")
    return skill


def _ensure_available(path: Path, source: Path | None = None) -> None:
    if not path.parent.is_dir():
        raise FileNotFoundError("destination folder does not exist")
    for sibling in path.parent.iterdir():
        if sibling.name.casefold() == path.name.casefold() and sibling != source:
            raise ValueError("a file or folder with that name already exists")


def list_skills() -> list[dict]:
    root = _root()
    skills = []
    for folder in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if folder.name.startswith(".") or folder.is_symlink() or not folder.is_dir() or not SLUG_RE.fullmatch(folder.name):
            continue
        manifest = folder / "SKILL.md"
        if manifest.is_symlink() or not manifest.is_file():
            continue
        try:
            raw = manifest.read_bytes()
            if len(raw) > MAX_FILE_BYTES:
                continue
            content = raw.decode("utf-8")
            title, description = _metadata(content, folder.name)
            files = _skill_files(folder)
            skills.append({"slug": folder.name, "title": title, "description": description,
                           "version": _version(raw), "updated_at": _stamp(manifest),
                           "size": sum(item["size"] for item in files), "file_count": len(files),
                           "enabled": not (folder / ".disabled").exists(), "files": files})
        except (OSError, UnicodeDecodeError):
            continue
    return skills


def get_skill(slug: str) -> dict:
    folder = _slug_dir(slug)
    manifest = _file_path(slug, "SKILL.md")
    if not manifest.is_file():
        raise FileNotFoundError(f"skill {slug!r} not found")
    raw = manifest.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("SKILL.md is larger than the 1 MiB limit")
    content = raw.decode("utf-8")
    title, description = _metadata(content, slug)
    files = _skill_files(folder)
    directories = _skill_directories(folder)
    return {"slug": slug, "title": title, "description": description,
            "body": content, "version": _version(raw), "updated_at": _stamp(manifest),
            "size": sum(item["size"] for item in files),
            "file_count": len(files), "enabled": not (folder / ".disabled").exists(),
            "files": files, "directories": directories,
            "tree_version": _tree_version(folder, files, directories)}


def get_file(slug: str, relative: str) -> dict:
    path = _file_path(slug, relative)
    if not path.is_file():
        raise FileNotFoundError(f"skill file {relative!r} not found")
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("skill files are limited to 1 MiB")
    try:
        content = raw.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        content, binary = "", True
    return {"path": _safe_relative(relative).as_posix(), "content": content,
            "version": _version(raw), "size": len(raw), "binary": binary}


def _compose(slug: str, title: str, description: str, body: str) -> str:
    if body.startswith("---\n") or body.startswith("---\r\n"):
        return body if body.endswith("\n") else body + "\n"
    inferred = _metadata(body, slug)[1] if body.strip() else ""
    detail = (description or inferred or title).strip()[:280]
    heading = title.strip()
    remainder = body.strip()
    return ("---\n" + f"name: {slug}\n" + "description: " + json.dumps(detail, ensure_ascii=False) +
            "\n---\n\n" + f"# {heading}\n\n" + (remainder + "\n" if remainder else ""))


def _atomic_write(path: Path, raw: bytes) -> None:
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("skill files are limited to 1 MiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(raw)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def save_skill(slug: str, title: str, description: str, body: str, version=None) -> tuple[dict, bool]:
    if not isinstance(title, str) or not title.strip():
        raise ValueError("skill title is required")
    if not isinstance(description, str) or not isinstance(body, str) or not body.strip():
        raise ValueError("skill description and instructions are required")
    with _skill_lock(slug):
        folder = _slug_dir(slug)
        manifest = folder / "SKILL.md"
        exists = manifest.is_file() and not manifest.is_symlink()
        if exists:
            current = get_skill(slug)
            if type(version) is not int or version != current["version"]:
                raise ValueError("Skill changed; read the current file before updating it.")
        elif folder.exists():
            raise ValueError("a folder with this skill slug already exists but has no SKILL.md")
        elif version not in (None, 0):
            raise ValueError("new skills start without a version")
        else:
            folder.mkdir()
        raw = _compose(slug, title, description, body).encode("utf-8")
        _atomic_write(manifest, raw)
        return get_skill(slug), not exists


def write_file(slug: str, relative: str, content: str, expected_version=None) -> dict:
    if not isinstance(content, str):
        raise ValueError("skill file content must be text")
    with _skill_lock(slug):
        path = _file_path(slug, relative)
        if path.exists():
            if not path.is_file():
                raise ValueError("skill file path must name a regular file")
            if expected_version is not None and _version(path.read_bytes()) != expected_version:
                raise ValueError("Skill file changed; refresh before saving it.")
        elif relative != "SKILL.md" and not path.parent.exists():
            raise FileNotFoundError("skill file folder does not exist")
        _atomic_write(path, content.encode("utf-8"))
        return get_file(slug, relative)


def create_entry(slug: str, relative: str, kind: str, tree_version, content: str = '') -> dict:
    if kind not in ('file', 'folder'):
        raise ValueError("entry kind must be file or folder")
    if not isinstance(content, str):
        raise ValueError("skill file content must be text")
    if kind == 'folder' and content:
        raise ValueError("folders cannot contain text")
    with _skill_lock(slug):
        skill = _check_tree_version(slug, tree_version)
        path = _file_path(slug, relative)
        _ensure_available(path)
        if kind == 'folder':
            if len(skill['directories']) >= MAX_SKILL_DIRECTORIES:
                raise ValueError(f"skills are limited to {MAX_SKILL_DIRECTORIES} folders")
            path.mkdir()
        else:
            if skill['file_count'] >= MAX_SKILL_FILES:
                raise ValueError(f"skills are limited to {MAX_SKILL_FILES} files")
            raw = content.encode('utf-8')
            if skill['size'] + len(raw) > MAX_SKILL_BYTES:
                raise ValueError("a skill cannot exceed 8 MiB")
            _atomic_write(path, raw)
        return {**get_skill(slug), 'path': _safe_relative(relative).as_posix(), 'kind': kind}


def move_entry(slug: str, source: str, destination: str, tree_version) -> dict:
    with _skill_lock(slug):
        _check_tree_version(slug, tree_version)
        origin = _file_path(slug, source)
        target = _file_path(slug, destination)
        if source.casefold() == 'skill.md' or destination.casefold() == 'skill.md':
            raise ValueError("the root SKILL.md cannot be renamed, moved or replaced")
        if source == destination:
            raise ValueError("choose a different destination")
        if not origin.exists():
            raise FileNotFoundError(f"skill entry {source!r} not found")
        if not origin.is_file() and not origin.is_dir():
            raise ValueError("only regular files and folders can be moved")
        kind = 'folder' if origin.is_dir() else 'file'
        if kind == 'folder':
            if target.parent.resolve().is_relative_to(origin.resolve()):
                raise ValueError("a folder cannot be moved inside itself")
            for current, dirs, names in os.walk(origin, followlinks=False):
                if any((Path(current) / name).is_symlink() for name in dirs + names):
                    raise ValueError("cannot move a folder containing symbolic links")
        _ensure_available(target, origin)
        origin.rename(target)
        return {**get_skill(slug), 'source': source, 'path': _safe_relative(destination).as_posix(), 'kind': kind}


def set_enabled(slug: str, enabled: bool) -> dict:
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    with _skill_lock(slug):
        folder = _slug_dir(slug)
        if not (folder / "SKILL.md").is_file():
            raise FileNotFoundError(f"skill {slug!r} not found")
        marker = folder / ".disabled"
        if marker.is_symlink():
            raise ValueError("skill state marker cannot be a symbolic link")
        if enabled:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
        else:
            marker.write_text("disabled\n", encoding="utf-8")
        return get_skill(slug)


def delete_skill(slug: str) -> None:
    with _skill_lock(slug):
        folder = _slug_dir(slug)
        if not (folder / "SKILL.md").is_file():
            raise FileNotFoundError(f"skill {slug!r} not found")
        for current, dirs, files in os.walk(folder, followlinks=False):
            here = Path(current)
            if any((here / name).is_symlink() for name in dirs + files):
                raise ValueError("cannot delete a skill folder that contains symbolic links")
        shutil.rmtree(folder)


def import_skill(slug: str, files: list[dict]) -> dict:
    folder = _slug_dir(slug)
    if not isinstance(files, list) or not files or len(files) > MAX_SKILL_FILES:
        raise ValueError(f"an import must contain between 1 and {MAX_SKILL_FILES} files")
    prepared: list[tuple[PurePosixPath, bytes]] = []
    total = 0
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("each imported file needs a path and base64 content")
        relative = _safe_relative(item.get("path"))
        key = relative.as_posix().lower()
        if key in seen:
            raise ValueError("the import contains duplicate file paths")
        seen.add(key)
        encoded = item.get("content_base64")
        if not isinstance(encoded, str):
            raise ValueError("imported files must be base64 encoded")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError(f"invalid base64 content for {relative}") from exc
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(f"{relative} is larger than the 1 MiB per-file limit")
        total += len(raw)
        if total > MAX_SKILL_BYTES:
            raise ValueError("an imported skill cannot exceed 8 MiB")
        prepared.append((relative, raw))
    if "SKILL.md" not in {path.as_posix() for path, _ in prepared}:
        raise ValueError("the imported folder must contain a root SKILL.md file")
    manifest = next(raw for path, raw in prepared if path.as_posix() == "SKILL.md")
    try:
        if not manifest.decode("utf-8").strip():
            raise ValueError("the imported SKILL.md cannot be empty")
    except UnicodeDecodeError as exc:
        raise ValueError("the imported SKILL.md must be UTF-8 text") from exc
    root = _root()
    with _skill_lock(slug):
        if folder.exists():
            raise ValueError("a skill with this slug already exists")
        staging = root / ("." + slug + ".import-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            for relative, raw in prepared:
                destination = staging.joinpath(*relative.parts)
                if not destination.resolve().is_relative_to(staging.resolve()):
                    raise ValueError("import path must stay inside the skill folder")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(raw)
            os.replace(staging, folder)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return get_skill(slug)
