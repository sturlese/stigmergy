"""Domain errors of the capture subsystem. Library code raises these instead of `SystemExit`;
the CLI maps them to a stderr line and an exit code, the MCP adapter to an `{"error": ...}`
payload.

Which messages may cross the network: `SubmissionRejected` messages are safe to echo verbatim —
the caller's own field/hint keys and static, value-free text, never a bucket, endpoint, DSN,
path, or whether another identity exists. `EvidenceError` is a class-name-only summary.
`QueueStateError` is a SCRUBBED state sentence — submission id, status and attempt counts only,
never material and never another identity — because the MCP adapter echoes it verbatim. Generic
over HTTP, specific in the local CLI — which is why the CLI, not the library, may name a DSN or
bucket.
"""


class CaptureError(RuntimeError):
    """Base class for capture-subsystem errors."""


class SubmissionRejected(CaptureError):
    """A submission was refused BEFORE anything was written — no queue row, no blob. A
    server-owned field arriving as an argument or a hint is an ERROR, never quietly ignored: a
    client that could set `submitted_by` could forge authorship."""


class EvidenceError(CaptureError):
    """The evidence store could not be reached or written. Deliberately value-free
    (`evidence store unavailable (<ExceptionClass>)`): boto3's exceptions embed the bucket,
    endpoint and credentials, none of which may reach the wire."""


class QueueStateError(CaptureError):
    """A transition attempted from a state that does not allow it. The state machine is enforced
    in the WHERE clause, not the caller — a lost race must fail loudly rather than silently
    update nothing."""
