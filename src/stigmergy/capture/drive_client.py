"""The Drive fetch seam: the operator's own Google auth, behind one protocol.

`GogDriveClient` shells out to the locally-authenticated `gog` CLI — the operator's credential
on the operator's machine. No Google credential exists server-side: the worker converts from the
evidence blob and never talks to Drive, so this stays a door and never becomes a mirror. Tests
inject a fake; nothing in the suite ever runs `gog`.

A NATIVE Google file has no original bytes; `gog drive download --format pdf` asks Drive itself
to export it, so the worker sees `.pdf` and no converter of ours touches a native format.
"""
import json
import os
import re
import subprocess
from dataclasses import dataclass

from stigmergy.capture.errors import SubmissionRejected

GOG_BIN_ENV = "STIGMERGY_DRIVE_GOG_BIN"
GOG_BIN_DEFAULT = "gog"

_NATIVE_MIME_PREFIX = "application/vnd.google-apps"

# Every URL shape Drive hands people, reduced to the one thing the API wants. Bare ids pass
# through untouched (the strictest pattern last — an id is a URL-safe token, never a slash).
_URL_ID_RES = (
    re.compile(r"/(?:file|document|presentation|spreadsheets)/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


@dataclass(frozen=True)
class DriveFile:
    """What the door needs to know about one Drive file, from one metadata call."""
    file_id: str
    name: str
    mime: str
    modified: str
    url: str
    size: int   # Drive's own size claim (bytes); 0 when Drive does not report one


def file_id_from(ref: str) -> str:
    """A Drive file id from a share URL or a bare id, refused when neither matches."""
    text = str(ref or "").strip()
    for pattern in _URL_ID_RES:
        match = pattern.search(text)
        if match:
            return match.group(1)
    if _BARE_ID_RE.match(text):
        return text
    raise SubmissionRejected(
        f"cannot find a Drive file id in {text!r} — pass the file's share URL or its bare id")


def is_native(mime: str) -> bool:
    return str(mime or "").startswith(_NATIVE_MIME_PREFIX)


class GogDriveClient:
    """The production seam: `gog drive get` for metadata, `gog drive download` for bytes.

    Every failure is reduced to a short operator sentence naming the step — `gog`'s own stderr
    is included because this is a LOCAL CLI talking to a LOCAL tool. The rule that error detail
    must not cross the wire guards servers, not an operator reading their own terminal.
    """

    def __init__(self, gog_bin: str | None = None):
        self.gog_bin = gog_bin or os.environ.get(GOG_BIN_ENV) or GOG_BIN_DEFAULT

    def _run(self, *args: str, step: str) -> str:
        try:
            proc = subprocess.run([self.gog_bin, *args], capture_output=True, text=True,
                                  timeout=300)
        except FileNotFoundError:
            raise SubmissionRejected(
                f"the `{self.gog_bin}` CLI is not installed (or not on PATH) — the drive door "
                f"fetches with the operator's own Google auth through it (brew install "
                f"steipete/tap/gogcli; `gog auth add`)") from None
        except subprocess.TimeoutExpired:
            raise SubmissionRejected(f"{step}: `{self.gog_bin}` timed out after 300s") from None
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise SubmissionRejected(f"{step} failed via `{self.gog_bin}` (rc={proc.returncode})"
                                     + (f": {detail}" if detail else ""))
        return proc.stdout

    def metadata(self, file_id: str) -> DriveFile:
        out = self._run("drive", "get", file_id, "--json", step="reading Drive metadata")
        try:
            info = json.loads(out).get("file") or {}
        except json.JSONDecodeError as ex:
            raise SubmissionRejected(
                f"reading Drive metadata: `{self.gog_bin}` returned something that is not "
                f"JSON ({ex})") from ex
        if not info.get("id") or not info.get("name"):
            raise SubmissionRejected(
                "reading Drive metadata: the response carries no file id/name — does the file "
                "exist, and does this account see it?")
        try:
            size = int(info.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return DriveFile(file_id=str(info["id"]), name=str(info["name"]),
                         mime=str(info.get("mimeType") or ""),
                         modified=str(info.get("modifiedTime") or ""),
                         url=str(info.get("webViewLink") or ""), size=size)

    def fetch(self, meta: DriveFile, dest_path: str) -> None:
        """The file's bytes at `dest_path` — verbatim for a binary upload, Drive's own PDF
        export for a native Google file (the caller has already renamed the destination
        `.pdf` in that case and validated the format either way)."""
        args = ["drive", "download", meta.file_id, "--out", dest_path]
        if is_native(meta.mime):
            args += ["--format", "pdf"]
        self._run(*args, step=f"downloading {meta.name!r} from Drive")
        if not os.path.isfile(dest_path) or os.path.getsize(dest_path) == 0:
            raise SubmissionRejected(
                f"downloading {meta.name!r} from Drive: `{self.gog_bin}` reported success but "
                f"wrote no bytes at the output path")
