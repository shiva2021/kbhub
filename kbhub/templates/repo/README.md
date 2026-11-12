# {{name}}

Knowledge base for **{{topic}}** in this code base. It is indexed by kbhub as project `{{name}}`.

## Goal
<One or two sentences: what this area of the code base does and what this knowledge base is for.>

## Links
- Current state: `STATE.md` (read in full at the start of a session on this topic)
- Decisions: `decisions.md` · Lessons: `lessons.md` · Open items: `open-items.md`
- Runbooks: `runbooks/` · Research: `research/` · Plans: `plans/` · Handovers: `handovers/`

## How to work
- Read this file and `STATE.md` in full. For everything else, call `search_kb` (project `{{name}}`)
  before opening files here, then `get_section` for the full text of a hit.
- Never act on superseded results.
- Record decisions, lessons and handovers here as markdown, following the `kbhub`
  skill, and commit them with the code change they describe.
- Update `STATE.md` at the end of every session on this topic.
