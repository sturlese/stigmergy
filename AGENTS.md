# Stigmergy: instructions for coding agents

Read [`CLAUDE.md`](./CLAUDE.md) before beginning substantive work. It is the shared project doctrine:
orientation, invariants, working rules, testing doctrine, and completion requirements. Do not
duplicate or change it for one agent's configuration.

The operating procedures are versioned under `.claude/skills/`. An agent that does not discover
that directory reads the matching `SKILL.md` directly when its trigger applies:

- [`land-a-change`](./.claude/skills/land-a-change/SKILL.md) for an issue, defect, or feature.
- [`validate-deployment`](./.claude/skills/validate-deployment/SKILL.md) after a deploy, a
  cross-subsystem release, or when deployment health needs evidence.
