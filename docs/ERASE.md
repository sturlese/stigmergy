# Emergency erase

Erases everything a deployment holds, every workspace, at once and for good. To erase one layer,
use the backoffice instead ([`OPERATIONS.md`](./OPERATIONS.md), Erasing a layer); to erase one
workspace, follow [Erasing one workspace](#erasing-one-workspace) below. Starting again is a new
organization, bucket, and project, bootstrapped as `OPERATIONS.md` describes.

Run it from the platform checkout with `fly`, `gh`, `supabase`, and `rclone` signed in to the owner
accounts and the deployment's `.env` loaded. Commands read secrets from the environment and never
print them. Note the start time for step 7.

```sh
set -a; . ./.env; set +a
ORG="$STIGMERGY_GITHUB_ORG"   # every workspace's repository lives here
```

## 1. Stop

```sh
fly scale count app=0 worker=0 slack=0 --app "$FLY_APP" --yes
```

## 2. Revoke

- **OpenRouter:** <https://openrouter.ai/settings/keys>, delete the deployment's key.
- **Slack:** revoke the bot token, then delete the app (api.slack.com/apps, the app, Basic
  Information, Delete App), which also ends its Socket Mode token.

  ```sh
  curl -s -X POST https://slack.com/api/auth.revoke -H "Authorization: Bearer $SLACK_BOT_TOKEN"
  ```

  Expect `"revoked":true`. Messages already posted stay in Slack.
- **GitHub App:** GitHub, Settings, Developer settings, GitHub Apps, the librarian app, Advanced,
  Delete GitHub App. It is uninstalled everywhere; a token it already issued lasts at most an hour.

The R2 tokens are revoked in step 4, after they delete the evidence.

## 3. Knowledge

```sh
gh auth refresh -h github.com -s delete_repo
for repo in $(gh repo list "$ORG" --limit 1000 --json nameWithOwner --jq '.[].nameWithOwner'); do
  gh repo delete "$repo" --yes
done
fly volumes list --app "$FLY_APP"      # expect none: the writer's checkouts are on the machine
fly apps destroy "$FLY_APP" --yes      # machines, volumes and snapshots, secrets, images
```

GitHub lets the owner restore a deleted repository for up to 90 days. Ask GitHub Support to remove
them and their cached views for good at <https://support.github.com>, naming the repositories, and
keep the ticket number. Fly's log search keeps about 7 days of logs, which hold identifiers and timings,
not knowledge text.

Delete every local clone, and ask anyone else who cloned a repository to delete theirs:

```sh
find ~ -maxdepth 4 -type d -name '*-brain' 2>/dev/null   # review the list, then delete each clone
```

## 4. Evidence

```sh
export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
  RCLONE_CONFIG_R2_ACCESS_KEY_ID="$STIGMERGY_EVIDENCE_ACCESS_KEY_ID" \
  RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY" \
  RCLONE_CONFIG_R2_ENDPOINT="$STIGMERGY_EVIDENCE_ENDPOINT"
rclone delete "r2:$STIGMERGY_EVIDENCE_BUCKET"
rclone size "r2:$STIGMERGY_EVIDENCE_BUCKET"   # expect 0 objects
```

R2 keeps no object versions, so a deleted object cannot be recovered. Then, in the Cloudflare
dashboard: R2, the bucket, Settings, Delete bucket; and R2, Manage API tokens, delete the
deployment's tokens.

## 5. Database

```sh
supabase projects list                 # the deployment's project, Central EU (Frankfurt)
supabase projects delete <project-ref>
```

Deleting the project removes the database with its backups and point-in-time recovery; a deleted
project cannot be restored.

## 6. Check

```sh
gh repo list "$ORG" --limit 1000       # expect: nothing
fly apps list | grep -w "$FLY_APP"     # expect: nothing
supabase projects list                 # expect: the project is gone
```

The bucket and the tokens are gone from the Cloudflare dashboard, the key from OpenRouter, the app
from Slack and GitHub.

## 7. Record

Outside the system: when it started and ended, who ran it, what was erased (repositories, Fly app,
bucket, Supabase project, the keys and apps revoked), the GitHub Support ticket, and what remains:
messages already posted in Slack, clones on other people's machines until they delete them, and
GitHub's copy until Support removes it or 90 days pass.

## Erasing one workspace

Erases one workspace and leaves every other untouched. In the master console, suspend it, then
delete it from its row and type its slug to confirm. The writer, under its lock and resuming after
any stop:

1. checks the workspace is still suspended and does not own the Slack app (otherwise the request
   fails having deleted nothing), then deletes its repository in the GitHub organization (one
   already gone counts as deleted; from here on the request keeps trying until it finishes);
2. deletes every row of every tenant table and its `tenants` row in one transaction, and clears it
   from operator sessions acting in it;
3. drops its HNSW indexes concurrently;
4. deletes every evidence object its rows referenced that no other workspace references
   (`capture.references.is_live` looks at every workspace);
5. removes its checkout from the worker.

`workspace_erasures` keeps the request and its receipt, and `platform_actions` records it. GitHub
keeps a deleted repository restorable for 90 days and its cached views until Support removes them;
Supabase backups keep the rows until their retention ends. The workspace that owns the Slack app is
erased only with the deployment, as above.
