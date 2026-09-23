"""Librarian configuration and Git errors."""


class LibrarianError(RuntimeError):
    pass


class LibrarianConfigError(LibrarianError):
    pass


class WorktreeError(LibrarianError):
    pass


class GitError(LibrarianError):
    retryable = False


class TransientGitError(GitError):
    """A bounded Git transport timeout whose outcome can be reconciled safely."""

    retryable = True
