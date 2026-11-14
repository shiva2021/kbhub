# kbhub reference

The [README](../README.md) gets you installed and working. This page is for when you want to know
more: where kbhub keeps things, what each command does, and which settings you can change.

You do not need to read it top to bottom. Jump to the part you need:

- [The short version](#the-short-version)
- [Where things live on your Mac](#where-things-live-on-your-mac)
- [Asked once per project](#asked-once-per-project)
- [Where the notes live](#where-the-notes-live)
- [Work and personal notes](#work-and-personal-notes)
- [Retiring old notes](#retiring-old-notes)
- [Commands](#commands)
- [What Claude can do](#what-claude-can-do)
- [Settings](#settings)
- [Search quality](#search-quality)
- [Updating and sharing](#updating-and-sharing)

## The short version

- A **knowledge base** is a folder of Markdown notes about one topic.
- kbhub keeps a **search index** of all your knowledge bases in `~/.kbhub/db`. The index is only a
  copy. Your notes are the real thing, and kbhub never edits them unless you ask it to.
- Claude Code searches that index by itself. You can search it too, with the `kbhub` command.

## Where things live on your Mac

kbhub keeps everything of its own in one folder, `~/.kbhub`, and adds a few small things to Claude
Code so that Claude knows it is there.

**kbhub's own folder**

| Path | What it is |
| --- | --- |
| `~/.kbhub/` | The program itself (a copy of this repository). |
| `~/.kbhub/config.toml` | Your settings. Setup writes this for you, and every setting is optional. |
| `~/.kbhub/registry.json` | The list of knowledge bases that live inside your code projects. |
| `~/.kbhub/db/` | The search index. You can delete it at any time; kbhub builds it again from your notes. |
| `~/.kbhub-config/projects.json` | Your answer, per code project, to "keep a knowledge base for this project?", and which knowledge bases belong to it. |

**What it adds elsewhere**

| Path | What it is |
| --- | --- |
| `~/.claude/skills/kbhub` | A link to the skill that teaches Claude how to keep a knowledge base tidy. |
| `~/.claude/settings.json` | One entry, so each Claude session is told at start-up which knowledge bases exist, and whether to ask you about one. |
| `~/.claude/CLAUDE.md` | One short block of text, between `<!-- kbhub:start -->` and `<!-- kbhub:end -->`. |
| `~/.local/bin/kbhub` | The `kbhub` command you type in Terminal. |

`kbhub uninstall` removes everything in the second table and leaves the first one alone.

## Asked once per project

The first time a Claude session starts in a code project (a git repository) that has no knowledge
base, Claude asks whether kbhub should keep one. Your answer is saved in
`~/.kbhub-config/projects.json`, so you are asked once per project, not once per session. Git
worktrees share the answer of the project they belong to.

- **Yes.** kbhub uses the knowledge base the project already has. If it has none, it adds any
  knowledge-base folders it finds inside the project to the index. If there are none of those
  either, it creates `<project>/general` in your knowledge-base folder (`new_project_parent` in
  [Settings](#settings)) and indexes it.
- **No.** Sessions in that project stop asking and stop offering.

Change your answer with `kbhub kb on` or `kbhub kb off`, run inside the project. `kbhub kb status`
shows the answer and the project's knowledge bases.

A project counts as having a knowledge base when one is registered inside it, when one was created
for it, when one is named `<project>` or `<project>/…`, or when one sits under
`<knowledge-base folder>/<project>/`. Two projects with the same folder name share the last two
rules. A project folder that starts with a dot loses it in the name (`.tools` becomes `tools`).

You are never asked in a folder that is not a git repository.

## Where the notes live

Notes about a code project are kept outside it unless you ask otherwise. Notes that belong to no
project sit in a folder of their own. All of them work the same way in search; the difference is
where the files live and how kbhub finds them.

### For a code project, outside it (the default)

Use this when the notes are about the code: why billing rounds the way it does, how to run a
release, what was tried and dropped.

- The notes go in `<knowledge-base folder>/<project>/<topic>/`, one folder per topic. For
  example `~/knowledge-bases/shop/billing/`.
- The name is `<project>/<topic>`, for example `shop/billing`.
- Nothing is written into the project, so there is nothing to commit and nothing for code review
  to see.
- It has the standalone layout described [below](#standalone).

### Inside a code project

Use this when you want the notes saved with the code. Ask Claude for a knowledge base "inside this
repo", or run `kbhub new-kb <topic> --in-repo`.

- The notes go in `<project>/knowledge-base/<topic>/`, one folder per topic.
- They are saved with the code, so they travel with it and show up in code review.
- The name is `<project>/<topic>`, for example `shop/billing`.
- kbhub remembers the folder in `~/.kbhub/registry.json`. It writes no marker file into your project.
- kbhub only creates the note files. It never stages, commits or pushes anything; that stays
  your job.

A new knowledge base starts with these files:

| File or folder | What goes in it |
| --- | --- |
| `README.md` | The front door: what this knowledge base covers. |
| `STATE.md` | Where things stand right now, and what is next. |
| `decisions.md` | What was decided, and why. |
| `lessons.md` | Things learnt the hard way. |
| `open-items.md` | Questions and loose ends. |
| `runbooks/` | Step-by-step guides. |
| `research/` | Findings and background reading. |
| `plans/` | Plans for work to come. |
| `handovers/` | Notes written at the end of a session for whoever picks it up. |

Two things worth knowing:

- Inside a git repository, kbhub indexes **only** the knowledge-base folders you have registered.
  The rest of the project is never read into the index.
- If you use git worktrees, a session reads the notes in its own checkout, not another one's.

### Standalone

Use this for work that belongs to no code project: research, planning, a personal project.

- It is any folder **outside** a git repository that contains a small file called `.kb.toml`.
- kbhub finds these folders by itself, by looking under the folders named in `scan_roots`
  (see [Settings](#settings)).
- The notes sit in a `knowledge-base/` folder inside it, with the same files as above, plus a
  `CLAUDE.md` at the top.
- The `.kb.toml` file can be empty. If you want, it can hold a `name`, a `scope`, `[[rules]]`
  for retiring old notes, and `[[eval]]` checks.

Never put a `.kb.toml` inside a git repository. Notes in a code project are registered instead.

## Work and personal notes

Every knowledge base is either `work` or `personal`. This keeps a search in a work project from
turning up your personal notes, and the other way round.

- A knowledge base is **personal** when it sits under one of your `personal_roots` folders, or
  when it says `scope = "personal"`.
- Everything else is **work**.
- A search covers the scope of the folder you run it from.

To reach across, name the project or ask for everything:

```
kbhub search "tax return" --project household
kbhub search "tax return" --scope all
```

Claude can do the same (`scope="all"`). Scope is a sensible default, not a lock: it stops
accidents, it does not hide anything from you.

## Retiring old notes

Decisions change. Rather than deleting the old note and losing the history, mark it as outdated.
Outdated notes stay on disk but stop appearing in search, so nobody acts on them by mistake.

**One section.** Put the word `superseded` in its heading:

```
## Pricing model (superseded by the March decision)
```

`historical`, `obsolete`, `deprecated`, `dropped` and `not needed` work the same way.

**Whole files or folders.** Add a rule:

```
kbhub rule add shop/billing "old/**" --status superseded --note "replaced by the 2026 design"
```

For a standalone knowledge base, write the same thing as a `[[rules]]` table in its `.kb.toml`.

**Background reading.** `--status background` is a softer option. The file stays searchable, and
results from it are labelled as background so they are not mistaken for current notes.

To see outdated notes again, for example to check the history, add `--all` to a search.

## Commands

You will mostly talk to Claude, and Claude runs these for you. They are here for when you want to
do something yourself. Every command starts with `kbhub`.

### Finding things

| Command | What it does |
| --- | --- |
| `kbhub search "question"` | Search your notes. Returns the best few sections, with file and line numbers. |
| `kbhub section PROJECT PATH [HEADING]` | Print one section in full. |
| `kbhub projects` | List your knowledge bases. |
| `kbhub sources [--project P]` | List the indexed files, with their type and whether they are current. |

Options for `search`:

| Option | What it does |
| --- | --- |
| `--project P` | Search one project only. |
| `--scope S` | `work`, `personal` or `all`. |
| `--doc-type T` | Only one kind of note, such as `decisions`, `runbook` or `handover`. |
| `--all` | Include outdated notes. |
| `-k N` | How many results to return. |

### Creating and adding knowledge bases

| Command | What it does |
| --- | --- |
| `kbhub new-kb TOPIC [--in DIR]` | Start a knowledge base. For a project it becomes `<knowledge-base folder>/<project>/TOPIC/`, outside the project. |
| `kbhub new-kb TOPIC --in-repo` | Start it inside the project instead, as `knowledge-base/TOPIC/`. |
| `kbhub kb on\|off\|status [PATH]` | Say whether a project gets a knowledge base, so sessions stop asking. `on` finds or creates one. |
| `kbhub new-project NAME [--parent DIR]` | Start a standalone knowledge base. |
| `kbhub scan [ROOT]` | Look for knowledge-base folders that are not indexed yet. It only reports; it changes nothing. |
| `kbhub register PATH [--name N] [--scope S]` | Add an existing folder of notes to the index. |
| `kbhub unregister NAME` | Take one out of the index. Your files are not touched. |
| `kbhub add-project PATH [--name N]` | Add a standalone folder that sits outside your scan folders. |

### Keeping the index in order

| Command | What it does |
| --- | --- |
| `kbhub rule add PROJECT GLOB --status S` | Mark files as superseded or background. |
| `kbhub index [--rebuild]` | Refresh the index now. You rarely need this, because it refreshes by itself. `--rebuild` starts from scratch. |
| `kbhub eval [PROJECT]` | Run the search checks written in a `.kb.toml`, to see whether search still finds what it should. |

### Looking after the install

| Command | What it does |
| --- | --- |
| `kbhub setup [--yes]` | Run the setup wizard. Safe to run again at any time. `--yes` accepts every suggestion without asking. |
| `kbhub doctor` | Check that every part is in place, and say how to fix what is not. |
| `kbhub update` | Get the newest version and reconnect it to Claude Code. |
| `kbhub uninstall` | Disconnect kbhub from Claude Code. Your settings, registry, index and notes all stay. |
| `kbhub cowork-skill` | Build the zip file of the Cowork skill. |

### Read-only mode

Normally a command checks first whether any notes have changed and refreshes the index. Add
`--no-sync` to `search`, `section`, `projects` or `sources` to skip that and only read. Add
`--keyword-only` to `search` to match words without the meaning model. Together they touch nothing
on disk, which is how the Cowork skill uses kbhub.

For scripts: `kbhub doctor` exits with code 1 when something required is missing.

## What Claude can do

Claude Code gets its own set of tools, which do the same jobs as the commands above. They come
from a small local program (`kbhub-server`, an MCP server) that Claude Code starts with each
session. You never run it yourself.

| Tool | What Claude uses it for |
| --- | --- |
| `search_kb` | Search your notes. |
| `get_section` | Read one section in full. |
| `list_projects` | See which knowledge bases exist. |
| `list_sources` | See which files are indexed. |
| `kb_setup` | Save your answer to "keep a knowledge base for this project?" and, on yes, find or create one. |
| `create_kb` | Start a new knowledge base for another topic. |
| `scan_kbs` | Look for folders of notes that are not indexed yet. |
| `register_kb` | Add one of those folders to the index. |
| `add_rule` | Mark files as superseded or background. |
| `reindex` | Refresh the index. |

There is no "save" tool on purpose. Claude writes notes as ordinary files, and the index picks
them up by itself.

## Settings

Your settings are in `~/.kbhub/config.toml`. Setup writes the file for you, and you can edit it in
any text editor. Every setting is optional.

These are the ones people actually change:

| Setting | What it means |
| --- | --- |
| `kb_scan_roots` | The folders `kbhub scan` looks through for knowledge-base folders inside projects. |
| `scan_roots` | The folders kbhub looks through for standalone knowledge bases. It goes three levels down (`scan_depth`). |
| `new_project_parent` | Your knowledge-base folder: where new knowledge bases are created, in `<project>/<topic>` folders for code projects. It must be outside every git repository. |
| `personal_roots` | Folders whose knowledge bases count as personal. |
| `[embedding] backend` | `"ollama"` for the best search, or `"wordllama"` for the built-in model that needs nothing installed. |

The full list, with every default and a line of explanation each, is in
[`config.example.toml`](../config.example.toml).

One thing to expect: if you change the `[embedding]` settings or the section size
(`max_chunk_tokens`), kbhub rebuilds the whole index on the next search. That is normal and takes
a moment.

## Search quality

kbhub searches in two ways at once and merges the results:

- **By meaning.** A question about "refunds" can find a note that only says "money back". This
  needs Ollama running, with the `nomic-embed-text` model.
- **By exact words.** Good for names, ticket numbers and anything you remember word for word.

If Ollama is not running, search still works, but by exact words only. Nothing is lost: kbhub
fills in the missing meaning data the next time Ollama is up. `kbhub doctor` tells you which mode
you are in.

## Updating and sharing

kbhub is installed as a git copy of this repository in `~/.kbhub`. `kbhub update` fetches the
newest version with `git pull` and reconnects it.

To share kbhub with someone, give their GitHub account access to the repository and send them the
two install lines from the README. That is all they need.
