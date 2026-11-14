---
name: kbhub
description: Use for any project, coding or research task. Finds project knowledge with the kbhub search tools before reading files, creates or registers knowledge bases (inside a code base or standalone), and records decisions, lessons, STATE.md and handovers.
---

# Knowledge bases (kbhub)

Project knowledge (decisions, lessons, plans, runbooks, research, handovers) lives as markdown in
knowledge bases and is indexed centrally by `kbhub`, wherever the files are:

- **for a code base, outside it** (the default): `<knowledge-base folder>/<repo>/<topic>/`, one topic
  per functionality (e.g. `~/knowledge-bases/core/billing/` is project `core/billing`). Nothing is
  written into the repo;
- **inside a code base**, only when the user asks: `<repo>/knowledge-base/<topic>/`, committed with
  the code;
- **standalone**: a folder outside any repo, marked with a `.kb.toml` (e.g. `~/notes/atlas`);
- **personal projects** (folders listed as `personal_roots` in the config), kept apart from work knowledge by scope.

Markdown is the source of truth; the index is derived and re-syncs by itself.

## When to use

- Any coding or research task, in any repo: the reasons behind the code are in a knowledge base.
- Any question starting "what did we decide", "why do we", "how do I run", "where are we up to".
- Starting a new project or a new area of a code base, finishing a session, making or reversing a
  decision, handing work over.

## Finding knowledge

1. Call `search_kb` **before** opening knowledge files. Ask a specific question and include exact
   identifiers when you have them (ticket numbers like `PE-472`, function names, id shapes like
   `<clientId>-<swId>`). Filter with `project` (names come from the session-start context or
   `list_projects`; a repo name such as `core` covers every `core/<topic>`) and `doc_type`
   (`decisions`, `lessons`, `plan`, `runbook`, ...).
2. Each hit shows `project · path:lines | doc type | status`. If a hit is relevant but cut short,
   call `get_section` with its project, path and its `§` heading.
3. Read a knowledge base's entry point (`README.md` in a repo, `CLAUDE.md` standalone) and `STATE.md`
   in full. Read nothing else in full by default.
4. **Never act on superseded results.** They are hidden unless you pass `include_superseded=true`;
   use that only to understand history. `background` results are context, not decisions.
5. `list_sources` shows which files exist for a project.

## Personal and work scope

Every knowledge base has a scope: `personal` (under a personal folder chosen at setup) or `work` (everything
else). By default a search covers only the scope of the folder the session works in, so personal
notes stay out of work results and the reverse. Scope is a default filter, not a lock: naming a
`project`, or passing `scope="all"` (`--scope all` on the command line), reaches across.

## A code base with no knowledge base: ask once

The session start tells you which case you are in. Follow it exactly:

- **"ACTION NEEDED … has not been asked yet"**: before anything else in your first reply, ask the
  user once whether kbhub should keep a knowledge base for this code base (Yes / No; use the
  AskUserQuestion tool if you have it). Then call `kb_setup(enable=true)` or
  `kb_setup(enable=false)` and carry on with their request. Do not skip it because the task looks
  small. If you cannot ask (a non-interactive run) or they do not answer, do not call `kb_setup`.
- **Yes** (`kb_setup(enable=true)`): it uses the knowledge base the code base already has; else it
  registers knowledge-base folders found in the repo; else it creates
  `<knowledge-base folder>/<repo>/general/` outside the repo and indexes it. Read the new
  `CLAUDE.md` and `STATE.md`, then record what the session learns.
- **No** (`kb_setup(enable=false)`): the answer is remembered. Do not ask again and do not offer a
  knowledge base in that code base unless the user brings it up.
- The answer is stored per code base in `~/.kbhub-config/projects.json`, outside every repo. The
  user can change it with `kbhub kb on` / `kbhub kb off` (`kbhub kb status` shows it).

## Knowledge bases for a code base

- A different functionality gets its own topic: call `create_kb(topic)` (command line:
  `kbhub new-kb <topic>` from inside the repo), e.g. `create_kb("payroll")` next to `billing`. It
  creates `<knowledge-base folder>/<repo>/<topic>/` outside the repo, names it `<repo>/<topic>`
  and indexes it at once. Do not pile unrelated areas into one knowledge base.
- **Nothing is written into the code base** unless the user asks for the knowledge to live there.
  Only then pass `in_repo=true` (`kbhub new-kb <topic> --in-repo`): it creates
  `<repo>/knowledge-base/<topic>/` with `README.md`, `STATE.md`, `decisions.md`, `lessons.md`,
  `open-items.md`, `runbooks/`, `research/`, `plans/`, `handovers/`.
- The tools work for the folder **where the session started**. If you have since moved to
  another repo or into a git worktree, pass `location=<that folder>`.
- For an in-repo knowledge base the tool **never commits**: it only writes files. Commit the
  knowledge files with the code change they describe. If you are on `main`/`master`, branch first;
  follow the repo's normal rules.
- Knowledge written on a feature branch or in a git worktree is searchable straight away: a session
  always reads and writes the copy in its own checkout.
- **Never create a `.kb.toml` inside a git repo.** In-repo knowledge bases are recorded in the central
  registry (`~/.kbhub/registry.json`), not by marker files.
- Only registered knowledge-base folders are indexed, never a repo's READMEs or other docs. A repo
  root cannot be registered.

## Finding and registering existing knowledge bases

If the session start says a folder "exists but is not registered", or the user asks you to pick up
existing notes:

1. `scan_kbs()` (command line: `kbhub scan`) lists knowledge-base folders on disk, including
   inside repos; `NEW` ones are not indexed yet. It changes nothing.
2. Show the user the `NEW` ones and, for each they confirm, call `register_kb(path)` (`kbhub
   register <path>`). The suggested name and scope are shown; pass `name` / `scope` to change them.
3. `kbhub unregister <name>` removes one from the index; the files are untouched.

## Starting a standalone project

For work that is not tied to one code base:

- Run `kbhub new-project <name>` (from `~/.kbhub`: `uv run kbhub new-project <name>`), or
  call `create_kb(name, location=<a folder outside any repo>)`. The default parent is `new_project_parent` in the config.
- It creates `.kb.toml`, `CLAUDE.md`, `STATE.md`, `knowledge-base/` (`decisions.md`, `lessons.md`,
  `open-items.md`, `runbooks/`, `research/`), `plans/` and `handovers/`, and it is indexed at once.
- To register an existing folder of notes outside a repo: `kbhub add-project <path> [--name N]`.

## Recording decisions

Add to the knowledge base's `decisions.md`, newest last, numbered `D-NNN`:

```markdown
## D-012: Site chats stay on client channels
- **Date:** 2026-11-11 · **Status:** accepted · **Decided by:** Sam
- **Decision:** what was decided, in one or two sentences.
- **Why:** the reason, and the options that were rejected.
```

To reverse a decision, never delete or rewrite it. Mark the old heading
`(superseded YYYY-MM-DD, see D-NNN)` and add the new decision below with a link back. The marker in
the heading is what hides the old decision from search.

Record gotchas and failed approaches in `lessons.md`, and unanswered questions in `open-items.md`.

## STATE.md

`STATE.md` is the one file every session reads in full, so keep it short (about 1.5k tokens at most)
and current. Sections: **Status**, **Active decisions** (ids only, linking to `decisions.md`),
**Blockers**, **Next steps**. Update it at the end of every session; move detail into the knowledge
base instead of letting it grow.

## Handovers

When work stops mid-stream or changes hands, write `handovers/YYYY-MM-DD-<topic>.md`: what was done,
what is in flight, how to verify it, and the exact next step. Link it from `STATE.md`.

## Marking material superseded

- One section: put `superseded` (or `historical`, `dropped`, `not needed`, `obsolete`,
  `deprecated`) in its heading. Everything nested under that heading is hidden from search too.
- Whole files or folders of an in-repo (registered) knowledge base: `add_rule(project, glob,
  status="superseded", note="Replaced by D-012")` (`kbhub rule add <project> <glob> --status
  superseded`). Use `status="background"` for explainers that are context only.
- Whole files or folders of a standalone project: add a rule to its `.kb.toml`:

  ```toml
  [[rules]]
  glob = "knowledge-base/research/old-approach/**"
  status = "superseded"
  note = "Replaced by D-012"
  ```

## Using it from Cowork

Cowork has no MCP server. It has its own skill, `kbhub-cowork` (uploaded to the claude.ai
account; `kbhub cowork-skill` builds the zip), which searches read-only through the computer
link. In short:

1. Request folder access to `~/.kbhub`.
2. Search with keywords only, never syncing:

   ```bash
   KBHUB_HOME="$HOME/mnt/.kbhub" UV_PROJECT_ENVIRONMENT="$HOME/.kbhub-venv" PYTHONDONTWRITEBYTECODE=1 uv run --frozen --directory "$HOME/mnt/.kbhub" kbhub search --no-sync --keyword-only "<q>"
   ```

   `KBHUB_HOME` tells the engine where the mounted index is; without it the search reports
   "No index found". The virtual environment lives in the Cowork VM's home, never in the user's
   folders. Add `--project <name>` to narrow the search. Cowork searches cover work knowledge
   only; add `--scope personal` or `--scope all` when you deliberately want personal notes.
3. Read full sections from the index with `kbhub section --no-sync <project> <path> [heading]`;
   `projects --no-sync` and `sources --no-sync` list what is indexed.
4. Never sync, rebuild or write the index from Cowork. Writing knowledge as markdown into a
   knowledge-base folder is fine; the Mac picks it up on its next sync.
