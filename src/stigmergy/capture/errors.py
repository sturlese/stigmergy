"""Domain errors of the capture subsystem.

Same posture as `stigmergy.index.errors` and `stigmergy.server.errors`: library code raises these
instead of `SystemExit`, and the console entry point (`capture.cli`) maps them to a clean stderr
line plus a non-zero exit code. The MCP adapter maps them to a `{"error": ...}` payload.

**Which messages may cross the network.** `SubmissionRejected` messages are written to be safe
to echo verbatim: they name the CALLER's own field/hint keys and static, value-free text (a size
limit, the allowed kinds, the allowed hint keys) — never a bucket, an endpoint, a DSN, a
filesystem path, or whether another identity exists. `EvidenceError` and `QueueStateError`
messages are class-name-only summaries for the same reason: the real cause is logged
server-side. The rule is "generic over HTTP, specific in local CLI" — which is why the CLI, not
the library, is where a raw DSN or bucket name is allowed to appear.
"""


class CaptureError(RuntimeError):
    """Base class for capture-subsystem errors."""


class SubmissionRejected(CaptureError):
    """A submission was refused BEFORE anything was written — no queue row, no blob.

    The two security-load-bearing cases: a server-owned field arriving as a tool argument, and
    a server-owned field arriving inside `hints`. Both are an ERROR, never
    something quietly ignored — a client that could set `submitted_by` could forge authorship of
    company knowledge.
    """


class ReplyRejected(CaptureError):
    """A reply to a `needs_input` row was refused — nothing was recorded and the row did not move.

    **Two kinds of refusal ride this one class, and the difference is in the WORDING, not the type.**

    - An IDENTITY failure gets one fixed, generic sentence, identical whether the id does not
      exist, belongs to somebody else, or belongs to somebody else and is not even `needs_input`.
      The three must be indistinguishable from outside or the response becomes an oracle for "does
      submission 14 exist" / "whose is it" — the same no-existence-leak posture `read_page` takes.
    - A STATE failure, raised only for a caller already authorized to see the row, may name the
      actual status. It costs no leak (that caller can read the row through `brain_submissions`
      anyway) and the generic sentence would be actively misleading there: it reads as "you are not
      allowed", which is false for a person looking at their own capture.

    Both are safe to echo verbatim over HTTP: neither names another identity, a path, or anything
    the caller did not already know.
    """


class EvidenceError(CaptureError):
    """The content-addressed evidence store could not be reached or written.

    The message is deliberately value-free (`evidence store unavailable (<ExceptionClass>)`):
    the bucket name, the endpoint and the credentials must never reach the wire, and the
    exception boto3 raises embeds all of them.
    """


class QueueStateError(CaptureError):
    """A transition was attempted from a state that does not allow it (e.g. finishing a row that
    was never claimed). The queue's state machine is enforced in the WHERE clause, not in the
    caller — a lost race must fail loudly rather than silently update nothing."""
