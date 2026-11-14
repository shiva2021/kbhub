<!-- kbhub:start -->
## Knowledge bases (kbhub)

Project knowledge (decisions, lessons, plans, runbooks, handovers) lives as markdown in knowledge
bases, kept outside the code base by default (or inside it, `<repo>/knowledge-base/<topic>/`, when
asked), all indexed by the `kbhub` MCP server.

- Before opening knowledge files, call `search_kb`. Use `get_section` for the full text of a hit and
  `list_projects` for names. Searches cover the session's scope (work or personal); name a
  `project` or pass `scope="all"` to reach the rest.
- Read a knowledge base's `README.md` / `CLAUDE.md` and `STATE.md` in full; nothing else by default.
- Never act on superseded results.
- When the session start says the user has not been asked yet about a knowledge base for this code
  base, ask them once, first thing, then call `kb_setup(enable=true|false)`. The answer is
  remembered: never ask again, and never offer one after a no.
- A new topic gets its own knowledge base: `create_kb(topic)`, created outside the repo. Write into
  the repo (`in_repo=true`) only when the user asks. Never create a `.kb.toml` inside a git repo.
- If a knowledge-base folder exists but is not registered, use `scan_kbs` then `register_kb`.
- Record decisions, lessons and handovers as markdown in the knowledge base and keep its `STATE.md`
  current, following the `kbhub` skill.
<!-- kbhub:end -->
