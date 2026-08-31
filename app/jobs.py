from __future__ import annotations

from dataclasses import asdict, dataclass
import asyncio
import json
from uuid import uuid4

from azure.identity import DefaultAzureCredential

from app.config import Settings


@dataclass(frozen=True, slots=True)
class IngestionJob:
    project_id: str
    provider: str
    source_id: str
    source_version: str
    trigger: str
    delivery_id: str
    correlation_id: str

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        provider: str,
        source_id: str,
        source_version: str,
        trigger: str,
        delivery_id: str,
    ) -> "IngestionJob":
        return cls(
            project_id=project_id,
            provider=provider,
            source_id=source_id,
            source_version=source_version,
            trigger=trigger,
            delivery_id=delivery_id,
            correlation_id=uuid4().hex,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "IngestionJob":
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("Invalid ingestion job payload.")
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        if set(payload) != allowed or any(not isinstance(payload[name], str) for name in allowed):
            raise ValueError("Invalid ingestion job fields.")
        return cls(**payload)


async def enqueue_job(settings: Settings, job: IngestionJob) -> None:
    if not settings.service_bus_namespace:
        raise RuntimeError("Azure Service Bus is not configured.")
    try:
        from azure.servicebus import ServiceBusMessage
        from azure.servicebus.aio import ServiceBusClient
    except ImportError as error:
        raise RuntimeError("Install the ingestion worker dependencies for Service Bus.") from error
    credential = DefaultAzureCredential(
        exclude_interactive_browser_credential=True,
        exclude_broker_credential=True,
    )
    client = ServiceBusClient(
        fully_qualified_namespace=settings.service_bus_namespace,
        credential=credential,
    )
    async with client:
        sender = client.get_queue_sender(settings.service_bus_queue_name)
        async with sender:
            await sender.send_messages(
                ServiceBusMessage(
                    job.to_json(),
                    content_type="application/json",
                    message_id=job.delivery_id,
                    correlation_id=job.correlation_id,
                )
            )
