"""Shared authorization guard for knowledge mutations."""

from __future__ import annotations

from dataclasses import dataclass

from stigmergy.kernel.acl import flows_into
from stigmergy.server.acl import visible


class WriteRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class WriteContext:
    actor_groups: frozenset[str] | None
    content_acl: tuple[str, ...] | None
    unrestricted: bool = False


def allow_create(context: WriteContext, target_acl: tuple[str, ...] | None) -> None:
    if target_acl != context.content_acl:
        raise WriteRefused("new knowledge must retain the capture audience")
    _actor_may_read(context, target_acl)


def allow_existing(context: WriteContext, page_acl: tuple[str, ...] | None) -> None:
    _actor_may_read(context, page_acl)
    if not flows_into(_list(context.content_acl), _list(page_acl)):
        raise WriteRefused("restricted material cannot affect a broader page")


def allow_explicit_master(context: WriteContext) -> None:
    if not context.unrestricted:
        raise WriteRefused("this operation requires the master identity")


def _actor_may_read(context: WriteContext, acl: tuple[str, ...] | None) -> None:
    if context.unrestricted:
        return
    groups = None if context.actor_groups is None else set(context.actor_groups)
    if not visible(_list(acl), groups):
        raise WriteRefused("the actor cannot affect knowledge they cannot read")


def _list(value: tuple[str, ...] | None) -> list[str] | None:
    return None if value is None else list(value)
