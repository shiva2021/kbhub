"""Share knowledge bases between devices through git. Standard library only (the session hooks
import this module).

A *space* is a folder of knowledge bases tracked by a git repository whose git directory lives
outside that folder, in `~/.kbhub-config/spaces/<name>.git`. The folder never gets a `.git`, so
every rule kbhub has about code repos keeps working unchanged. Only the markdown travels: each
device builds its own index from its own copy.

What is shared is default-deny. The space's committed `.gitignore`, written only by this module,
ignores everything and re-includes the knowledge bases named on its `# shared:` lines, so every
device agrees on the list.

`sync_space` is the one sync: commit, fetch, rebase, repair, push. It never raises."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import tomllib
from pathlib import Path

from . import kbregistry, prefs
from .config import SPACE_NAME_RE, SUPERSEDED_RE, GlobalConfig, Space, _spaces

BRANCH = "main"
UPSTREAM = f"refs/remotes/origin/{BRANCH}"
IGNORE, ATTRS = ".gitignore", ".gitattributes"
CONFLICTS = ".kbhub-conflicts"
HEADER = "# Written by kbhub (`kbhub share add` / `kbhub share remove`). Do not edit by hand."
SHARED_PREFIX = "# shared: "
ATTRS_TEXT = ("# Written by kbhub. Entries are only ever appended to these files, so keep both sides.\n"
              "decisions.md merge=union\nlessons.md merge=union\n")
UNSAFE_REL = re.compile(r"[*?\[\]\\!#\n\r]|^\s|\s$")
_SPACES_RE = re.compile(r"(?ms)^\[\[spaces\]\]\s*\n.*?(?=^\[|\Z)")


# ------------------------------------------------------------------ plumbing

def git_dir(space: Space) -> Path:
    return prefs.prefs_home() / "spaces" / f"{space.name}.git"


def _paused_file(space: Space) -> Path:
    return git_dir(space) / "kbhub-paused"


def _host() -> str:
    name = re.sub(r"[^A-Za-z0-9-]+", "-", socket.gethostname().split(".")[0]).strip("-")
    return name or "device"


def _git(space: Space, *args: str, timeout: float | None = None, stdin: str | None = None) -> tuple[int, str, str]:
    """Run git against the space; returns (exit code, stdout, stderr). Never prompts, never
    raises: a missing git is 127 and a timeout is 124."""
    cmd = ["git", "--git-dir", str(git_dir(space)), "--work-tree", str(space.path),
           "-c", "commit.gpgsign=false", "-c", "core.quotepath=false", *args]
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"})
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=8")
    if not _has_identity(env):       # commits and rebases both need one
        for who in ("AUTHOR", "COMMITTER"):
            env.setdefault(f"GIT_{who}_NAME", "kbhub")
            env.setdefault(f"GIT_{who}_EMAIL", f"kbhub@{_host()}")
    try:
        p = subprocess.run(cmd, cwd=space.path, capture_output=True, encoding="utf-8", errors="surrogateescape",
                           timeout=timeout, env=env, input=stdin)
        return p.returncode, p.stdout, p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 1, "", str(exc)


_identity: dict[tuple, bool] = {}


def _has_identity(env: dict) -> bool:
    """Whether git knows who the user is (asked once per git configuration)."""
    key = (env.get("GIT_CONFIG_GLOBAL"), env.get("HOME"))
    if key not in _identity:
        try:
            p = subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True, env=env)
            _identity[key] = p.returncode == 0 and bool(p.stdout.strip())
        except OSError:
            _identity[key] = False
    return _identity[key]


def _must(space: Space, *args: str, **kw) -> str:
    code, out, err = _git(space, *args, **kw)
    if code != 0:
        raise ValueError(f"git {' '.join(args[:2])} failed: {err or out.strip() or code}")
    return out


def _z(out: str) -> list[str]:
    return [p for p in out.split("\0") if p]


def _count(space: Space, spec: str) -> int:
    code, out, _ = _git(space, "rev-list", "--count", spec)
    return int(out.strip() or 0) if code == 0 else 0


def _has_ref(space: Space, ref: str) -> bool:
    return _git(space, "rev-parse", "--verify", "--quiet", ref)[0] == 0


def _rebasing(space: Space) -> bool:
    return (git_dir(space) / "rebase-merge").exists() or (git_dir(space) / "rebase-apply").exists()


def _abort_rebase(space: Space) -> None:
    """Back to where the branch was before the rebase. Stale state that git itself cannot abort
    (an interrupted run can leave an empty folder) is cleared, which leaves the branch as it is."""
    if _git(space, "rebase", "--abort")[0] != 0:
        _git(space, "rebase", "--quit")
        for name in ("rebase-merge", "rebase-apply"):
            shutil.rmtree(git_dir(space) / name, ignore_errors=True)


def _guard_ignore(space: Space) -> None:
    """The ignore file is what keeps unshared folders on this device, so never stage without it:
    a missing one is put back from the last commit, and one kbhub did not write stops the sync."""
    ignore = space.path / IGNORE
    if not ignore.exists() and _git(space, "checkout", "HEAD", "--", IGNORE)[0] != 0:
        raise ValueError(f"{ignore} is missing and could not be restored; nothing was shared")
    if not _is_ours(ignore):
        raise ValueError(f"{ignore} was not written by kbhub; nothing was shared. Restore it with "
                         f"`kbhub share git -- checkout HEAD -- {IGNORE}`")


def _commit(space: Space, message: str) -> bool:
    """Stage everything the ignore file lets through and commit it. True when a commit was made.
    Second guard, whatever the ignore file says: a file outside the shared folders is never
    committed."""
    _guard_ignore(space)
    _must(space, "add", "-A")
    allowed = shared(space)
    staged = _z(_must(space, "diff", "--cached", "--name-only", "--diff-filter=d", "-z"))
    outside = [f for f in staged if f not in (IGNORE, ATTRS)
               and not any(f == r or f.startswith(r + "/") for r in allowed)]
    if outside:
        _must(space, "reset", "--quiet")
        shown = ", ".join(outside[:5]) + (f" and {len(outside) - 5} more" if len(outside) > 5 else "")
        raise ValueError(f"refused to share files outside the shared knowledge bases: {shown}. "
                         f"{space.path / IGNORE} may have been edited by hand")
    if _git(space, "diff", "--cached", "--quiet")[0] == 0:
        return False
    _must(space, "commit", "--quiet", "--no-verify", "-m", message)
    return True


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="surrogateescape")


# ------------------------------------------------------------------ the allowlist

def shared(space: Space) -> list[str]:
    """Folders (relative to the space) that are shared, read from the committed ignore file."""
    try:
        text = (space.path / IGNORE).read_text(encoding="utf-8")
    except OSError:
        return []
    return sorted(_listed(text)) if text.startswith(HEADER) else []


def ignore_text(rels: list[str]) -> str:
    """The ignore file for a set of shared folders. Git cannot re-include a path under an ignored
    folder, so each parent level is re-included and its other children ignored again."""
    rels = sorted(set(rels))
    lines = [HEADER, "# Only the folders on the `shared` lines leave this device.",
             *(SHARED_PREFIX + r for r in rels), "/*", f"!/{IGNORE}", f"!/{ATTRS}"]
    tree: dict = {}
    for rel in rels:
        node = tree
        for part in rel.split("/"):
            node = node.setdefault(part, {})
        node[""] = True                                   # a shared folder ends here

    def emit(node: dict, prefix: str) -> None:
        for name in sorted(k for k in node if k):
            here, child = f"{prefix}/{name}", node[name]
            lines.append(f"!{here}/")
            if "" not in child:
                lines.append(f"{here}/*")
                emit(child, here)

    emit(tree, "")
    return "\n".join([*lines, f"{CONFLICTS}/", ".DS_Store"]) + "\n"


def _is_ours(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").startswith(HEADER)
    except OSError:
        return False


def _kb_root(space: Space, path: str) -> str:
    """The shared folder a file (relative to the space) belongs to, or "" for the space root."""
    return max((r for r in shared(space) if path == r or path.startswith(r + "/")), key=len, default="")


def space_of(cfg: GlobalConfig, folder: Path) -> Space | None:
    """The space that shares `folder` (a knowledge base's folder), if any."""
    real = Path(os.path.realpath(folder))
    for space in cfg.spaces:
        try:
            rel = real.relative_to(os.path.realpath(space.path)).as_posix()
        except ValueError:
            continue
        if _kb_root(space, rel):
            return space
    return None


# ------------------------------------------------------------------ config.toml

def _save_spaces(cfg: GlobalConfig, spaces: list[Space]) -> GlobalConfig:
    """Rewrite the [[spaces]] tables of config.toml and reload it."""
    text = cfg.path.read_text(encoding="utf-8") if cfg.path.is_file() else ""
    head = _SPACES_RE.sub("", text).rstrip("\n")
    blocks = []
    for s in spaces:
        block = f"[[spaces]]\nname = {json.dumps(s.name)}\npath = {json.dumps(str(s.path))}\n"
        blocks.append(block + (f"remote = {json.dumps(s.remote)}\n" if s.remote else ""))
    new = "\n\n".join(filter(None, [head, *(b.rstrip("\n") for b in blocks)])) + "\n"
    try:
        ok = _spaces(cfg.path, tomllib.loads(new)) == spaces
    except Exception:
        ok = False
    if not ok:
        raise ValueError(f"could not update the [[spaces]] tables in {cfg.path}; edit it by hand")
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    cfg.path.write_text(new, encoding="utf-8")
    return GlobalConfig.load(cfg.path)


def find_space(cfg: GlobalConfig, name: str | None = None) -> Space:
    if not cfg.spaces:
        raise ValueError("no shared space yet: run `kbhub share init` (first device) or `kbhub share join URL`")
    if name is None:
        if len(cfg.spaces) > 1:
            raise ValueError("more than one space: name it with --space " + " | ".join(s.name for s in cfg.spaces))
        return cfg.spaces[0]
    space = next((s for s in cfg.spaces if s.name == name), None)
    if space is None:
        raise ValueError(f"no space named {name!r}. Spaces: {', '.join(s.name for s in cfg.spaces)}")
    return space


def _new_space(cfg: GlobalConfig, name: str, path, remote: str | None) -> Space:
    if not SPACE_NAME_RE.match(name or ""):
        raise ValueError("a space name is lower-case letters, digits, - or _")
    folder = Path(os.path.abspath(os.path.expanduser(str(path or cfg.new_project_parent))))
    if kbregistry.git_root(folder) is not None:
        raise ValueError(f"{folder} is inside a git repository; a space must be a folder of its own")
    for other in cfg.spaces:
        a, b = os.path.realpath(folder), os.path.realpath(other.path)
        if a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep):
            raise ValueError(f"{folder} overlaps the space {other.name!r} at {other.path}")
    space = Space(name, folder, remote or None)
    if git_dir(space).exists():
        raise ValueError(f"{git_dir(space)} already exists; delete it or choose another name")
    return space


def _create_repo(space: Space) -> None:
    space.path.mkdir(parents=True, exist_ok=True)
    git_dir(space).parent.mkdir(parents=True, exist_ok=True)
    _must(space, "init", "--quiet")
    _must(space, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
    _must(space, "config", "core.bare", "false")
    if space.remote:
        _must(space, "remote", "add", "origin", space.remote)


# ------------------------------------------------------------------ commands

def init(cfg: GlobalConfig, name: str = "personal", path=None, remote: str | None = None) -> tuple[GlobalConfig, Space]:
    """Start a space on this device, or give an existing one its remote. Shares nothing yet."""
    existing = next((s for s in cfg.spaces if s.name == name), None)
    if existing is not None:
        if not remote or path:
            raise ValueError(f"the space {name!r} already exists at {existing.path}")
        return set_remote(cfg, existing, remote)
    space = _new_space(cfg, name, path, remote)
    ignore = space.path / IGNORE
    if ignore.exists() and not _is_ours(ignore):
        raise ValueError(f"{ignore} already exists and was not written by kbhub; move it away first")
    if remote and _remote_has_notes(space, remote):
        raise ValueError(f"{remote} already holds notes: use `kbhub share join {remote}` on this device")
    _create_repo(space)
    _write_text(ignore, ignore_text(shared(space)))
    _write_text(space.path / ATTRS, ATTRS_TEXT)
    _commit(space, f"kbhub: start space {name}")
    return _save_spaces(cfg, [*cfg.spaces, space]), space


def _remote_has_notes(space: Space, remote: str) -> bool:
    try:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=8")
        p = subprocess.run(["git", "ls-remote", "--heads", remote, BRANCH], capture_output=True, text=True,
                           timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and bool(p.stdout.strip())


def set_remote(cfg: GlobalConfig, space: Space, remote: str) -> tuple[GlobalConfig, Space]:
    verb = ["remote", "set-url", "origin", remote] if _git(space, "remote", "get-url", "origin")[0] == 0 \
        else ["remote", "add", "origin", remote]
    _must(space, *verb)
    new = Space(space.name, space.path, remote)
    return _save_spaces(cfg, [new if s.name == space.name else s for s in cfg.spaces]), new


def _project(cfg: GlobalConfig, name: str):
    from .registry import discover_projects
    projects = discover_projects(cfg)[0]
    match = projects.get(name) or next((p for n, p in projects.items() if n.lower() == name.lower()), None)
    if match is None:
        raise ValueError(f"Unknown knowledge base {name!r}. Known: {', '.join(sorted(projects)) or '(none)'}")
    return match


def preview_add(cfg: GlobalConfig, name: str) -> dict:
    """Everything `add` would do, checked but not done: the space, the folder, the scope and the
    files that would leave this device."""
    project = _project(cfg, name)
    if project.repo is not None or project.source != "marker":
        raise ValueError(f"{project.name} is registered inside a code repo or by hand; only standalone knowledge "
                         "bases (a folder with a .kb.toml) can be shared. One inside a repo travels with its code.")
    real = Path(os.path.realpath(project.path))
    space = rel = None
    for s in cfg.spaces:
        try:
            rel, space = real.relative_to(os.path.realpath(s.path)).as_posix(), s
            break
        except ValueError:
            continue
    if space is None or rel in (None, "."):
        where = ", ".join(str(s.path) for s in cfg.spaces) or "none yet: run `kbhub share init`"
        raise ValueError(f"{project.path} is not inside a shared space ({where})")
    if UNSAFE_REL.search(rel):
        raise ValueError(f"the folder name {rel!r} has characters an ignore file cannot hold; rename it first")
    current = shared(space)
    if rel in current:
        raise ValueError(f"{project.name} is already shared in the space {space.name!r}")
    clash = next((r for r in current if r.startswith(rel + "/") or rel.startswith(r + "/")), None)
    if clash:
        raise ValueError(f"{rel} overlaps {clash}, which is already shared")
    files = []
    for folder, dirs, names in os.walk(real):
        if ".git" in dirs or ".git" in names:
            raise ValueError(f"{folder} holds a git repository of its own; it cannot be shared inside a space")
        dirs[:] = sorted(d for d in dirs if d != CONFLICTS)
        files.extend((Path(folder) / n).relative_to(real).as_posix() for n in sorted(names) if n != ".DS_Store")
    return {"space": space, "project": project, "rel": rel, "scope": project.scope, "files": files}


def _pin_scope(project) -> None:
    """Write the scope into .kb.toml, so it travels instead of depending on each device's folders."""
    path = project.path / ".kb.toml"
    text = path.read_text(encoding="utf-8")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML ({exc}); fix it before sharing") from None
    if raw.get("scope") == project.scope:
        return
    table = re.search(r"(?m)^[ \t]*\[", text)
    at = table.start() if table else len(text)
    head = text[:at]
    new = (head + ("" if not head or head.endswith("\n") else "\n") + f'scope = "{project.scope}"\n'
           + ("\n" if table else "") + text[at:])
    if tomllib.loads(new) != {**raw, "scope": project.scope}:
        raise ValueError(f'could not add the scope to {path}; add `scope = "{project.scope}"` by hand')
    path.write_text(new, encoding="utf-8")


def add(cfg: GlobalConfig, name: str, net_timeout: float = 60) -> dict:
    """Share a knowledge base: put it on the allowlist, pin its scope, and sync."""
    plan = preview_add(cfg, name)
    space = plan["space"]
    if not git_dir(space).is_dir():
        raise ValueError(f"the space {space.name!r} is not set up on this device")
    _pin_scope(plan["project"])
    _write_text(space.path / IGNORE, ignore_text([*shared(space), plan["rel"]]))
    return sync_space(space, net_timeout, message=f"kbhub: share {plan['rel']}")


def remove(cfg: GlobalConfig, name: str, net_timeout: float = 60) -> dict:
    """Stop sharing a knowledge base. Its files stay on this device; other devices drop their
    copy at their next sync, and the remote keeps the history."""
    for space in cfg.spaces:
        current = shared(space)
        rel = name if name in current else None
        if rel is None:
            try:
                project = _project(cfg, name)
                got = Path(os.path.realpath(project.path)).relative_to(os.path.realpath(space.path)).as_posix()
                rel = got if got in current else None
            except ValueError:
                rel = None
        if rel is None:
            continue
        _write_text(space.path / IGNORE, ignore_text([r for r in current if r != rel]))
        _must(space, "rm", "-r", "--cached", "--quiet", "--ignore-unmatch", "--", rel)
        return sync_space(space, net_timeout, message=f"kbhub: stop sharing {rel}")
    raise ValueError(f"{name!r} is not shared in any space")


def join(cfg: GlobalConfig, url: str, name: str = "personal", path=None, net_timeout: float = 300) -> dict:
    """Connect this device to a space that already exists on a remote. Files that are missing here
    are written; a local file that differs from the remote one is never overwritten: it is listed
    and the space stays paused until it is moved aside (or `kbhub share sync --keep-local`)."""
    if any(s.name == name for s in cfg.spaces):
        raise ValueError(f"the space {name!r} already exists on this device")
    space = _new_space(cfg, name, path, url)
    _create_repo(space)
    try:
        code, _, err = _git(space, "fetch", "--quiet", "origin", timeout=net_timeout)
        if code != 0:
            raise ValueError(f"could not fetch {url}: {err or 'no answer'}")
        if not _has_ref(space, UPSTREAM):
            raise ValueError(f"{url} has no notes yet: run `kbhub share init --remote {url}` on the first device")
        _must(space, "update-ref", f"refs/heads/{BRANCH}", UPSTREAM)
        _must(space, "read-tree", "HEAD")
    except ValueError:
        shutil.rmtree(git_dir(space), ignore_errors=True)
        raise
    landed, differing = _land(space)
    if differing:
        _write_text(_paused_file(space), "\n".join(differing) + "\n")
    return {"cfg": _save_spaces(cfg, [*cfg.spaces, space]), "space": space, "landed": landed, "differing": differing}


def _land(space: Space) -> tuple[int, list[str]]:
    """Write the tracked files that are missing on disk; report those that exist with other content."""
    missing = _z(_must(space, "ls-files", "-z", "--deleted"))
    differing = sorted(set(_z(_must(space, "ls-files", "-z", "--modified"))) - set(missing))
    if missing:
        _must(space, "checkout-index", "-z", "--stdin", stdin="\0".join(missing) + "\0")
    return len(missing), differing


# ------------------------------------------------------------------ sync

def sync_space(space: Space, net_timeout: float = 20, keep_local: bool = False, message: str | None = None) -> dict:
    """Commit, fetch, rebase, repair, push. Never raises. Only the network steps are time-boxed: a
    killed rebase would leave the copy half merged.

    Returns {"space", "ok", "pulled" (files that changed here), "pushed", "pending" (None, "pull",
    "push" or "paused"), "saved" (conflict copies written now), "conflicts" (all waiting conflict
    copies), "notes"}."""
    res = {"space": space.name, "ok": True, "pulled": [], "pushed": False, "pending": None, "saved": [],
           "conflicts": [], "notes": [], "skipped": False}
    if not git_dir(space).is_dir() or not space.path.is_dir():
        res.update(ok=False, notes=["not set up on this device (run `kbhub share join` or `kbhub share init`)"])
        return res
    try:
        with open(git_dir(space).parent / f"{space.name}.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:      # another session is syncing; its result is as good as ours
                res.update(skipped=True, notes=["another session is syncing"])
                return res
            try:
                _sync(space, res, net_timeout, keep_local, message or f"kbhub: notes from {_host()}")
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except Exception as exc:
        if _rebasing(space):
            _abort_rebase(space)
        res["ok"] = False
        res["notes"].append(f"sync failed: {exc}")
    res["conflicts"] = conflict_files(space)
    return res


def _sync(space: Space, res: dict, net_timeout: float, keep_local: bool, message: str) -> None:
    (git_dir(space) / "index.lock").unlink(missing_ok=True)   # we hold the space lock: a leftover is stale
    if _rebasing(space):                 # left behind by an interrupted run
        _abort_rebase(space)
    paused = _paused_file(space)
    if paused.exists():
        if not keep_local:
            listed = set(paused.read_text(encoding="utf-8").split("\n")) - {""}
            still = sorted(listed & set(_land(space)[1]))
            if still:
                res["pending"] = "paused"
                res["notes"].append("paused: these local files differ from the shared ones and were left alone: "
                                    + ", ".join(still) + ". Move them aside, or run `kbhub share sync --keep-local` "
                                    "to share the local versions.")
                return
        paused.unlink()
    _commit(space, message)
    if _git(space, "remote", "get-url", "origin")[0] != 0:
        res["notes"].append("no remote yet (`kbhub share init --remote URL`)")
        return
    code, _, err = _git(space, "fetch", "--quiet", "origin", timeout=net_timeout)
    if code != 0:
        res["pending"] = "pull"
        res["notes"].append("the remote did not answer in time" if code == 124 else f"fetch failed: {_last_line(err)}")
        return
    upstream = _has_ref(space, UPSTREAM)
    if upstream and _count(space, f"HEAD..{UPSTREAM}"):
        mine = _count(space, f"{UPSTREAM}..HEAD")
        incoming = _z(_must(space, "diff", "--name-only", "-z", f"HEAD...{UPSTREAM}"))
        blocked = _would_overwrite(space)
        if blocked:
            res["pending"] = "pull"
            res["notes"].append("not pulled: these local files are not shared yet but exist in the shared notes with "
                                "other content: " + ", ".join(blocked) + ". Move them aside and sync again.")
            return
        if not _rebase(space, res):
            res["pending"] = "pull"
            res["notes"].append("could not merge the incoming notes; nothing was changed")
            return
        res["pulled"] = incoming
        tidy = ignore_text(shared(space))    # two devices' lists merged line by line: make it whole again
        if _is_ours(space.path / IGNORE) and (space.path / IGNORE).read_text(encoding="utf-8") != tidy:
            _write_text(space.path / IGNORE, tidy)
        if mine:
            repair(space)
        _commit(space, "kbhub: tidy after merge")
    if not upstream or _count(space, f"{UPSTREAM}..HEAD"):
        code, _, err = _git(space, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}", timeout=net_timeout)
        if code == 0:
            res["pushed"] = True
        else:
            res["pending"] = "push"
            res["notes"].append("the remote did not answer in time" if code == 124 else f"push failed: {_last_line(err)}")


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no details"


def _would_overwrite(space: Space) -> list[str]:
    """Incoming new files that already exist here, untracked, with other content. Git treats an
    ignored file as expendable and would replace it silently, so these stop the pull."""
    added = _z(_must(space, "diff", "--name-only", "--diff-filter=A", "-z", f"HEAD...{UPSTREAM}"))
    here = [p for p in added if (space.path / p).is_file()]
    if not here:
        return []
    tracked = set(_z(_must(space, "ls-files", "-z", "--", *here)))
    out = []
    for path in here:
        if path in tracked:
            continue
        mine = _git(space, "hash-object", "--", path)[1].strip()
        theirs = _git(space, "rev-parse", f"{UPSTREAM}:{path}")[1].strip()
        if mine != theirs:
            out.append(path)
    return out


def _rebase(space: Space, res: dict) -> bool:
    """Replay the local commits on top of the remote ones, resolving every conflict by rule."""
    unmerged = lambda: _z(_git(space, "diff", "--name-only", "--diff-filter=U", "-z")[1])
    code = _git(space, "rebase", UPSTREAM)[0]
    for _ in range(500):
        if not _rebasing(space):
            return code == 0
        todo = unmerged()
        if code != 0 and not todo:
            break                        # stopped for a reason that is not a conflict: give up, lose nothing
        for path in todo:
            _resolve(space, path, res)
        code = _git(space, "rebase", "--continue")[0]
        if code != 0 and _rebasing(space) and not unmerged() and _git(space, "diff", "--cached", "--quiet")[0] == 0:
            code = _git(space, "rebase", "--skip")[0]     # every change of that commit is already upstream
    _abort_rebase(space)
    return False


def _stage(space: Space, n: int, path: str) -> str | None:
    code, out, _ = _git(space, "show", f":{n}:{path}")
    return out if code == 0 else None


def _resolve(space: Space, path: str, res: dict) -> None:
    """Settle one conflicted file. During a rebase stage 2 is the remote side and stage 3 the local
    commit being replayed. The remote version stays; the local one is saved as a conflict copy."""
    base, remote, local = (_stage(space, n, path) for n in (1, 2, 3))
    if path == IGNORE and remote is not None and local is not None:
        _write_text(space.path / path, ignore_text(_merge_lists(base or "", remote, local)))
        _must(space, "add", "--", path)
        return
    if remote is not None and local is not None and base is not None and Path(path).name == "STATE.md":
        merged = _merge_state(base, remote, local)
        if merged is not None:
            _write_text(space.path / path, merged)
            _must(space, "add", "--", path)
            return
    if local is not None and local != remote:
        res["saved"].append(_save_copy(space, path, local))
    if remote is None:
        _must(space, "rm", "--quiet", "--force", "--ignore-unmatch", "--", path)
    else:
        _must(space, "checkout", "--ours", "--", path)     # byte for byte, as the remote has it
        _must(space, "add", "--", path)


def _listed(text: str) -> set[str]:
    return {line[len(SHARED_PREFIX):].strip() for line in text.splitlines() if line.startswith(SHARED_PREFIX)}


def _merge_lists(base: str, remote: str, local: str) -> list[str]:
    """The shared folders after two devices changed the list while apart: what either added is
    kept, what either removed is gone."""
    was, theirs, mine = _listed(base), _listed(remote), _listed(local)
    return sorted((theirs | mine) - (was - theirs) - (was - mine))


def _save_copy(space: Space, path: str, text: str) -> str:
    root = _kb_root(space, path)
    inner = path[len(root) + 1:] if root else path
    p = Path(inner.replace("/", "__"))
    folder = space.path / root / CONFLICTS
    dest, n = folder / f"{p.stem}.{_host()}{p.suffix}", 2
    while dest.exists():
        dest, n = folder / f"{p.stem}.{_host()}-{n}{p.suffix}", n + 1
    _write_text(dest, text)
    return dest.relative_to(space.path).as_posix()


def conflict_files(space: Space) -> list[str]:
    """Conflict copies waiting to be reconciled, relative to the space."""
    out = []
    for root in ["", *shared(space)]:
        folder = space.path / root / CONFLICTS
        try:
            out.extend((folder / n).relative_to(space.path).as_posix() for n in sorted(os.listdir(folder)))
        except OSError:
            continue
    return out


# ------------------------------------------------------------------ merge rules

def _ids_line(lines: list[str]) -> int | None:
    """Index of the one line under "Active decisions" in STATE.md, when it has a shape kbhub writes."""
    from .recording import STATE_HEADING_RE, STATE_IDS_RE, STATE_NONE_RE, _headings
    heads = _headings(lines)
    at = next((n for n, (_, _, title) in enumerate(heads) if STATE_HEADING_RE.match(title)), None)
    if at is None:
        return None
    end = heads[at + 1][0] if at + 1 < len(heads) else len(lines)
    body = [i for i in range(heads[at][0] + 1, end) if lines[i].strip()]
    if len(body) != 1 or not (STATE_IDS_RE.match(lines[body[0]]) or STATE_NONE_RE.match(lines[body[0]])):
        return None
    return body[0]


def _merge_state(base: str, remote: str, local: str) -> str | None:
    """Merge two edits of STATE.md whose only clash is the "Active decisions" id line (both sides'
    `record` calls rewrite it). That line is rebuilt from decisions.md afterwards, so it is taken
    from the remote on all three sides and the rest merged as usual. None when a real conflict is left."""
    sides = [text.split("\n") for text in (base, remote, local)]
    at = [_ids_line(lines) for lines in sides]
    if any(i is None for i in at):
        return None
    for lines, i in zip(sides, at):
        lines[i] = sides[1][at[1]]
    with tempfile.TemporaryDirectory() as tmp:
        names = []
        for label, lines in zip(("base", "remote", "local"), sides):
            names.append(os.path.join(tmp, label))
            Path(names[-1]).write_text("\n".join(lines), encoding="utf-8", errors="surrogateescape")
        try:
            p = subprocess.run(["git", "merge-file", "-p", "--quiet", names[2], names[0], names[1]],
                               capture_output=True, encoding="utf-8", errors="surrogateescape")
        except OSError:
            return None
    return p.stdout if p.returncode == 0 else None


def repair(space: Space) -> list[str]:
    """Put right what a merge of two devices' decisions can leave behind; returns the files it
    changed (relative to the space). Decisions the remote already has keep their numbers."""
    changed = []
    for rel in shared(space):
        root = space.path / rel
        name = next((c for c in ("knowledge-base/decisions.md", "decisions.md") if (root / c).is_file()), None)
        if name is None:
            continue
        text = (root / name).read_text(encoding="utf-8")
        code, theirs, _ = _git(space, "show", f"{UPSTREAM}:{rel}/{name}")
        new = repair_decisions(text, theirs if code == 0 else "")
        if new != text:
            _write_text(root / name, new)
            changed.append(f"{rel}/{name}")
        state = root / "STATE.md"
        if state.is_file():
            old = state.read_text(encoding="utf-8")
            fresh = refresh_state(old, new)
            if fresh != old:
                _write_text(state, fresh)
                changed.append(f"{rel}/STATE.md")
    return changed


def _decision_heads(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, number, title) of each recorded decision heading."""
    from .recording import DECISION_RE, PLACEHOLDER, _headings
    out = []
    for i, _, title in _headings(lines):
        m = DECISION_RE.match(title)
        if m and m.group(2).lstrip(": ").strip() != PLACEHOLDER:
            out.append((i, int(m.group(1)), title))
    return out


def repair_decisions(text: str, remote_text: str) -> str:
    """`text` (decisions.md after a merge) with two faults mended:
    - a heading a union merge left twice because one device marked it superseded: the superseded
      form stays;
    - two different decisions with one number (both devices recorded while apart): the one the
      remote does not have yet takes the next free number."""
    lines = text.split("\n")
    heads = _decision_heads(lines)
    drop = set()
    for (i, n, a), (j, m, b) in zip(heads, heads[1:]):
        if n != m or any(lines[k].strip() for k in range(i + 1, j)):
            continue
        dead_a, dead_b = bool(SUPERSEDED_RE.search(a)), bool(SUPERSEDED_RE.search(b))
        if dead_a != dead_b:
            drop.add(j if dead_a else i)
        elif a == b:
            drop.add(j)
    if drop:
        lines = [line for k, line in enumerate(lines) if k not in drop]
        heads = _decision_heads(lines)

    theirs = {line.strip() for line in remote_text.split("\n")}
    by_number: dict[int, list[int]] = {}
    for i, n, _ in heads:
        by_number.setdefault(n, []).append(i)
    top = max(by_number, default=0)
    for n in sorted(by_number):
        group = by_number[n]
        if len(group) < 2:
            continue
        mine = [i for i in group if lines[i].strip() not in theirs]
        if len(mine) == len(group):
            mine = mine[1:]              # all local: the first keeps the number
        for i in mine:
            top += 1
            old, new = f"D-{n:03d}", f"D-{top:03d}"
            lines[i] = re.sub(rf"\bD-0*{n}\b", new, lines[i], count=1)
            for k, _, title in heads:    # a decision this one superseded points at its old number
                if lines[k].strip() not in theirs and lines[k].rstrip().endswith(f"see {old})"):
                    lines[k] = lines[k].rstrip()[:-len(f"see {old})")] + f"see {new})"
    return "\n".join(lines)


def refresh_state(state_text: str, decisions_text: str) -> str:
    """STATE.md with its "Active decisions" id line rebuilt from decisions.md. Left alone when the
    line is hand-written or there is nothing to list."""
    from .recording import STATE_IDS_RE, STATE_NONE_RE
    lines = state_text.split("\n")
    at = _ids_line(lines)
    active = [f"D-{n:03d}" for _, n, title in sorted(_decision_heads(decisions_text.split("\n")), key=lambda h: h[1])
              if not SUPERSEDED_RE.search(title)]
    if at is None or not active:
        return state_text
    m = STATE_IDS_RE.match(lines[at]) or STATE_NONE_RE.match(lines[at])
    tail = m.group(m.lastindex)
    lines[at] = ", ".join(dict.fromkeys(active)) + "." + tail
    return "\n".join(lines)


# ------------------------------------------------------------------ reporting

def sync_all(cfg: GlobalConfig, net_timeout: float = 20) -> list[dict]:
    return [sync_space(space, net_timeout) for space in cfg.spaces]


def describe(res: dict, quiet: bool = False) -> str:
    """One line about a sync. With `quiet`, nothing unless the reader should know or act."""
    parts = []
    if res["pulled"]:
        n = len(res["pulled"])
        parts.append(f"{n} file{'' if n == 1 else 's'} updated from another device")
    if res["pushed"] and not quiet:
        parts.append("pushed")
    if res["pending"] == "push":
        parts.append("push pending (it will be retried)")
    elif res["pending"] == "pull":
        parts.append("pull pending")
    if res["conflicts"]:
        parts.append("conflict copies waiting: " + ", ".join(res["conflicts"])
                     + " (compare each with the current file, merge what is missing, then delete the copy)")
    notes = [n for n in res["notes"] if not (quiet and n in ("another session is syncing",)
                                             or quiet and n.startswith("no remote yet"))]
    parts.extend(notes)
    if not parts:
        if quiet:
            return ""
        parts.append("up to date")
    return f"Shared space {res['space']}: " + "; ".join(parts) + "."


def status(cfg: GlobalConfig) -> list[dict]:
    """Each space as it stands on this device, without touching the network."""
    out = []
    for space in cfg.spaces:
        ready = git_dir(space).is_dir() and space.path.is_dir()
        info = {"name": space.name, "path": str(space.path), "remote": space.remote, "ready": ready,
                "shared": shared(space), "ahead": 0, "behind": 0, "changes": 0, "paused": [], "conflicts": []}
        if ready:
            if _has_ref(space, UPSTREAM):
                info["ahead"] = _count(space, f"{UPSTREAM}..HEAD")
                info["behind"] = _count(space, f"HEAD..{UPSTREAM}")
            elif _has_ref(space, "HEAD"):
                info["ahead"] = _count(space, "HEAD")
            info["changes"] = len(_z(_git(space, "status", "--porcelain", "-z")[1]))
            if _paused_file(space).exists():
                info["paused"] = [p for p in _paused_file(space).read_text(encoding="utf-8").split("\n") if p]
            info["conflicts"] = conflict_files(space)
        out.append(info)
    return out


def passthrough(space: Space, args: list[str]) -> int:
    """`kbhub share git -- ...`: plain git against the space, for looking around by hand."""
    cmd = ["git", "--git-dir", str(git_dir(space)), "--work-tree", str(space.path), *args]
    try:
        return subprocess.run(cmd, cwd=space.path).returncode
    except FileNotFoundError:
        raise ValueError("git is not installed") from None
