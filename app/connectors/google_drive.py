"""A Google Drive folder, read with a service account the deployment owner sets up. See D59.

The owner shares the folder with the service account's email address. Google Docs are exported as Word files, so they're
read like an upload. Called with httpx and a signed token, rather than Google's large client library.
"""
import time

import httpx
import jwt

from app.connectors.base import MAX_FILE_BYTES, ConnectorError, SourceFile, readable

DRIVE = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_DOC = "application/vnd.google-apps.document"
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class GoogleDriveConnector:
    def __init__(self, folder_id: str, credentials: dict, client: httpx.Client | None = None):
        self.folder_id = folder_id
        self.credentials = credentials  # a service account key, as Google gives it in JSON
        self.client = client or httpx.Client(timeout=60)
        self._token: tuple[str, float] | None = None

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        now = int(time.time())
        assertion = jwt.encode({"iss": self.credentials["client_email"], "scope": SCOPE, "aud": TOKEN_URL,
                                "iat": now, "exp": now + 3600}, self.credentials["private_key"], algorithm="RS256")
        try:
            response = self.client.post(TOKEN_URL, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ConnectorError("Google didn't accept the service account. Ask the server's owner to check it.") \
                from error
        self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return self._token[0]

    def _get(self, url: str, **params) -> httpx.Response:
        try:
            response = self.client.get(url, params=params, headers={"Authorization": f"Bearer {self._access_token()}"})
        except httpx.HTTPError as error:
            raise ConnectorError("Google Drive didn't answer. The next sync will try again.") from error
        if response.status_code == 404:
            raise ConnectorError("The Drive folder wasn't found. Is it shared with the service account?")
        if response.status_code >= 400:
            raise ConnectorError(f"Google Drive refused the request ({response.status_code}).")
        return response

    def list_files(self) -> list[SourceFile]:
        files, page = [], None
        while True:
            params = {"q": f"'{self.folder_id}' in parents and trashed = false", "pageSize": 200,
                      "fields": "nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime, size)"}
            if page:
                params["pageToken"] = page
            body = self._get(f"{DRIVE}/files", **params).json()
            for item in body.get("files", []):
                if item.get("mimeType") == GOOGLE_DOC:
                    files.append(SourceFile(item["id"], f"{item['name']}.docx", f"doc-{item['modifiedTime']}"))
                elif readable(item["name"]):
                    files.append(SourceFile(item["id"], item["name"],
                                            item.get("md5Checksum") or item.get("modifiedTime", "")))
            page = body.get("nextPageToken")
            if not page:
                return files

    def fetch(self, file: SourceFile) -> bytes:
        if file.version.startswith("doc-"):
            response = self._get(f"{DRIVE}/files/{file.key}/export", mimeType=WORD)
        else:
            response = self._get(f"{DRIVE}/files/{file.key}", alt="media")
        if len(response.content) > MAX_FILE_BYTES:
            raise ConnectorError(f"{file.name} is larger than the {MAX_FILE_BYTES // 1_000_000} MB limit.")
        return response.content
