<!-- kbhub:start -->
## Knowledge bases (kbhub)

Project knowledge (decisions, lessons, plans, runbooks, handovers) lives as markdown in knowledge
bases, inside code bases (`<repo>/knowledge-base/<topic>/`) or standalone, all indexed by the
`kbhub` MCP server.

- Before opening knowledge files, call `search_kb`. Use `get_section` for the full text of a hit and
  `list_projects` for names. Searches cover the session's scope (work or personal); name a
  `project` or pass `scope="all"` to reach the rest.
- Read a knowledge base's `README.md` / `CLAUDE.md` and `STATE.md` in full; nothing else by default.
- Never act on superseded results.
- Working in a code base with no knowledge base for the topic? Offer to create one with
  `create_kb(topic)`; one topic per functionality. It only writes files: commit them with the change
  they describe, on a branch. Never create a `.kb.toml` inside a git repo.
- If a knowledge-base folder exists but is not registered, use `scan_kbs` then `register_kb`.
- Record decisions, lessons and handovers as markdown in the knowledge base and keep its `STATE.md`
  current, following the `kbhub` skill.
<!-- kbhub:end -->
