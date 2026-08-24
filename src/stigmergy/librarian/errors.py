"""Librarian configuration and Git errors."""


class LibrarianError(RuntimeError):
    pass


class LibrarianConfigError(LibrarianError):
    pass


class WorktreeError(LibrarianError):
    pass


class GitError(LibrarianError):
    pass
