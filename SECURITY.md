# Security policy

## Reporting

Report vulnerabilities through
[GitHub private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability).
Do not open a public issue.

## Security boundaries

Stigmergy treats captured text, files, URLs, Slack content, page bodies, titles, links, and entity
names as untrusted input.

- Untrusted material must not become model instructions or escape its structured boundary.
- Authentication and ACL checks must not reveal whether hidden pages, sources, entities, captures,
  contradictions, or changes exist.
- A capture files in exactly one layer; nothing derived from it may reach `public` or another
  layer, and every read is scoped to the reader's layers in SQL before any cap.
- Public fetching must reject non-public network destinations and revalidate redirects and DNS.
- Original artifacts and exact patches must remain private and hash-verified.
- The commit must contain exactly the paths and bytes accepted by the writer gates.
- Secrets, tokens, session values, presigned URLs, artifact bytes, restricted titles, and DSNs must
  not enter logs or safe errors.
- Only the trusted writer identity may change `wiki/`, `sources/`, or `layers/`.

The loopback Docker credentials are fixed test values, not deployment credentials. Credential-like
test fixtures grant no access and are allowlisted only where the secret-scanning test requires it.

## Operator responsibilities

People, agents, layers, tokens, and Slack channel layers live in each workspace's Postgres roster
and are managed in the backoffice. Each identity has personal tokens shown once and stored only as
SHA-256; revoke a token, disable an identity, or remove a layer member there. On a fresh deployment,
create the first platform operator with `stigmergy-admin operator bootstrap <email> --name <name>`;
the operator creates workspaces in the master console, which prints each workspace's first
administrator token. With direct database access, `stigmergy-admin --tenant <slug> mint <email>`
recovers a lost administrator token and `stigmergy-admin operator mint <email>` a lost operator
token. Keep database, model, object-store, Slack, webhook, and GitHub App credentials in the
deployment secret store. When Postgres is hosted with a public data API (for example Supabase), the
API roles must have no access to the schema that holds Stigmergy's tables.
