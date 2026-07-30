"""Research graph — agentic multi-hop retrieval pipeline.

Activated when pipeline.prompt_config["research_mode"] is True.

Graph topology (fixed — change requires ADR and diagram update in docs/02-Architektura.md):

    node_research_plan →[sufficient or max_steps?]→ node_research_synthesize → END
           ↑                        ↓
           └──── node_research_retrieve ←──────────┘

The graph iteratively generates sub-queries, retrieves evidence, and reasons about
whether accumulated chunks are sufficient before producing a final synthesised answer.
"""
