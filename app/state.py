import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from typing import Protocol

from azure.core import MatchConditions
from azure.core.credentials import TokenCredential
from azure.data.tables import TableClient, UpdateMode
from azure.identity import DefaultAzureCredential

from app.config import Settings
from app.models import SourceDocument


@dataclass(frozen=True, slots=True)
class SourceManifest:
    project_id: str
    provider: str
    scope: str
    source_id: str
    source_type: str
    title: str
    source_url: str
    version: str
    content_hash: str
    chunk_count: int
    last_seen_run: str
    deleted: bool = False
    parser_version: str = "legacy"
    chunker_version: str = "legacy"
    embedding_profile: str = "legacy"
    schema_version: str = "1"


class ManifestStore(Protocol):
    async def acquire_scope_lease(
        self, project_id: str, provider: str, scope: str, owner: str, ttl_seconds: int
    ) -> bool: ...

    async def release_scope_lease(
        self, project_id: str, provider: str, scope: str, owner: str
    ) -> None: ...

    async def get_manifest(
        self, project_id: str, provider: str, scope: str, source_id: str
    ) -> SourceManifest | None: ...

    async def save_manifest(
        self,
        document: SourceDocument,
        scope: str,
        chunk_count: int,
        scan_id: str,
        *,
        parser_version: str = "legacy",
        chunker_version: str = "legacy",
        embedding_profile: str = "legacy",
        schema_version: str = "1",
    ) -> None: ...

    async def mark_deleted(self, manifest: SourceManifest, scan_id: str) -> None: ...

    async def touch_manifest(self, manifest: SourceManifest, scan_id: str) -> None: ...

    async def record_quarantine(
        self,
        document: SourceDocument,
        scope: str,
        scan_id: str,
        reason_code: str,
    ) -> None: ...

    async def get_cursor(self, project_id: str, provider: str, scope: str) -> datetime | None: ...

    async def save_cursor(
        self, project_id: str, provider: str, scope: str, value: datetime
    ) -> None: ...

    async def stale_page(
        self,
        project_id: str,
        provider: str,
        scope: str,
        scan_id: str,
        continuation_token: str | None,
        page_size: int = 1000,
    ) -> tuple[tuple[SourceManifest, ...], str | None]: ...


class AzureTableManifestStore:
    """Operational ingestion state only; source content never enters this table."""

    def __init__(
        self,
        endpoint: str,
        table_name: str,
        credential: TokenCredential,
    ) -> None:
        if not endpoint:
            raise RuntimeError("PI_INGEST_STATE_TABLE_ENDPOINT is required.")
        self._table = TableClient(endpoint, table_name, credential=credential)

    @classmethod
    def from_settings(cls, settings: Settings) -> "AzureTableManifestStore":
        credential = DefaultAzureCredential(
            managed_identity_client_id=(settings.state_managed_identity_client_id or None),
            exclude_interactive_browser_credential=True,
            exclude_broker_credential=True,
        )
        return cls(settings.state_table_endpoint, settings.state_table_name, credential)

    async def get_manifest(
        self, project_id: str, provider: str, scope: str, source_id: str
    ) -> SourceManifest | None:
        try:
            entity = await asyncio.to_thread(
                self._table.get_entity,
                _partition(project_id, provider, scope),
                _row(source_id),
            )
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return None
            raise
        return _manifest(entity)

    async def acquire_scope_lease(
        self, project_id: str, provider: str, scope: str, owner: str, ttl_seconds: int
    ) -> bool:
        partition = _partition(project_id, provider, scope)
        now = datetime.now(UTC)
        entity = {
            "PartitionKey": partition,
            "RowKey": "__lease__",
            "owner": owner,
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        }
        try:
            await asyncio.to_thread(self._table.create_entity, entity)
            return True
        except Exception as error:
            if getattr(error, "status_code", None) != 409:
                raise
        current = await asyncio.to_thread(self._table.get_entity, partition, "__lease__")
        expires_at = _datetime(current.get("expires_at"))
        if expires_at is not None and expires_at > now:
            return False
        entity["etag"] = current.metadata.get("etag")
        try:
            await asyncio.to_thread(
                self._table.update_entity,
                entity,
                mode=UpdateMode.REPLACE,
                etag=current.metadata.get("etag"),
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except Exception as error:
            if getattr(error, "status_code", None) in {409, 412}:
                return False
            raise

    async def release_scope_lease(
        self, project_id: str, provider: str, scope: str, owner: str
    ) -> None:
        partition = _partition(project_id, provider, scope)
        try:
            current = await asyncio.to_thread(self._table.get_entity, partition, "__lease__")
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return
            raise
        if str(current.get("owner") or "") != owner:
            return
        try:
            await asyncio.to_thread(
                self._table.delete_entity,
                partition,
                "__lease__",
                etag=current.metadata.get("etag"),
                match_condition=MatchConditions.IfNotModified,
            )
        except Exception as error:
            if getattr(error, "status_code", None) not in {404, 412}:
                raise

    async def save_manifest(
        self,
        document: SourceDocument,
        scope: str,
        chunk_count: int,
        scan_id: str,
        *,
        parser_version: str = "legacy",
        chunker_version: str = "legacy",
        embedding_profile: str = "legacy",
        schema_version: str = "1",
    ) -> None:
        entity = {
            "PartitionKey": _partition(document.project_id, document.provider, scope),
            "RowKey": _row(document.source_id),
            "project_id": document.project_id,
            "provider": document.provider,
            "scope": scope,
            "source_id": document.source_id,
            "source_type": document.source_type,
            "title": document.title[:1000],
            "source_url": document.source_url[:1500],
            "version": document.version[:500],
            "content_hash": document.content_hash,
            "chunk_count": chunk_count,
            "last_seen_run": scan_id,
            "deleted": document.deleted,
            "parser_version": parser_version,
            "chunker_version": chunker_version,
            "embedding_profile": embedding_profile,
            "schema_version": schema_version,
            "indexed_at": datetime.now(UTC).isoformat(),
        }
        await asyncio.to_thread(self._table.upsert_entity, entity, mode=UpdateMode.REPLACE)

    async def mark_deleted(self, manifest: SourceManifest, scan_id: str) -> None:
        document = SourceDocument(
            project_id=manifest.project_id,
            provider=manifest.provider,
            source_id=manifest.source_id,
            source_type=manifest.source_type,
            title=manifest.title,
            reference=manifest.source_id,
            source_url=manifest.source_url,
            version=manifest.version,
            content="",
            updated_at=None,
            deleted=True,
        )
        await self.save_manifest(
            document,
            manifest.scope,
            0,
            scan_id,
            parser_version=manifest.parser_version,
            chunker_version=manifest.chunker_version,
            embedding_profile=manifest.embedding_profile,
            schema_version=manifest.schema_version,
        )

    async def touch_manifest(self, manifest: SourceManifest, scan_id: str) -> None:
        entity = {
            "PartitionKey": _partition(
                manifest.project_id, manifest.provider, manifest.scope
            ),
            "RowKey": _row(manifest.source_id),
            "project_id": manifest.project_id,
            "provider": manifest.provider,
            "scope": manifest.scope,
            "source_id": manifest.source_id,
            "source_type": manifest.source_type,
            "title": manifest.title,
            "source_url": manifest.source_url,
            "version": manifest.version,
            "content_hash": manifest.content_hash,
            "chunk_count": manifest.chunk_count,
            "last_seen_run": scan_id,
            "deleted": manifest.deleted,
            "parser_version": manifest.parser_version,
            "chunker_version": manifest.chunker_version,
            "embedding_profile": manifest.embedding_profile,
            "schema_version": manifest.schema_version,
        }
        await asyncio.to_thread(self._table.upsert_entity, entity, mode=UpdateMode.REPLACE)

    async def record_quarantine(
        self,
        document: SourceDocument,
        scope: str,
        scan_id: str,
        reason_code: str,
    ) -> None:
        entity = {
            "PartitionKey": _partition(document.project_id, document.provider, scope),
            "RowKey": "__quarantine__" + _row(document.source_id),
            "project_id": document.project_id,
            "provider": document.provider,
            "scope": scope,
            "source_id": document.source_id,
            "source_version": document.version[:500],
            "scan_id": scan_id,
            "reason_code": reason_code[:100],
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        await asyncio.to_thread(self._table.upsert_entity, entity, mode=UpdateMode.REPLACE)

    async def get_cursor(self, project_id: str, provider: str, scope: str) -> datetime | None:
        try:
            entity = await asyncio.to_thread(
                self._table.get_entity,
                _partition(project_id, provider, scope),
                "__cursor__",
            )
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return None
            raise
        value = entity.get("cursor")
        return _datetime(value)

    async def save_cursor(
        self, project_id: str, provider: str, scope: str, value: datetime
    ) -> None:
        await asyncio.to_thread(
            self._table.upsert_entity,
            {
                "PartitionKey": _partition(project_id, provider, scope),
                "RowKey": "__cursor__",
                "cursor": value.astimezone(UTC).isoformat(),
            },
            mode=UpdateMode.REPLACE,
        )

    async def stale_page(
        self,
        project_id: str,
        provider: str,
        scope: str,
        scan_id: str,
        continuation_token: str | None,
        page_size: int = 1000,
    ) -> tuple[tuple[SourceManifest, ...], str | None]:
        partition = _partition(project_id, provider, scope)

        def read_page():
            pages = self._table.query_entities(
                f"PartitionKey eq '{partition}' and RowKey ne '__cursor__'",
                results_per_page=page_size,
            ).by_page(continuation_token=continuation_token)
            try:
                values = list(next(pages))
            except StopIteration:
                return [], None
            return values, pages.continuation_token

        entities, token = await asyncio.to_thread(read_page)
        manifests = tuple(
            manifest
            for entity in entities
            if not str(entity.get("RowKey") or "").startswith("__")
            if (manifest := _manifest(entity)).last_seen_run != scan_id
            and not manifest.deleted
        )
        return manifests, token


    async def purge_scope(self, project_id: str, provider: str, scope: str) -> int:
        """Delete every row for one provider scope, cursor and quarantine included.

        stale_page() deliberately hides `__` rows and already-deleted manifests,
        because reconciliation must not resurrect them. A purge needs the
        opposite: the cursor is exactly what has to go, or the next incremental
        run would resume from a timestamp for state that no longer exists.
        """

        partition = _partition(project_id, provider, scope)

        def read_keys() -> list[str]:
            return [
                str(entity["RowKey"])
                for entity in self._table.query_entities(
                    f"PartitionKey eq '{partition}'", select=["RowKey"]
                )
            ]

        keys = await asyncio.to_thread(read_keys)
        for row_key in keys:
            await asyncio.to_thread(self._table.delete_entity, partition, row_key)
        return len(keys)


    async def scan_scopes(self, project_id: str) -> dict[str, set[str]]:
        """Return the provider scopes this project has written manifests under.

        A full table scan, because the partition key is a hash of
        project|provider|scope and the scope is precisely the unknown. Only used
        by recovery tooling, where the alternative is having no copy of the
        mapping at all.
        """

        def scan() -> dict[str, set[str]]:
            found: dict[str, set[str]] = {}
            for entity in self._table.list_entities(
                select=["project_id", "provider", "scope"]
            ):
                if str(entity.get("project_id") or "") != project_id:
                    continue
                provider = str(entity.get("provider") or "").upper()
                scope = str(entity.get("scope") or "")
                if provider and scope:
                    found.setdefault(provider, set()).add(scope)
            return found

        return await asyncio.to_thread(scan)


def _partition(project_id: str, provider: str, scope: str) -> str:
    identity = f"{project_id}|{provider.upper()}|{scope}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _row(source_id: str) -> str:
    return hashlib.sha256(source_id.encode()).hexdigest()


def _manifest(entity: dict[str, object]) -> SourceManifest:
    return SourceManifest(
        project_id=str(entity.get("project_id") or ""),
        provider=str(entity.get("provider") or ""),
        scope=str(entity.get("scope") or ""),
        source_id=str(entity.get("source_id") or ""),
        source_type=str(entity.get("source_type") or ""),
        title=str(entity.get("title") or ""),
        source_url=str(entity.get("source_url") or ""),
        version=str(entity.get("version") or ""),
        content_hash=str(entity.get("content_hash") or ""),
        chunk_count=int(entity.get("chunk_count") or 0),
        last_seen_run=str(entity.get("last_seen_run") or ""),
        deleted=bool(entity.get("deleted", False)),
        parser_version=str(entity.get("parser_version") or "legacy"),
        chunker_version=str(entity.get("chunker_version") or "legacy"),
        embedding_profile=str(entity.get("embedding_profile") or "legacy"),
        schema_version=str(entity.get("schema_version") or "1"),
    )


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None
