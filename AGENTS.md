# Stigmergy — Codex instructions

Read [`CLAUDE.md`](./CLAUDE.md) before beginning substantive work. It is the shared project doctrine: orientation, invariants, working rules, testing doctrine, and completion requirements. Do not duplicate or change it for Codex-specific configuration.

The Stigmergy operating procedures are versioned under `.claude/skills/`. Codex does not discover that directory automatically, so read the matching `SKILL.md` directly when its trigger applies:

- [`land-a-change`](./.claude/skills/land-a-change/SKILL.md) for an issue, defect, or feature.
- [`validate-deployment`](./.claude/skills/validate-deployment/SKILL.md) after a deploy, a cross-subsystem release, or when deployment health needs evidence.

When those procedures refer to a Claude `squad:*` workflow, use the installed Build Squad Codex plugin equivalent:

| Claude workflow | Codex skill |
|---|---|
| `squad:fix` | `$build-squad:squad-fix` |
| `squad:define` | `$build-squad:squad-define` |
| `squad:build` | `$build-squad:squad-build` |
| `squad:refactor` | `$build-squad:squad-refactor` |
| `squad:review` | `$build-squad:squad-review` |
| `squad:semantic-architecture` | `$build-squad:squad-semantic-architecture` |
| `squad:breaking-change` | `$build-squad:squad-breaking-change` |
| `squad:final-validation` | `$build-squad:squad-final-validation` |

Do not edit `.claude/` for Codex-specific work.
