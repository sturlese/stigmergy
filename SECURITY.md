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
- Restricted evidence must not influence a broader page.
- Public fetching must reject non-public network destinations and revalidate redirects and DNS.
- Original artifacts and exact patches must remain private and hash-verified.
- The commit must contain exactly the paths and bytes accepted by the writer gates.
- Secrets, OAuth tokens, presigned URLs, artifact bytes, restricted titles, and DSNs must not enter
  logs or safe errors.
- Only the trusted writer may change `wiki/`, `sources/`, or `ops/entity-registry.json`.

The loopback Docker credentials are fixed test values, not deployment credentials. Credential-like
test fixtures grant no access and are allowlisted only where the secret-scanning test requires it.

## Operator responsibilities

Generate per-deployment MCP tokens with `stigmergy-issue-token`, store only their hashes in the
server token map, and rotate them by replacing that map. Configure the backoffice only with a hash
from `stigmergy-admin-token`; without `STIGMERGY_ADMIN_TOKEN_HASH`, the admin routes remain disabled.
Keep database, model, object-store, Slack, webhook, and GitHub App credentials in the deployment
secret store.
