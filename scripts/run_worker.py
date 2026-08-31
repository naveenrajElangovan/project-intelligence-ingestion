"""CPU-first Service Bus worker. Queue messages contain identifiers, never source bodies."""

import asyncio
from pathlib import Path

from azure.identity import DefaultAzureCredential

from app.config import get_settings
from app.dependencies import get_ingestion_service
from app.jobs import IngestionJob


async def _run() -> None:
    settings = get_settings()
    artifact_manifest = Path("/opt/model-checksums.json")
    if artifact_manifest.exists():
        from scripts.verify_model_artifacts import verify

        verify(
            artifact_manifest,
            [Path(settings.docling_artifacts_path), Path(settings.embedding_tokenizer)],
        )
    if not settings.service_bus_namespace:
        raise SystemExit("PI_INGEST_SERVICE_BUS_NAMESPACE is required.")
    try:
        from azure.servicebus.aio import ServiceBusClient
    except ImportError as error:
        raise SystemExit("Install requirements-worker.txt before starting the worker.") from error

    credential = DefaultAzureCredential(
        exclude_interactive_browser_credential=True,
        exclude_broker_credential=True,
    )
    client = ServiceBusClient(settings.service_bus_namespace, credential)
    async with client:
        receiver = client.get_queue_receiver(
            settings.service_bus_queue_name,
            max_wait_time=30,
            prefetch_count=settings.docling_max_concurrency,
        )
        async with receiver:
            active: set[asyncio.Task[None]] = set()

            async def process(message) -> None:
                try:
                    job = IngestionJob.from_json(str(message))
                    await get_ingestion_service().ingest_project(
                        job.project_id, (job.provider,), full=False
                    )
                except Exception as error:
                    if message.delivery_count >= 5:
                        await receiver.dead_letter_message(
                            message,
                            reason="INGESTION_FAILED",
                            error_description=type(error).__name__,
                        )
                    else:
                        await receiver.abandon_message(message)
                else:
                    await receiver.complete_message(message)

            async for message in receiver:
                active.add(asyncio.create_task(process(message)))
                if len(active) >= settings.docling_max_concurrency:
                    done, active = await asyncio.wait(
                        active, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
            if active:
                await asyncio.gather(*active)


if __name__ == "__main__":
    asyncio.run(_run())
