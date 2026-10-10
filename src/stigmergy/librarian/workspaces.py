"""One knowledge checkout per tenant for the single writer.

Each tenant's repository is cloned on its first job under the writer's repository root, then
fetched and verified before every use. A job receives only its own tenant's checkout: the
librarian's context, the gates, the commit, and the push all happen inside it.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable

from stigmergy.kernel import tenancy
from stigmergy.knowledge.planner import PydanticPlanner, ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import bootstrap, gitcmd
from stigmergy.librarian.errors import LibrarianConfigError, LibrarianError
from stigmergy.server import platform


class CheckoutUnavailable(RuntimeError):
    """A tenant's checkout could not be cloned, fetched, or verified; the job retries later."""

    retryable = True
    category = "checkout_unavailable"


def github_url(tenant: platform.Tenant) -> str:
    return f"https://github.com/{tenant.repo}.git"


class Workspaces:
    """`deps_for(tenant id)`: the writer dependencies bound to that tenant's checkout."""

    def __init__(self, conn, settings, evidence, *, planner=None,
                 url_for: Callable[[platform.Tenant], str] = github_url):
        self.conn = conn
        self.settings = settings
        self.evidence = evidence
        self.planner = planner or (PydanticPlanner(settings) if settings.backend == "pydantic"
                                   else ScriptedPlanner())
        self.url_for = url_for
        self.root = os.path.abspath(settings.repo)
        self._ready: set[str] = set()

    def path(self, tenant_id: str) -> str:
        return os.path.join(self.root, str(tenant_id))

    def tenant(self, tenant_id: str) -> platform.Tenant:
        found = platform.tenant_by_id(self.conn, tenant_id)
        if found is None:
            raise LibrarianConfigError("the job names a tenant that does not exist")
        return found

    def refresh(self, tenant_id: str) -> str:
        """Clone or fast-forward the tenant's checkout and require it clean at the remote tip."""
        tenant = self.tenant(tenant_id)
        repo = self.path(tenant.id)
        try:
            bootstrap.prepare(repo=repo, url=self.url_for(tenant), branch=self.settings.branch)
            if repo not in self._ready:
                gitcmd.reap(repo, self.settings.worktree_root)
                self._ready.add(repo)
        except (LibrarianError, OSError) as error:
            self._ready.discard(repo)
            raise CheckoutUnavailable(f"the workspace checkout could not be prepared "
                                      f"({error.__class__.__name__})") from error
        return repo

    def forget(self, tenant_id: str) -> bool:
        """Remove a deleted tenant's checkout and its worktrees; whether there was one."""
        repo = self.path(tenancy.checked_uuid(tenant_id))
        self._ready.discard(repo)
        if not os.path.isdir(repo):
            return False
        try:
            gitcmd.reap(repo, self.settings.worktree_root)
        except (LibrarianError, OSError):
            pass
        shutil.rmtree(repo)
        return True

    def deps_for(self, tenant_id: str) -> WriterDeps:
        repo = self.path(tenant_id)
        if repo not in self._ready:
            repo = self.refresh(tenant_id)
        return WriterDeps(settings=self.settings, evidence=self.evidence, planner=self.planner,
                          repo=repo)


class SingleWorkspace:
    """One checkout serving whichever tenant the connection acts for: tests and local runs."""

    def __init__(self, deps: WriterDeps):
        self.deps = deps
        self.settings = deps.settings
        self.evidence = deps.evidence

    def refresh(self, tenant_id: str) -> str:
        return self.deps.repo

    def forget(self, tenant_id: str) -> bool:
        return False

    def deps_for(self, tenant_id: str) -> WriterDeps:
        return self.deps


def resolve(deps_or_workspaces):
    """Accept the writer dependencies of one checkout or the per-tenant workspaces."""
    if isinstance(deps_or_workspaces, WriterDeps):
        return SingleWorkspace(deps_or_workspaces)
    return deps_or_workspaces
