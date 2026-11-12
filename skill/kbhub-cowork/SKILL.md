---
name: kbhub-cowork
description: Use in Cowork for any project, coding or research task, and for questions such as "what did we decide", "why do we", "how do I run", "where are we up to". Searches the central knowledge index on the user's Mac (decisions, lessons, plans, runbooks, handovers) read-only through the computer link before any knowledge file is opened, and records new knowledge as markdown. Not for Claude Code, which has the kbhub tools built in.
---

# Knowledge index from Cowork

The user's Mac keeps one search index of every knowledge base (decisions, lessons, plans,
runbooks, research, handovers), whether the markdown lives inside a code base or in a folder of its
own. Cowork cannot reach the search tools Claude Code has, so it reads the same index **read-only**
with the commands below. One search costs about 2k tokens; opening the knowledge folders instead
costs tens of thousands and returns superseded material as if it were current.

## Rules

- Search before opening any knowledge file, and before answering from memory.
- Read-only: every command carries `--no-sync`. From Cowork, never sync, rebuild, or change the
  index, the registry or the config, and never run a command of this tool without `--no-sync`.
- Never act on superseded results.
- The index is as fresh as the Mac's last sync. Say so when an answer depends on recent work.

## 1. Set up (once per session)

1. Request folder access to `~/.kbhub`. That one folder is enough to search and read.
2. Check that the index is where the commands expect it:

   ```bash
   ls "$HOME/mnt/.kbhub/db/manifest.json" && command -v uv
   ```

   - If the file is not there, find where the folder was mounted and use that path instead of
     `$HOME/mnt/.kbhub` in both places of every command:
     `find / -maxdepth 6 -path "*.kbhub/db/manifest.json" 2>/dev/null`
   - If `uv` is missing, install it into the VM's home (`pip install --user uv`, or the official
     installer) and try again.
   - If either still fails, stop and tell the user exactly what is missing. Do not fall back to
     reading whole knowledge folders without saying so.
3. The first command builds a small Linux environment in the VM's home (`~/.kbhub-venv`); it
   needs network access once and takes a minute. Nothing is installed into the user's folders.

## 2. Search

```bash
KBHUB_HOME="$HOME/mnt/.kbhub" UV_PROJECT_ENVIRONMENT="$HOME/.kbhub-venv" PYTHONDONTWRITEBYTECODE=1 uv run --frozen --directory "$HOME/mnt/.kbhub" kbhub search --no-sync --keyword-only "<words>"
```

Everything up to `kbhub` is the same for every command; only the part after it changes.
"No index found" means `KBHUB_HOME` points at the wrong folder: go back to set-up step 2.

Options: `--project <name>` (a repo name such as `core` covers every `core/<topic>`),
`--doc-type <type>` (`decisions`, `lessons`, `plan`, `runbook`, `research`, `handover`, `state`,
`open-items`, `entrypoint`, `notes`), `-k <n>` results (default 6, at most 15), `--all` to include
superseded material, `--scope personal|all`.

**This search matches words, not meaning.** Word endings and common filler words are handled, but
synonyms are not, so the query has to contain words the note itself uses:

- Use the exact identifiers you have: ticket numbers (`PE-472`), decision ids (`D-012`), function
  and file names, id shapes (`<clientId>-<swId>`).
- Use two to five distinctive words, not a sentence: `site channel rollback`, not "how do we roll
  back the site channel migration if something goes wrong".
- If the result is thin, search again with other words for the same thing (`rollback` / `revert` /
  `undo`; `channel` / `conversation`). Two or three short searches beat one long one.
- Narrow with `--project` and `--doc-type decisions` when you know where the answer should be.
- No hits after a few variations: say the knowledge base has nothing on it. Do not guess.

Each hit reads `project · path:lines | doc type | status`, then `§ heading breadcrumb`, then the text.

- `current`: use it.
- `background`: context only, not a decision; check it against current decisions.
- Superseded matches are only listed, under "Also matched in SUPERSEDED material". Do not act on
  them. Look at them (`--all`) only when the user asks about history, and say they are superseded.

## 3. Read a full section

When a hit is relevant but cut short, print the whole section from the index:

```bash
KBHUB_HOME="$HOME/mnt/.kbhub" UV_PROJECT_ENVIRONMENT="$HOME/.kbhub-venv" PYTHONDONTWRITEBYTECODE=1 uv run --frozen --directory "$HOME/mnt/.kbhub" kbhub section --no-sync "<project>" "<path>" "<heading>"
```

`<project>` and `<path>` are copied from the hit. `<heading>` is the hit's `§` breadcrumb or any
part of it (a decision number is enough); sections nested under it come along. Leave the heading
out to print the whole file, which is right for a knowledge base's `STATE.md` and its entry point
(`README.md` or `CLAUDE.md`) and for nothing else by default. Sections marked `SUPERSEDED` in the
output are history.

## 4. See what exists

The same command with a different ending:

- `kbhub projects --no-sync` lists the knowledge bases in the index: name, scope, size, and
  the folder on the Mac.
- `kbhub sources --no-sync --project <name>` lists a project's files with doc type and status.

At the start of work on a project, list the projects, then read that project's `STATE.md` with the
section command. It says where the work is up to and what comes next.

## Scope: work and personal

Searches and listings from Cowork cover work knowledge only. Personal projects are kept apart on
purpose. Reach them only when the user asks about a personal project: name it with `--project`, or
add `--scope personal` or `--scope all`.

## Writing knowledge back

The index is derived from markdown, so new knowledge is written as markdown, never into the index.

1. Find the folder: the `projects` listing shows each knowledge base's folder on the Mac. Request
   access to that folder; without it, give the user the text to save instead.
2. Follow the layout that is already there:
   - **Decisions** go in `decisions.md`, newest last, numbered `D-NNN`, with date, status, who
     decided, the decision and the reason. To reverse one, never delete it: add
     `(superseded YYYY-MM-DD, see D-NNN)` to the old heading and write the new decision below.
   - **Lessons** (gotchas, failed approaches) go in `lessons.md`; unanswered questions in
     `open-items.md`.
   - **`STATE.md`** stays short and current: Status, Active decisions (ids only), Blockers, Next
     steps. Update it when the session ends.
   - **Handovers**: `handovers/YYYY-MM-DD-<topic>.md` with what was done, what is in flight, how
     to verify it, and the exact next step.
3. A knowledge base inside a code base is committed with the code. From Cowork, write the files
   only; leave branching and committing to the user.
4. Tell the user that what you wrote becomes searchable after the Mac's next sync, which happens
   the next time a Claude Code session there searches. Until then, a search from Cowork will not
   find it.

Creating a new knowledge base, or adding an existing folder to the index, is done from Claude Code
on the Mac. If the user wants one, write the notes as markdown and tell them to ask Claude Code to
set it up.
