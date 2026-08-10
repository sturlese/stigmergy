# Security policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository — **not** as a public issue.

Include what you did, what happened, and what you expected. A proof of concept helps; a working
exploit is not required. Expect an acknowledgement within a week.

## What this system's threat model actually is

Worth stating plainly, because it shapes what counts as a vulnerability here.

This platform ingests **untrusted material** — documents, transcripts, Slack threads — and hands it
to language models that also read **trusted instructions**. It then writes to a git repository and
serves answers to callers with different permissions. Three properties carry the weight:

**1. Untrusted content must never be read as instructions.** Page *bodies* — the bulk of what a
model ever sees — are wrapped by a hardened fence with in-band neutralization, so content carrying
the closing delimiter cannot end the fence early. Page-derived *fields* that travel as structure
(a title, an entity name, a link's label) are `neutralize_fence`d at the service boundary rather
than fenced, so they cannot break a fence either, but they do reach the model outside one. A way
to make captured material act as an instruction is a vulnerability; so is a page-derived string
that reaches a model neither fenced nor neutralized.

*Known gap, stated rather than implied: `views/synthesis.py` builds its member list out of each
member's path, title and `as_of` and puts all three into the prompt with neither treatment. The
title is the one an attacker steers most easily, but `as_of` is frontmatter-derived too, so the
gap is the whole line rather than one field of it.*

**2. `server.acl.visible()` is the one place read access is decided — and now the only
implementation of it.** A second, fail-open `visible()` used to live in `stigmergy.kernel`, the
module every package may import, with no caller at all; it was deleted rather than documented,
and `tests/test_contract_parity.py` fails if one comes back. Every read surface filters through
it, and an architecture test fails the build if a module reads the page index without naming an ACL
predicate or appearing on a declared exception list. Anything that returns a page — or merely
*confirms the existence* of a page — to an identity whose audiences do not cover it is a
vulnerability. Existence leaks count: a refusal that distinguishes "no such page" from "not yours"
is a bug, and the code deliberately returns the same string for both.

**3. The diff the gates approved is the diff that lands.** Code, not a model, decides what leaves:
eight gates run over the produced diff — zone, binary-page, body-rewrite, secrets, PII, frontmatter,
contract, anchoring — and the commit carries exactly the paths AND the bytes they approved, so a
file rewritten in the window between the gates and the commit is refused, not filed. A way to get
content committed that the gates did not approve, or to make a gate pass content it should refuse,
is a vulnerability.

Also in scope: authentication bypass on the HTTP transport, cross-identity leakage through the
per-request auth middleware or session handling, secrets reaching a log or an error message, and
sandbox escape from the librarian's confined write path.

## Not vulnerabilities

- **The local development stack.** `docker-compose.yml` binds Postgres and MinIO to loopback with
  well-known credentials (`stigmergy:stigmergy`, `minioadmin:minioadmin`). They are documented
  non-secrets for a local test stack, not a deployment.
- **Test fixtures that look like credentials.** The suite contains deliberately fake tokens and keys
  so that the secrets gate can be tested against realistic shapes. They grant nothing; the gitleaks
  configuration allowlists exactly two such literals, each with a written reason.
- **Denial of service through resource exhaustion** on a deployment you control.
- **A model producing a wrong answer.** The verifier's job is to keep an *untraced figure* from
  shipping, not to make the model correct. An answer that is wrong but carries no unverified figure
  is a quality problem. An answer that ships a figure the tools never returned is a bug — report it.

## For operators

Two things this repository cannot do for you:

- **Every token is per-deployment.** The HTTP transport authenticates against a hash map you
  generate (`stigmergy-issue-token`); rotate by regenerating the store. Nothing in this repository
  ships a credential.
- **The admin console is inert until configured.** With no `STIGMERGY_ADMIN_TOKEN_HASH` set, every
  `/admin` route refuses. It is an ops surface and is never a read surface over the knowledge repo.
