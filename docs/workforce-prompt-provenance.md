# Workforce Prompt Provenance

Workforce prompts are implementation assets, not free-form copy. A public team or private specialist may only be
constructed when its component ID exists in `agents/workforce/prompt_provenance.py` and every referenced source exists.

## Gate

1. Research an open-source implementation or framework-owned example for the role.
2. Pin the source to a commit SHA when the upstream repository exposes a stable file.
3. Record only the adopted principles. Do not copy a third-party prompt wholesale.
4. The prompt constructor appends those recorded principles to the runtime instructions; adapt them to the local tools,
   capability registry, output contract, and permission boundary in the role-specific text.
5. Add deterministic prompt-contract tests and behavioral eval cases before publishing the component.
6. Re-research and update provenance when a role, tool boundary, or model family changes materially.

## Sources Used In V1

| Source | Principles adopted | Workforce roles |
|---|---|---|
| SWE-agent default config | Read relevant code, reproduce, make a minimal patch, rerun evidence | Code, Tester, Fixer, Engineering Team |
| OpenHands goal judge | Inspect current workspace state; require file, command, or test evidence | Engineering reviewers, Quality Council |
| DeerFlow deep research | Multi-angle queries, full-source reading, iterative gap search, limitations | Search, Web Research, SEO, Market Research |
| Agno demo-os research team | Primary sources, cross-check figures, trust boundaries, contradiction handling | Research and evidence roles |
| Agno demo-os Dash team | Minimal delegation, conditional decomposition, synthesis | Router and domain leads |
| Agno demo-os Operator | Inspect, blast radius, rollback, approval | SRE, Security, Database |
| Agno demo-os content pipeline | Research before drafting and specific independent evaluation | Growth, Content, Critic |
| Microsoft content generation accelerator | Brief parsing, enterprise grounding, separate compliance validation | Marketing, Content, Growth Lead |
| CAMEL-AI OWL | Dynamic specialist collaboration and tool/model capability matching | Workforce Router and team leads |

The exact URLs, revisions, role mapping, and adopted principles are executable metadata in
`agents/workforce/prompt_provenance.py`. CI fails when a registered Workforce component has no mapping.
