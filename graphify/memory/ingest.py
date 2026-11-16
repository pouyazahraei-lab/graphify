"""Incremental ingestion: scan registered roots, process only what changed.

Change detection is (size, mtime) first, then SHA-256, so a touched-but-equal
file costs one hash and no re-parse. Each file is replaced inside a single
transaction. Files that disappeared from a root are removed with everything
derived from them. Every action is written to the run log.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path

from graphify.memory import sources as S
from graphify.memory.store import MemoryStore

DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", ".obsidian", ".trash", ".Trash", "node_modules", "__pycache__",
    ".venv", "venv", "env", ".tox", ".mypy_cache", ".pytest_cache", "graphify-out",
    "dist", "build", ".idea", ".vscode", "$RECYCLE.BIN", "System Volume Information",
}
MAX_FILE_BYTES = 200 * 1024 * 1024
IGNORE_FILE = ".memoryignore"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _key(path: Path) -> str:
    """Stable storage key: absolute, resolved, forward slashes (Windows-safe)."""
    return Path(os.path.abspath(path)).as_posix()


def _load_ignore(root: Path) -> list[str]:
    f = root / IGNORE_FILE
    if not f.is_file():
        return []
    return [ln.strip() for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _ignored(rel: str, patterns: list[str]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    for p in patterns:
        p = p.rstrip("/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) or rel.startswith(p + "/"):
            return True
    return False


def scan_root(root: Path) -> tuple[list[Path], list[Path], list[tuple[Path, str]]]:
    """Return (candidate files, git repositories, skipped (path, reason))."""
    from graphify.detect import _is_sensitive

    patterns = _load_ignore(root)
    files: list[Path] = []
    repos: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    if root.is_file():
        return [root], [], []
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if (d / ".git").exists():
            repos.append(d)
        keep = []
        for name in dirnames:
            rel = (d / name).relative_to(root).as_posix()
            if name in DEFAULT_EXCLUDE_DIRS or (name.startswith(".") and name != ".") or _ignored(rel, patterns):
                continue
            keep.append(name)
        dirnames[:] = sorted(keep)
        for name in sorted(filenames):
            p = d / name
            rel = p.relative_to(root).as_posix()
            if name == IGNORE_FILE or name.startswith("~$") or _ignored(rel, patterns):
                continue
            if p.suffix.lower() not in S.SUPPORTED_EXTS:
                continue
            if _is_sensitive(p):
                skipped.append((p, "looks like a secrets/credentials file"))
                continue
            files.append(p)
    return files, repos, skipped


class Ingestor:
    def __init__(self, store: MemoryStore, *, git_history: bool = True, verbose: bool = False):
        self.store = store
        self.git_history = git_history
        self.verbose = verbose

    def _say(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def ingest(self, roots: list[str | Path] | None = None, *, register: bool = True, force: bool = False) -> dict:
        st = self.store
        if roots:
            root_paths = [Path(os.path.abspath(Path(r).expanduser())) for r in roots]
            for r in root_paths:
                if not r.exists():
                    raise FileNotFoundError(f"source not found: {r}")
                if register:
                    st.add_root(Path(_key(r)))
        else:
            root_paths = [Path(r) for r in st.roots()]
        if not root_paths:
            raise ValueError("no sources registered; pass a folder or file to ingest")

        run = st.start_run(note=", ".join(_key(r) for r in root_paths))
        counts = {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0, "skipped": 0, "errors": 0}
        changed_sources: set[int] = set()
        status = "ok"
        try:
            for root in root_paths:
                root_key = _key(root)
                if not root.exists():
                    st.log(run, "warn", root_key, "root_missing", "registered source folder is not reachable; nothing deleted")
                    self._say(f"! {root_key} is not reachable (unplugged drive?) - skipped, nothing deleted")
                    continue
                files, repos, skipped = scan_root(root)
                seen: set[str] = set()
                for p, reason in skipped:
                    st.log(run, "info", _key(p), "skip", reason)
                    counts["skipped"] += 1
                for p in files:
                    k = _key(p)
                    seen.add(k)
                    self._process_file(run, root_key, p, k, counts, changed_sources, force)
                if self.git_history:
                    for repo in repos:
                        k = _key(repo) + "#git-log"
                        seen.add(k)
                        self._process_git(run, root_key, repo, k, counts, changed_sources, force)
                for row in st.files_under(root_key):
                    if row["path"] not in seen:
                        with st.tx() as db:
                            st.delete_file_sources(db, row["path"])
                            db.execute("UPDATE files SET status = 'deleted', deleted_at = datetime('now') WHERE path = ?",
                                       (row["path"],))
                        st.log(run, "info", row["path"], "deleted", "source no longer present")
                        counts["deleted"] += 1
                        self._say(f"- {row['path']}")
            links = st.resolve_links()
            full = counts["deleted"] > 0
            inferred = st.match_names(None if full else changed_sources)
            removed = st.garbage_collect()
            st.log(run, "info", None, "derive",
                   f"wikilinks resolved={links['resolved']} unresolved={links['unresolved']}; "
                   f"name-match mentions={inferred}; orphan entities removed={removed}")
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        except Exception as exc:
            status = "failed"
            st.log(run, "error", None, "run_failed", repr(exc))
            raise
        finally:
            if counts["errors"] and status == "ok":
                status = "ok_with_errors"
            st.finish_run(run, counts, status)
        counts["run_id"] = run
        counts["status"] = status
        return counts

    # ------------------------------------------------------------------ per file
    def _process_file(self, run, root_key, p: Path, k: str, counts, changed_sources, force) -> None:
        st = self.store
        try:
            stat = p.stat()
        except OSError as exc:
            st.log(run, "error", k, "stat_failed", repr(exc))
            counts["errors"] += 1
            return
        row = st.file_row(k)
        if stat.st_size > MAX_FILE_BYTES:
            if not row or row["status"] != "skipped":
                with st.tx() as db:
                    st.delete_file_sources(db, k)
                    st.mark_file(db, k, root_key, stat.st_size, stat.st_mtime, "", "skipped", "file too large")
                st.log(run, "info", k, "skip", "file too large")
            counts["skipped"] += 1
            return
        if (not force and row and row["status"] != "deleted" and row["size"] == stat.st_size
                and row["mtime"] == stat.st_mtime):
            counts["unchanged" if row["status"] == "active" else "skipped"] += 1
            return
        try:
            digest = sha256_file(p)
        except OSError as exc:
            st.log(run, "error", k, "read_failed", repr(exc))
            counts["errors"] += 1
            return
        if not force and row and row["status"] != "deleted" and row["sha256"] == digest:
            st.touch_file_stat(k, stat.st_size, stat.st_mtime)
            counts["unchanged" if row["status"] == "active" else "skipped"] += 1
            return
        is_new = not row or row["status"] == "deleted"
        try:
            parsed = S.parse_file(p, k)
        except S.SkipSource as exc:
            with st.tx() as db:
                st.delete_file_sources(db, k)
                st.mark_file(db, k, root_key, stat.st_size, stat.st_mtime, digest, "skipped", str(exc))
            st.log(run, "info", k, "skip", str(exc))
            counts["skipped"] += 1
            return
        except Exception as exc:  # a broken file must not stop the run
            with st.tx() as db:
                st.mark_file(db, k, root_key, stat.st_size, stat.st_mtime, digest, "error", repr(exc)[:500])
            st.log(run, "error", k, "parse_failed", repr(exc))
            counts["errors"] += 1
            self._say(f"! {k}: {exc}")
            return
        with st.tx() as db:
            st.delete_file_sources(db, k)
            for unit, ps in self._units(parsed):
                changed_sources.add(st.write_source(db, k, unit, ps))
            st.mark_file(db, k, root_key, stat.st_size, stat.st_mtime, digest, "active")
        action = "added" if is_new else "updated"
        counts[action] += 1
        st.log(run, "info", k, action, f"{sum(len(x.chunks) for x in parsed)} chunks")
        self._say(f"{'+' if is_new else '~'} {k}")

    @staticmethod
    def _units(parsed):
        if len(parsed) == 1:
            yield "", parsed[0]
            return
        for ps in parsed:
            yield str(ps.meta.get("conversation_id") or ps.title), ps

    def _process_git(self, run, root_key, repo: Path, k: str, counts, changed_sources, force) -> None:
        st = self.store
        head = S.git_head(repo)
        if head is None:
            return
        row = st.file_row(k)
        if not force and row and row["status"] == "active" and row["sha256"] == head:
            counts["unchanged"] += 1
            return
        try:
            parsed = S.parse_git_log(repo, k)
        except S.SkipSource as exc:
            st.log(run, "info", k, "skip", str(exc))
            counts["skipped"] += 1
            return
        with st.tx() as db:
            st.delete_file_sources(db, k)
            changed_sources.add(st.write_source(db, k, "", parsed))
            st.mark_file(db, k, root_key, 0, 0.0, head, "active")
        counts["added" if not row or row["status"] == "deleted" else "updated"] += 1
        st.log(run, "info", k, "git_log", f"{len(parsed.chunks)} commits at {head[:12]}")
        self._say(f"+ {k} ({len(parsed.chunks)} commits)")
