# kbhub reference

Everything the [README](../README.md) leaves out: every command, every setting, and how the parts fit.

## Where things are

| Path | What |
| --- | --- |
| `~/.kbhub/` | The program (this repository). |
| `~/.kbhub/config.toml` | Your settings. Written by `kbhub setup`; all keys are optional. |
| `~/.kbhub/registry.json` | The list of knowledge bases that live inside code projects. |
| `~/.kbhub/db/` | The search index. Only a copy of your notes: delete it any time and it rebuilds. |
| `~/.claude/skills/kbhub` | Link to the skill that teaches Claude how to keep a knowledge base. |
| `~/.claude/settings.json` | Gets one SessionStart hook entry. |
| `~/.claude/CLAUDE.md` | Gets one short block between `<!-- kbhub:start -->` and `<!-- kbhub:end -->`. |
| `~/.local/bin/kbhub` | The `kbhub` command. |

## Two kinds of knowledge base

**Inside a code project**: `<project>/knowledge-base/<topic>/`, one folder per topic, saved with
the code. Files: `README.md` (entry point), `STATE.md` (where things stand), `decisions.md`,
`lessons.md`, `open-items.md`, and the folders `runbooks/`, `research/`, `plans/`, `handovers/`.
It is recorded in `registry.json`; no marker file is written into the project. Named
`<project>/<topic>`, for example `shop/billing`. kbhub only writes the files: it never stages,
commits or pushes.

**Standalone**: any folder outside a git repository that contains a `.kb.toml` file, found
automatically under the `scan_roots`. Same files, under `knowledge-base/`, plus a `CLAUDE.md`.
Optional settings in `.kb.toml`: `name`, `scope`, `[[rules]]` and `[[eval]]`.

Inside a git repository, only registered knowledge-base folders are indexed, never the rest of
the project. With git worktrees, a session reads the copy in its own checkout.

## Scope: work and personal

Every knowledge base is `work` or `personal`. It is personal when it sits under one of the
`personal_roots`, or says `scope = "personal"`. A search covers the scope of the folder it runs
from. Name a project, or pass `--scope all` (tools: `scope="all"`), to reach the rest. Scope is a
default filter, not a lock.

## Marking notes as outdated

- One section: put the word `superseded` (or `historical`, `obsolete`, `deprecated`, `dropped`,
  `not needed`) in its heading.
- Whole files or folders: a rule. `kbhub rule add <name> "old/**" --status superseded --note "why"`
  for knowledge bases in code projects; a `[[rules]]` table in `.kb.toml` for standalone ones.
- `--status background` keeps a file searchable but ranked as background reading.

Superseded material is hidden from search unless you ask for it (`--all`).

## Commands

| Command | What it does |
| --- | --- |
| `kbhub setup [--yes]` | The setup wizard. Safe to run again. |
| `kbhub doctor` | Checks the install; exit code 1 if something required is missing. |
| `kbhub update` | Pulls the newest version, updates libraries, reconnects. |
| `kbhub uninstall` | Disconnects from Claude Code; keeps settings, registry and index. |
| `kbhub search "question" [--project P] [--scope S] [--doc-type T] [--all] [-k N]` | Search. |
| `kbhub section PROJECT PATH [HEADING]` | Print one full section. |
| `kbhub projects` | List knowledge bases. |
| `kbhub sources [--project P]` | List indexed files with type and status. |
| `kbhub new-kb TOPIC [--in DIR]` | New knowledge base; inside a project it becomes `knowledge-base/TOPIC/`. |
| `kbhub new-project NAME [--parent DIR]` | New standalone knowledge base. |
| `kbhub scan [ROOT]` | Find knowledge-base folders that are not indexed yet. Changes nothing. |
| `kbhub register PATH [--name N] [--scope S]` | Add an existing folder to the index. |
| `kbhub unregister NAME` | Remove one from the index. Files are untouched. |
| `kbhub add-project PATH [--name N]` | Add a standalone folder outside the scan roots. |
| `kbhub rule add PROJECT GLOB --status S` | Mark files superseded or background. |
| `kbhub index [--rebuild]` | Refresh the index now (it also refreshes by itself). |
| `kbhub eval [PROJECT]` | Run the retrieval checks defined in a `.kb.toml`. |
| `kbhub cowork-skill` | Build the zip of the Cowork skill. |

`search --no-sync --keyword-only`, and `section`, `projects`, `sources` with `--no-sync`, read the
index without writing anything. That is what the Cowork skill uses.

## Tools Claude gets

`search_kb`, `get_section`, `list_projects`, `list_sources`, `reindex`, `scan_kbs`, `register_kb`,
`create_kb`, `add_rule`. They are served by a local MCP server (`kbhub-server`) that Claude Code
starts with each session.

## Settings (`config.toml`)

Every key with its default is documented in [`config.example.toml`](../config.example.toml).
The ones that matter most:

| Key | Meaning |
| --- | --- |
| `kb_scan_roots` | Folders `kbhub scan` searches for knowledge-base folders inside projects. |
| `scan_roots` | Folders searched for standalone knowledge bases (`.kb.toml`), up to `scan_depth` (3) levels down. |
| `new_project_parent` | Where new standalone knowledge bases are created. |
| `personal_roots` | Folders whose knowledge bases are personal. |
| `[embedding] backend` | `"ollama"` (best search) or `"wordllama"` (built-in, offline). |

Changing chunk sizes or the `[embedding]` table rebuilds the index on the next search.

## Search quality

With Ollama running, search combines meaning ("embeddings" from the `nomic-embed-text` model) with
word matching. If Ollama is not answering, searches still work but match words only, and the
missing embeddings are filled in the next time it is up. `kbhub doctor` shows which mode you are in.

## Updating and sharing

kbhub is installed as a git clone in `~/.kbhub`; `kbhub update` runs `git pull`. To share it with
someone, give their GitHub account access to the repository and send them the two install lines
from the README.
