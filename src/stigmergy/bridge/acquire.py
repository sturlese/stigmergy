"""Local input acquisition before normalized cloud submission."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from urllib.parse import unquote, urlsplit

from stigmergy.bridge.cloud import AcquiredArtifact, BridgeError
from stigmergy.bridge.drive import DriveClient, file_id_from_url
from stigmergy.capture import artifacts, fetch, schema
from stigmergy.capture.provenance import without_capability


class Acquirer:
    def __init__(self, drive: DriveClient | None = None) -> None:
        self.drive = drive

    def text(self, value: str) -> AcquiredArtifact:
        if not value:
            raise BridgeError("text is required")
        return AcquiredArtifact(
            data=value.encode("utf-8"),
            media_type=schema.MEDIA_TEXT,
            original_name=None,
            source_url=None,
            locator=None,
            acquisition=None,
        )

    def path(self, value: str) -> AcquiredArtifact:
        path = Path(value).expanduser()
        try:
            if not path.is_file():
                raise BridgeError("path must name one regular file")
            size = path.stat().st_size
            if not 0 < size <= schema.MAX_ARTIFACT_BYTES:
                raise BridgeError("file must be between 1 byte and 50 MiB")
            data = path.read_bytes()
        except BridgeError:
            raise
        except OSError as error:
            raise BridgeError("local file could not be read") from error
        media_type = artifacts.detect_media(data, original_name=path.name)
        return AcquiredArtifact(data, media_type, path.name, None, None, None)

    def url(self, value: str) -> AcquiredArtifact:
        if file_id_from_url(value):
            if self.drive is None:
                raise BridgeError("Google Drive support is not configured")
            artifact = self.drive.acquire(value)
            detected = artifacts.detect_media(
                artifact.data,
                declared=artifact.media_type,
                original_name=artifact.original_name,
            )
            return AcquiredArtifact(
                artifact.data,
                detected,
                artifact.original_name,
                artifact.source_url,
                artifact.locator,
                artifact.acquisition,
            )
        acquired = fetch.fetch_public(value)
        name = _url_name(acquired.final_url)
        media_type = artifacts.detect_media(
            acquired.data,
            declared=acquired.response_media_type or None,
            original_name=name,
        )
        return AcquiredArtifact(
            acquired.data,
            media_type,
            name,
            acquired.final_url,
            acquired.final_url,
            schema.AcquisitionProvenance(
                original_url=without_capability(value),
                final_url=acquired.final_url,
                acquired_at=dt.datetime.now(dt.UTC),
            ),
        )


def _url_name(url: str) -> str | None:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]).strip()
    return name or None
