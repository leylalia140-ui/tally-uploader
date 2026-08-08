import io
import logging
import os
from typing import Optional
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_FILE = "token.json"


class GoogleDriveClient:
    def __init__(self):
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        else:
            # Build credentials from individual env vars (Railway deployment)
            creds = Credentials(
                token=None,
                refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
                token_uri="https://oauth2.googleapis.com/token",
                client_id=os.environ["GOOGLE_CLIENT_ID"],
                client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
                scopes=SCOPES,
            )

        if creds.expired or not creds.valid:
            creds.refresh(Request())

        self.service = build("drive", "v3", credentials=creds)

    # ──────────────────────────────────────────
    # Folder helpers
    # ──────────────────────────────────────────

    def _find_folder(self, name: str, parent_id: str) -> Optional[str]:
        """Return folder ID if it exists inside parent_id, else None."""
        escaped = name.replace("'", "\\'")
        query = (
            f"name='{escaped}' "
            f"and mimeType='application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents "
            f"and trashed=false"
        )
        result = (
            self.service.files()
            .list(
                q=query,
                fields="files(id, name)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self, name: str, parent_id: str) -> str:
        """Create a folder inside parent_id and return its ID."""
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = (
            self.service.files()
            .create(body=metadata, fields="id", supportsAllDrives=True)
            .execute()
        )
        logger.info(f"Created folder '{name}' (id={folder['id']})")
        return folder["id"]

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Return existing folder ID or create it."""
        folder_id = self._find_folder(name, parent_id)
        if folder_id:
            return folder_id
        return self._create_folder(name, parent_id)

    def find_file(self, name: str, parent_id: str) -> Optional[str]:
        """Return file ID if a file named `name` exists inside parent_id, else None."""
        escaped = name.replace("'", "\\'")
        query = (
            f"name='{escaped}' "
            f"and '{parent_id}' in parents "
            f"and trashed=false"
        )
        result = (
            self.service.files()
            .list(
                q=query,
                fields="files(id, name)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def download_file(self, file_id: str, dest_path: str) -> None:
        """Download a Drive file to a local path."""
        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

    def resolve_folder_path(self, path_parts: list[str]) -> str:
        """
        Walk (or create) a nested folder path starting from the root Drive folder.
        path_parts = ["Models", "Sherry Hicks", "Instagram Reels", "26th March 2026"]
        Returns the ID of the deepest folder.
        """
        current_id = settings.GOOGLE_DRIVE_ROOT_FOLDER_ID
        for part in path_parts:
            current_id = self.get_or_create_folder(part, current_id)
        return current_id

    # ──────────────────────────────────────────
    # File upload (streaming, supports large files)
    # ──────────────────────────────────────────

    def upload_file(
        self,
        file_name: str,
        file_stream: io.IOBase,
        folder_id: str,
        mime_type: str = "video/mp4",
    ) -> dict:
        """
        Upload a file to folder_id using a resumable upload.
        file_stream must be a seekable BytesIO or similar object.
        Returns the created file metadata dict with 'id' and 'name'.
        """
        metadata = {"name": file_name, "parents": [folder_id]}
        media = MediaIoBaseUpload(
            file_stream,
            mimetype=mime_type,
            chunksize=10 * 1024 * 1024,  # 10 MB chunks
            resumable=True,
        )
        created = (
            self.service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        logger.info(f"Uploaded '{file_name}' → id={created['id']}")
        return created

    def make_file_public(self, file_id: str) -> str:
        """Grant anyone-with-link read access. Returns shareable URL."""
        self.service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()
        return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

    def make_folder_public(self, folder_id: str) -> str:
        """Grant anyone-with-link read access to a folder. Returns shareable URL."""
        self.service.permissions().create(
            fileId=folder_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()
        return f"https://drive.google.com/drive/folders/{folder_id}?usp=sharing"
