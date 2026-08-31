import base64
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from app.chunking import is_indexable_path, is_visual_asset_path
from app.config import Settings
from app.globbing import matches_any
from app.models import ChangedFile, RepositoryFile
from app.projects import RepositoryMapping


class GitHubAppClient:
    def __init__(
        self,
        settings: Settings,
        repository: RepositoryMapping,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._http_client = http_client
        self._cached_token: str | None = None
        self._cached_token_until: datetime | None = None

    async def pull_request_files(
        self, installation_id: int, pull_request_number: int, commit_sha: str
    ) -> tuple[ChangedFile, ...]:
        client = self._http_client or httpx.AsyncClient(timeout=30.0)
        owns_client = self._http_client is None
        try:
            token = await self._cached_installation_token(client, installation_id)
            headers = _github_headers(token)
            changes: list[ChangedFile] = []
            page = 1
            while True:
                response = await client.get(
                    f"https://api.github.com/repos/{self._repository.full_name}/pulls/"
                    f"{pull_request_number}/files",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError("GitHub returned an invalid changed-files response.")
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    path = str(item.get("filename") or "")
                    status = str(item.get("status") or "")
                    if not self._path_is_allowed(path) or status not in {
                        "added",
                        "modified",
                        "removed",
                        "renamed",
                    }:
                        continue
                    previous_path = (
                        str(item["previous_filename"])
                        if isinstance(item.get("previous_filename"), str)
                        else None
                    )
                    content = None
                    content_hash = None
                    if status != "removed" and is_indexable_path(path):
                        content = await self._file_content(client, headers, path, commit_sha)
                        if content is not None:
                            content_hash = hashlib.sha256(content.encode()).hexdigest()
                    changes.append(
                        ChangedFile(
                            path=path,
                            status=status,
                            previous_path=previous_path,
                            content=content,
                            content_hash=content_hash,
                            source_url=(
                                f"https://github.com/{self._repository.full_name}/blob/"
                                f"{commit_sha}/{path}"
                            ),
                        )
                    )
                if len(payload) < 100:
                    break
                page += 1
            return tuple(changes)
        finally:
            if owns_client:
                await client.aclose()

    async def repository_files(
        self, branch: str
    ) -> tuple[str, tuple[RepositoryFile, ...]]:
        """Return the branch head and blob descriptors without downloading bodies."""
        client = self._http_client or httpx.AsyncClient(timeout=45.0)
        owns_client = self._http_client is None
        try:
            installation = await client.get(
                f"https://api.github.com/repos/{self._repository.full_name}/installation",
                headers=_github_headers(self._app_jwt()),
            )
            installation.raise_for_status()
            installation_payload = installation.json()
            installation_id = (
                installation_payload.get("id")
                if isinstance(installation_payload, dict)
                else None
            )
            if not isinstance(installation_id, int):
                raise RuntimeError("GitHub returned no App installation ID.")
            token = await self._cached_installation_token(client, installation_id)
            headers = _github_headers(token)
            branch_response = await client.get(
                f"https://api.github.com/repos/{self._repository.full_name}/branches/{branch}",
                headers=headers,
            )
            branch_response.raise_for_status()
            branch_payload = branch_response.json()
            commit = branch_payload.get("commit") if isinstance(branch_payload, dict) else None
            commit_sha = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
            commit_details = commit.get("commit") if isinstance(commit, dict) else None
            tree = commit_details.get("tree") if isinstance(commit_details, dict) else None
            tree_sha = str(tree.get("sha") or "") if isinstance(tree, dict) else ""
            if not commit_sha or not tree_sha:
                raise RuntimeError("GitHub returned an incomplete branch response.")
            tree_response = await client.get(
                f"https://api.github.com/repos/{self._repository.full_name}/git/trees/{tree_sha}",
                headers=headers,
                params={"recursive": "1"},
            )
            tree_response.raise_for_status()
            tree_payload = tree_response.json()
            if not isinstance(tree_payload, dict) or tree_payload.get("truncated") is True:
                raise RuntimeError(
                    "GitHub tree is truncated; split this repository mapping by include path."
                )
            values = tree_payload.get("tree")
            if not isinstance(values, list):
                raise RuntimeError("GitHub returned an invalid repository tree.")
            files = tuple(
                RepositoryFile(
                    path=str(item.get("path") or ""),
                    blob_sha=str(item.get("sha") or ""),
                    size=int(item.get("size") or 0),
                    visual_asset=is_visual_asset_path(str(item.get("path") or "")),
                )
                for item in values
                if isinstance(item, dict)
                and item.get("type") == "blob"
                and isinstance(item.get("size"), int)
                and int(item["size"]) <= self._settings.github_max_file_bytes
                and (
                    is_indexable_path(str(item.get("path") or ""))
                    or is_visual_asset_path(str(item.get("path") or ""))
                )
                and self._path_is_allowed(str(item.get("path") or ""))
            )
            return commit_sha, files
        finally:
            if owns_client:
                await client.aclose()

    async def blob_content(self, blob_sha: str) -> str | None:
        value = await self.blob_bytes(blob_sha)
        if value is None:
            return None
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None

    async def blob_bytes(self, blob_sha: str) -> bytes | None:
        client = self._http_client or httpx.AsyncClient(timeout=45.0)
        owns_client = self._http_client is None
        try:
            installation = await client.get(
                f"https://api.github.com/repos/{self._repository.full_name}/installation",
                headers=_github_headers(self._app_jwt()),
            )
            installation.raise_for_status()
            payload = installation.json()
            installation_id = payload.get("id") if isinstance(payload, dict) else None
            if not isinstance(installation_id, int):
                raise RuntimeError("GitHub returned no App installation ID.")
            token = await self._cached_installation_token(client, installation_id)
            response = await client.get(
                f"https://api.github.com/repos/{self._repository.full_name}/git/blobs/{blob_sha}",
                headers=_github_headers(token),
            )
            response.raise_for_status()
            value = response.json()
            encoded = value.get("content") if isinstance(value, dict) else None
            if not isinstance(encoded, str) or value.get("encoding") != "base64":
                return None
            try:
                return base64.b64decode(encoded, validate=False)
            except ValueError:
                return None
        finally:
            if owns_client:
                await client.aclose()

    def _path_is_allowed(self, path: str) -> bool:
        normalized = path.lower().replace("\\", "/")
        name = normalized.rsplit("/", 1)[-1]
        if (
            name.startswith(".env")
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
            or any(part in {"node_modules", "vendor", ".gradle", "build", "dist", "secrets"} for part in normalized.split("/"))
        ):
            return False
        # Same matcher as the exclusions below, so an include and an exclude
        # written in the same syntax behave the same way.
        if self._repository.include_paths and not matches_any(
            path, self._repository.include_paths
        ):
            return False
        # fnmatch has no path semantics, so "**/*.docx" missed a .docx at the
        # repository root and "*.kt" matched every .kt in the tree. An exclusion
        # that silently fails to exclude is worse than none.
        return not matches_any(path, self._repository.exclude_paths)

    async def _installation_token(self, client: httpx.AsyncClient, installation_id: int) -> str:
        response = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers=_github_headers(self._app_jwt()),
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("GitHub returned no installation token.")
        return token

    async def _cached_installation_token(
        self, client: httpx.AsyncClient, installation_id: int
    ) -> str:
        now = datetime.now(UTC)
        if (
            self._cached_token
            and self._cached_token_until
            and self._cached_token_until > now
        ):
            return self._cached_token
        self._cached_token = await self._installation_token(client, installation_id)
        self._cached_token_until = now + timedelta(minutes=50)
        return self._cached_token

    async def _file_content(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        path: str,
        commit_sha: str,
    ) -> str | None:
        response = await client.get(
            f"https://api.github.com/repos/{self._repository.full_name}/contents/{path}",
            headers=headers,
            params={"ref": commit_sha},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        size = payload.get("size")
        encoded = payload.get("content")
        if not isinstance(size, int) or size > self._settings.github_max_file_bytes:
            return None
        if payload.get("encoding") != "base64" or not isinstance(encoded, str):
            return None
        try:
            return base64.b64decode(encoded, validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    def _app_jwt(self) -> str:
        now = datetime.now(UTC)
        try:
            private_key = base64.b64decode(self._settings.github_private_key_base64).decode()
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("The GitHub App private key is invalid.") from error
        return jwt.encode(
            {
                "iat": int((now - timedelta(seconds=30)).timestamp()),
                "exp": int((now + timedelta(minutes=9)).timestamp()),
                "iss": self._settings.github_app_id,
            },
            private_key,
            algorithm="RS256",
        )


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
