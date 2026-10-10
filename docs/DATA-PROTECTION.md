# Data protection

Whoever operates a deployment decides where its data lives. This page lists the services the
reference deployment uses (Fly, Supabase, Cloudflare R2, GitHub, OpenRouter, and Slack where it is
enabled) and every place personal data ends up, so an operator can prepare its own records. Every
service that touches knowledge should be a processor under its own data processing agreement (DPA);
the reference deployment keeps data at rest in the EU (Supabase and Fly in Frankfurt, R2 under EU
jurisdiction) except the knowledge repositories on GitHub. How content is erased:
[`specs/data-erasure.md`](../specs/data-erasure.md); erasing everything at once:
[`ERASE.md`](./ERASE.md).

## Processors

| Service | What it processes | Where | DPA | Sub-processors | Transfers outside the EU |
|---|---|---|---|---|---|
| Supabase | Index, roster, capture queue, change ledger, audit log | `eu-central-1` (Frankfurt) | [DPA](https://supabase.com/legal/dpa), offered with the terms | [list](https://supabase.com/legal/customer-resources/subprocessor-list) | SCCs in the DPA |
| Fly.io | The application, the writer's checkout of the knowledge repository, logs | `fra` (Frankfurt) | [compliance documents](https://docs.fly.io/security/security-at-fly-io#compliance-documents) | [list](https://fly.io/legal/sub-processors.md) | [Data Privacy Framework](https://fly.io/legal/data-privacy-framework.md), SCCs |
| Cloudflare R2 | The original bytes of every capture and the exact patch of every change | EU jurisdiction bucket ([spec §2](../specs/data-erasure.md)) | [DPA](https://www.cloudflare.com/cloudflare-customer-dpa/), part of the self-serve agreement | [list](https://www.cloudflare.com/gdpr/subprocessors/) | See the DPA |
| GitHub | Each workspace's knowledge repository and its history, in the platform's private organization | United States | [DPA](https://github.com/customer-terms/github-data-protection-agreement) | [list](https://docs.github.com/en/site-policy/privacy-policies/github-subprocessors) | Data Privacy Framework, SCCs |
| OpenRouter | Every source and question sent to a model: filing, answers, embeddings, OCR, layer routing, alias decisions; only zero-retention providers | United States; providers vary | [DPA](https://openrouter.ai/data-processing-agreement), offered with the terms for commercial use | [trust center](https://trust.openrouter.ai/) | SCCs in the DPA |
| Slack | Questions, answers, and captured threads | United States unless the workspace uses Slack data residency | [DPA](https://slack.com/terms-of-service/data-processing), part of Slack's terms | [list](https://slack.com/slack-subprocessors) | See the DPA |

The local bridge reads local files and private Google Drive documents with the user's own
credentials on the user's machine; Google is not a processor of the service.

## Where personal data lives

| Store | What it holds | Erased by |
|---|---|---|
| Knowledge repositories (GitHub, the writer's checkouts, every clone) | Each workspace's current pages and their full history | Layer erase rewrites one workspace's history; erasing a workspace deletes its repository |
| R2 | Original bytes, exact change patches | Layer erase deletes the layer's objects; erasing a workspace deletes the objects no other workspace references; emergency erase deletes the bucket |
| Postgres | Per workspace: capture requests, extractions and reports; artifacts, uploads, Slack submissions; change manifests; index text, fragments and embeddings; graph measures; model usage; audit and admin arguments; the roster (names, emails). Platform-wide: workspaces, operators, access requests (emails) | Layer erase deletes or redacts the layer's rows; erasing a workspace deletes its rows; emergency erase deletes the database |
| Supabase backups | Copies of Postgres | Expire with their retention; deleted with the project |
| A refile's export file (operator machine, only during a refile) | One workspace's people and capture requests | The operator deletes it after the handover |
| Logs (Fly, Supabase) | Identifiers, timings, error classes; no knowledge text | Expire with their retention |
| Model providers | Nothing: zero-retention routing only | Not applicable |

## Before a pilot with outside data

1. Accept every DPA above; Fly's is downloaded from its compliance documents.
2. Tell the people whose words are captured, for example in the meeting invitation, that notes are
   taken with AI and kept in an internal knowledge base, for what purpose, and for how long.
3. Give the pilot its own layer and its user only that layer, so its content can be erased whole
   from the backoffice ([`docs/OPERATIONS.md`](./OPERATIONS.md), Erasing a layer).
4. Keep a short note of purpose, who reads it, retention, and how erasure works.

## Legal documents

The operator writes its own data processing agreement, records of processing, privacy notice, and
sub-processor list. Public pages are served from the deployment
([`OPERATIONS.md`](./OPERATIONS.md), Deployment).
