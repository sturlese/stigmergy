"""Google Drive acquisition with credentials confined to the local keychain."""

from __future__ import annotations

import datetime as dt
import io
import json
import re
from pathlib import Path

import keyring
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from stigmergy.bridge.cloud import AcquiredArtifact, BridgeError
from stigmergy.capture.provenance import without_capability
from stigmergy.capture.schema import (
    MAX_ARTIFACT_BYTES,
    MEDIA_DOCX,
    MEDIA_PPTX,
    AcquisitionProvenance,
)

SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)
KEYRING_SERVICE = "stigmergy-google-drive"
KEYRING_ACCOUNT = "oauth"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"

_URL_PATTERNS = (
    re.compile(r"^https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)"),
    re.compile(r"^https://docs\.google\.com/(?:document|presentation|spreadsheets)/d/([A-Za-z0-9_-]+)"),
    re.compile(r"^https://drive\.google\.com/open\?id=([A-Za-z0-9_-]+)"),
)


def file_id_from_url(url: str) -> str | None:
    for pattern in _URL_PATTERNS:
        match = pattern.match(url)
        if match:
            return match.group(1)
    return None


class DriveClient:
    def __init__(self, client_secrets: str, *, keyring_module=keyring, service=None) -> None:
        self.client_secrets = client_secrets
        self.keyring = keyring_module
        self._service = service

    def acquire(self, url: str) -> AcquiredArtifact:
        file_id = file_id_from_url(url)
        if not file_id:
            raise BridgeError("the Google Drive URL is not supported")
        service = self._service or build("drive", "v3", credentials=self._credentials(), cache_discovery=False)
        metadata = service.files().get(fileId=file_id, fields="id,name,mimeType,size").execute()
        mime = metadata.get("mimeType", "")
        if mime == GOOGLE_SHEET:
            raise BridgeError("Google Sheets are not supported")
        if mime == GOOGLE_DOC:
            request = service.files().export_media(fileId=file_id, mimeType=MEDIA_DOCX)
            media_type = MEDIA_DOCX
            name = _export_name(metadata.get("name"), ".docx")
        elif mime == GOOGLE_SLIDES:
            request = service.files().export_media(fileId=file_id, mimeType=MEDIA_PPTX)
            media_type = MEDIA_PPTX
            name = _export_name(metadata.get("name"), ".pptx")
        elif mime.startswith("application/vnd.google-apps."):
            raise BridgeError("this Google Drive file type is not supported")
        else:
            request = service.files().get_media(fileId=file_id)
            media_type = mime
            name = metadata.get("name") or None
        data = _download(request)
        locator = f"https://drive.google.com/file/d/{file_id}/view"
        acquired_at = dt.datetime.now(dt.UTC)
        return AcquiredArtifact(
            data=data,
            media_type=media_type,
            original_name=name,
            source_url=locator,
            locator=locator,
            acquisition=AcquisitionProvenance(
                original_url=without_capability(url),
                final_url=locator,
                drive_file_id=file_id,
                drive_media_type=mime,
                export_media_type=(
                    media_type if mime in {GOOGLE_DOC, GOOGLE_SLIDES} else None
                ),
                acquired_at=acquired_at,
            ),
        )

    def _credentials(self) -> Credentials:
        raw = self.keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        credentials = None
        if raw:
            try:
                credentials = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
            except (ValueError, TypeError, json.JSONDecodeError):
                credentials = None
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials or not credentials.valid:
            if not self.client_secrets or not Path(self.client_secrets).is_file():
                raise BridgeError("Google OAuth client secrets are not configured")
            flow = InstalledAppFlow.from_client_secrets_file(self.client_secrets, SCOPES)
            credentials = flow.run_local_server(port=0)
        try:
            self.keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, credentials.to_json())
        except Exception as error:
            raise BridgeError("Google credentials could not be stored in the OS keychain") from error
        return credentials


def _download(request) -> bytes:
    target = io.BytesIO()
    downloader = MediaIoBaseDownload(target, request, chunksize=4 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk(num_retries=1)
        if target.tell() > MAX_ARTIFACT_BYTES:
            raise BridgeError("file exceeds the 50 MiB artifact limit")
    return target.getvalue()


def _export_name(name: str | None, suffix: str) -> str | None:
    if not name:
        return None
    return name if name.lower().endswith(suffix) else name + suffix
