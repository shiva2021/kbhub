"""kbhub command line.

    kbhub index [--rebuild]
    kbhub search "what did we decide about channel ids?" [--project P] [--doc-type T] [--all]
    kbhub section PROJECT PATH [HEADING]
    kbhub sources [--project P]
    kbhub projects
    kbhub scan [ROOT]                       find knowledge-base folders that are not registered
    kbhub register PATH [--name N]          register a folder (works inside git repos)
    kbhub new-kb TOPIC [--in DIR] [--in-repo]   new knowledge base for a repo (kept outside it unless --in-repo)
    kbhub kb on|off|status [PATH]           keep a knowledge base for a code base, or not; ask no more
    kbhub rule add PROJECT GLOB --status superseded
    kbhub unregister NAME
    kbhub add-project PATH [--name N]       (folders outside git repos, with a .kb.toml)
    kbhub new-project NAME [--parent DIR]
    kbhub eval [PROJECT]
    kbhub cowork-skill [--out ZIP]          package the Cowork skill for upload to claude.ai
    kbhub setup | doctor | update | uninstall   install, check, upgrade, remove

section, sources and projects take --no-sync: read the index as it is, writing nothing (Cowork).

Searches cover the scope (work or personal) of the folder you run them from; use --scope all,
or --project, to reach the rest.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import ConfigError, GlobalConfig
from .embedder import EmbedderUnavailable


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kbhub", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index", help="sync the index with the markdown files")
    p.add_argument("--rebuild", action="store_true", help="re-index everything from scratch")
    p = sub.add_parser("search", help="search all knowledge bases")
    p.add_argument("query")
    p.add_argument("--project")
    p.add_argument("--doc-type")
    p.add_argument("--all", action="store_true", help="include superseded material")
    p.add_argument("-k", type=int, default=6, help="number of results (1-15, default 6)")
    p.add_argument("--no-sync", action="store_true", help="read-only: search the index as it is")
    p.add_argument("--keyword-only", action="store_true", help="read-only keyword search (no embedder needed)")
    p.add_argument("--scope", help="work, personal or all (default: the scope of the current folder)")
    p = sub.add_parser("section", help="print one full section, fresh from disk")
    p.add_argument("project")
    p.add_argument("path")
    p.add_argument("heading", nargs="?")
    p.add_argument("--no-sync", action="store_true", help="read-only: print the section as the index holds it")
    p = sub.add_parser("sources", help="list indexed files")
    p.add_argument("--project")
    p.add_argument("--scope")
    p.add_argument("--no-sync", action="store_true", help="read-only: list from the index as it is")
    p = sub.add_parser("projects", help="list knowledge-base projects")
    p.add_argument("--scope")
    p.add_argument("--no-sync", action="store_true", help="read-only: list the projects the index holds")
    p = sub.add_parser("setup", help="run the setup wizard (install what is missing, connect Claude Code)")
    p.add_argument("--yes", "-y", action="store_true", help="accept every default without asking")
    p.add_argument("--wire-only", action="store_true", help="only reconnect Claude Code; ask and install nothing")
    sub.add_parser("doctor", help="check the install and say how to fix what is wrong")
    sub.add_parser("update", help="get the newest version and reconnect it")
    sub.add_parser("uninstall", help="disconnect kbhub from Claude Code (your notes and settings are kept)")
    p = sub.add_parser("cowork-skill", help="package the Cowork skill as a zip to upload to claude.ai")
    p.add_argument("--out", help="zip file to write (default: <engine home>/dist/kbhub-cowork.zip)")
    p = sub.add_parser("scan", help="find knowledge-base folders on disk that are not registered yet")
    p.add_argument("root", nargs="?")
    p = sub.add_parser("register", help="register an existing knowledge-base folder (inside a git repo or not)")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--scope")
    p = sub.add_parser("new-kb", help="create a knowledge base; for a repo it goes to <knowledge-base folder>/<repo>/<topic>/")
    p.add_argument("topic")
    p.add_argument("--in", dest="location", help="folder to create it for (default: the current repo, else the configured parent)")
    p.add_argument("--name")
    p.add_argument("--scope")
    p.add_argument("--in-repo", action="store_true", help="write it into the repo, at <repo>/knowledge-base/<topic>/")
    p = sub.add_parser("kb", help="say whether a code base gets a knowledge base (sessions then stop asking)")
    p.add_argument("choice", choices=("on", "off", "status"))
    p.add_argument("path", nargs="?", help="a folder of the code base (default: the current folder)")
    p.add_argument("--scope", help="work or personal, for a knowledge base that `on` creates")
    p = sub.add_parser("rule", help="status / doc-type rules of a registered knowledge base")
    rule = p.add_subparsers(dest="rule_cmd", required=True)
    p = rule.add_parser("add")
    p.add_argument("project")
    p.add_argument("glob")
    p.add_argument("--status")
    p.add_argument("--doc-type")
    p.add_argument("--note")
    p = sub.add_parser("unregister", help="remove a knowledge base from the registry (files are kept)")
    p.add_argument("name")
    p = sub.add_parser("add-project", help="register an existing folder as a knowledge base")
    p.add_argument("path")
    p.add_argument("--name")
    p = sub.add_parser("new-project", help="create a new knowledge base with the standard layout")
    p.add_argument("name")
    p.add_argument("--parent")
    p = sub.add_parser("eval", help="run the [[eval]] retrieval checks from .kb.toml")
    p.add_argument("project", nargs="?")
    return ap


def _run(a: argparse.Namespace) -> int:
    if a.cmd in ("add-project", "new-project"):
        from .registry import add_project, new_project
        cfg = GlobalConfig.load()
        project = (add_project(cfg, a.path, a.name) if a.cmd == "add-project"
                   else new_project(cfg, a.name, a.parent))
        print(project.path)
        return 0

    if a.cmd in ("scan", "register", "new-kb", "kb", "rule", "unregister"):
        return _manage(a)

    if a.cmd == "cowork-skill":
        from .cowork import package
        print(package(a.out))
        return 0

    from .format import format_hits
    from .index import KBIndex

    here = Path(os.getcwd())
    if a.cmd == "search":
        read_only = a.no_sync or a.keyword_only
        idx = KBIndex(read_only=read_only, keyword_only=a.keyword_only, session_dir=here)
        if not read_only:
            idx.sync()
        print(format_hits(idx.search(a.query, a.project, a.doc_type, a.all, a.k, a.scope)))
        return 0

    if a.cmd in ("section", "sources", "projects") and a.no_sync:
        return _read_only(a, KBIndex(read_only=True, keyword_only=True, session_dir=here))

    idx = KBIndex(session_dir=here)
    if a.cmd == "index":
        print(json.dumps(idx.sync(rebuild=a.rebuild)))
    elif a.cmd == "section":
        print(idx.section(a.project, a.path, a.heading))
    elif a.cmd == "sources":
        idx.sync()
        for r in idx.sources(a.project, a.scope):
            print(_source_line(r))
    elif a.cmd == "projects":
        stats = idx.sync()
        for p in idx.projects(a.scope):
            flag = "  MISSING" if p["missing"] else ""
            repo = f"  (repo: {p['repo']})" if p["repo"] else ""
            print(f"{p['name']}  [{p['scope']}]  {p['path']}  {p['files']} files  ~{p['tokens']} tokens{repo}{flag}")
            if p["warning"]:
                print(f"    warning: {p['warning']}")
        for w in stats["warnings"]:
            print(f"warning: {w}", file=sys.stderr)
    elif a.cmd == "eval":
        return _eval(idx, a.project, format_hits)
    return 0


def _source_line(r: dict) -> str:
    return (f"{r['tokens']:>7} tok  {r['chunks']:>3} ch  {r['status']:<22} {r['doc_type']:<12} "
            f"{r['project']} · {r['path']}")


def _read_only(a: argparse.Namespace, idx) -> int:
    """section / sources / projects straight from the index: no sync, no writes, and no need for the
    knowledge-base folders to exist where this runs (Cowork)."""
    if a.cmd == "section":
        print(idx.indexed_section(a.project, a.path, a.heading))
    elif a.cmd == "sources":
        for r in idx.sources(a.project, a.scope):
            print(_source_line(r))
    else:
        for p in idx.indexed_projects(a.scope):
            print(f"{p['name']}  [{p['scope']}]  {p['files']} files  ~{p['tokens']} tokens  {p['path']}")
    return 0


def _manage(a: argparse.Namespace) -> int:
    from . import kbregistry, manage
    cfg = GlobalConfig.load()
    if a.cmd == "scan":
        found = manage.scan(cfg, a.root)
        for c in found:
            state = f"ok   {c['registered']}" if c["registered"] else f"NEW  {c['name']}"
            print(f"{state}  [{c['scope']}]  {c['path']}  {c['files']} files")
        if not found:
            print("No knowledge-base folders found.")
        elif any(not c["registered"] for c in found):
            print("\nRegister a NEW folder with: kbhub register <path> [--name N] [--scope work|personal]")
    elif a.cmd == "register":
        print(manage.register_kb(cfg, a.path, a.name, a.scope).name)
    elif a.cmd == "new-kb":
        location = a.location
        if location is None and kbregistry.git_root(Path(os.getcwd())) is not None:
            location = os.getcwd()
        print(manage.create_kb(cfg, a.topic, location, a.name, a.scope, a.in_repo).path)
    elif a.cmd == "kb":
        where = Path(os.path.abspath(os.path.expanduser(a.path or os.getcwd())))
        if a.choice == "status":
            from . import prefs
            from .registry import discover_projects, repo_projects
            root = kbregistry.git_root(where)
            if root is None:
                raise ValueError(f"{where} is not inside a git repository")
            repo = kbregistry.main_clone(root)
            print(f"{repo}: {prefs.get_choice(repo) or 'not asked yet'}")
            for project in sorted(repo_projects(discover_projects(cfg, session_dir=where)[0], where, cfg),
                                  key=lambda p: p.name.lower()):
                print(f"  {project.name}  [{project.scope}]  {project.path}")
        else:
            done = manage.setup_project_kb(cfg, where, a.choice == "on", a.scope)
            print(f"{done['repo']}: {a.choice}")
            for project in done["projects"]:
                state = "created" if done["created"] else "registered" if done["registered"] else "found"
                print(f"  {state}  {project.name}  [{project.scope}]  {project.path}")
            for skipped in done["skipped"]:
                print(f"  not registered: {skipped}")
            if done["created"]:
                print("Run `kbhub index` to make it searchable now (sessions index it by themselves).")
    elif a.cmd == "rule":
        manage.add_rule(cfg, a.project, a.glob, a.status, a.doc_type, a.note)
        print(f"rule added to {a.project}")
    elif a.cmd == "unregister":
        if not manage.unregister(cfg, a.name):
            raise ValueError(f"{a.name!r} is not in the registry")
        print(f"unregistered {a.name} (files untouched)")
    return 0


def _eval(idx, only: str | None, format_hits) -> int:
    from .chunker import approx_tokens
    idx.sync()
    projects = idx.projects_map()
    names = [idx._resolve_project(only).name] if only else sorted(projects)
    print(f"embedder: {idx.embedder.id}")
    n = hit1 = hit3 = hit6 = 0
    for name in names:
        for question, expect in projects[name].config.eval_cases:
            res = idx.search(question, project=name, limit=6)
            rank = next((i for i, h in enumerate(res["hits"], 1) if expect in h["path"]), None)
            n, hit1, hit3, hit6 = n + 1, hit1 + (rank == 1), hit3 + bool(rank and rank <= 3), hit6 + bool(rank)
            print(f"{'rank ' + str(rank) if rank else 'MISS  '} ~{approx_tokens(format_hits(res)):>5} tok  {question}")
    if not n:
        print("no [[eval]] cases found in .kb.toml")
    print(f"hit@1 {hit1}/{n}  hit@3 {hit3}/{n}  hit@6 {hit6}/{n}")
    return 0


def _admin(a) -> int:
    """setup, doctor, update, uninstall: these must work even when the config or index is broken."""
    from . import doctor, install
    from . import setup as wizard
    from .system import Env
    if a.cmd == "setup":
        return wizard.main((["--yes"] if a.yes else []) + (["--wire-only"] if a.wire_only else []))
    env = Env.real()
    if a.cmd == "doctor":
        lines, ok = doctor.report(doctor.checks(env))
        print("\n".join(lines))
        return 0 if ok else 1
    if a.cmd == "uninstall":
        print("\n".join(install.uninstall(env.claude_home, env.kb_home, bin_dir=env.user_bin, run=env.run,
                                          claude=env.find("claude") or "claude")))
        return 0
    uv, repo = env.find("uv") or "uv", str(install.REPO)
    for label, cmd in (("Getting the newest version", [env.find("git") or "git", "-C", repo, "pull", "--ff-only"]),
                       ("Updating its libraries", [uv, "sync", "--inexact", "--project", repo]),
                       ("Reconnecting", [uv, "run", "--project", repo, "python", "-m", "kbhub.setup", "--wire-only"])):
        print(f"{label}…", flush=True)
        code, out = env.run(cmd)
        if code != 0:
            print(f"Error: {label.lower()} failed:\n{out}", file=sys.stderr)
            return 1
        if out:
            print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    try:
        if a.cmd in ("setup", "doctor", "update", "uninstall"):
            return _admin(a)
        return _run(a)
    except (ValueError, ConfigError, RuntimeError, EmbedderUnavailable, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
