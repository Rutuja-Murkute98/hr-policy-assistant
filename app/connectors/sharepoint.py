"""A SharePoint folder, read through Microsoft Graph with an app registration the deployment owner sets up. See D59.

The app needs the Sites.Read.All application permission, granted by a Microsoft 365 admin. Called with httpx rather than
Microsoft's client library.
"""
import time
from urllib.parse import quote

import httpx

from app.connectors.base import MAX_FILE_BYTES, ConnectorError, SourceFile, readable

GRAPH = "https://graph.microsoft.com/v1.0"


class SharePointConnector:
    def __init__(self, tenant: str, client_id: str, client_secret: str, site_id: str, drive_id: str, folder: str,
                 client: httpx.Client | None = None):
        self.tenant, self.client_id, self.client_secret = tenant, client_id, client_secret
        self.site_id, self.drive_id, self.folder = site_id, drive_id, folder.strip("/")
        self.client = client or httpx.Client(timeout=60)
        self._token: tuple[str, float] | None = None

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        try:
            response = self.client.post(
                f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": self.client_id,
                      "client_secret": self.client_secret, "scope": "https://graph.microsoft.com/.default"})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ConnectorError("Microsoft didn't accept the app registration. Ask the server's owner to check it.") \
                from error
        self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return self._token[0]

    def _get(self, url: str) -> httpx.Response:
        try:
            response = self.client.get(url, headers={"Authorization": f"Bearer {self._access_token()}"},
                                       follow_redirects=True)
        except httpx.HTTPError as error:
            raise ConnectorError("SharePoint didn't answer. The next sync will try again.") from error
        if response.status_code == 404:
            raise ConnectorError("The SharePoint folder wasn't found. Check the site, library and folder.")
        if response.status_code >= 400:
            raise ConnectorError(f"SharePoint refused the request ({response.status_code}).")
        return response

    def _drive(self) -> str:
        return f"{GRAPH}/sites/{quote(self.site_id, safe='')}/drives/{quote(self.drive_id, safe='')}"

    def list_files(self) -> list[SourceFile]:
        folder = f"/root:/{quote(self.folder)}:/children" if self.folder else "/root/children"
        url: str | None = f"{self._drive()}{folder}?$select=id,name,file,eTag,cTag"
        files = []
        while url:
            body = self._get(url).json()
            for item in body.get("value", []):
                if "file" in item and readable(item["name"]):
                    files.append(SourceFile(item["id"], item["name"], item.get("cTag") or item.get("eTag", "")))
            url = body.get("@odata.nextLink")
        return files

    def fetch(self, file: SourceFile) -> bytes:
        response = self._get(f"{self._drive()}/items/{quote(file.key, safe='')}/content")
        if len(response.content) > MAX_FILE_BYTES:
            raise ConnectorError(f"{file.name} is larger than the {MAX_FILE_BYTES // 1_000_000} MB limit.")
        return response.content
