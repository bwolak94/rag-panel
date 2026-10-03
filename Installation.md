# Claude Code Configuration — Installation

## Structure

```
your-repo/
├── CLAUDE.md                    ← from this package (project memory + rule imports)
├── docs/                        ← copy your project documents here (PRD, Architecture, API, ...)
└── .claude/                     ← rename the dot-claude folder to .claude
    ├── settings.json            ← permissions (shared in the repo)
    ├── agents/                  ← 7 sub-agents
    │   ├── architect.md         (decisions, ADRs — call first)
    │   ├── design.md            (UX, API contracts, flows, copy)
    │   ├── ml-engineer.md       (models, benchmarks, evaluation)
    │   ├── rag-engineer.md      (LangGraph, retrieval, prompts)
    │   ├── backend-dev.md       (FastAPI, Postgres, integrations)
    │   ├── microservices.md     (queues, Docker/K8s, observability)
    │   └── python-reviewer.md   (review — call last)
    ├── skills/                  ← procedures (run as /name or automatically)
    │   ├── new-endpoint/
    │   ├── langgraph-node/
    │   ├── rag-eval/
    │   └── tenant-isolation-check/
    └── rules/                   ← imported by CLAUDE.md
        ├── coding-standards.md
        ├── security.md
        └── rag-conventions.md
```

## Steps

1. Unpack the archive in the repository root.
2. Rename `dot-claude` to `.claude` (if you unpacked from `claude-config.zip`, the folder already has the correct name).
3. Leave `CLAUDE.md` in the repository root; copy project documents into `docs/`.
4. Verify: `claude` → `/agents` should list 7 agents; `/skills` (or typing `/new-endpoint`) should show the skills.

## Workflow

- **New feature:** "Use the architect agent to decide how to build X" → pass the result: "backend-dev: implement according to this decision" → "python-reviewer: review the changes."
- **RAG changes:** rag-engineer (implementation) + skill `/rag-eval` after prompt changes.
- **Before release:** `/tenant-isolation-check`.
- Agents cannot see each other's conversations — paste the output of one into the next agent's prompt (or let the main Claude Code thread orchestrate: describe the task and it will delegate according to CLAUDE.md).

## Customisation

- Agent models: architect has `model: opus` (more expensive, better for decisions); others inherit your default. Change in the frontmatter according to your budget.
- `settings.json` — list of allowed Bash commands; extend as needed.
