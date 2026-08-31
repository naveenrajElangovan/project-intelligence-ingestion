"""Create only the empty operational state table; never inserts source/test data."""

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    credential = DefaultAzureCredential(
        managed_identity_client_id=(settings.state_managed_identity_client_id or None),
        exclude_interactive_browser_credential=True,
        exclude_broker_credential=True,
    )
    service = TableServiceClient(settings.state_table_endpoint, credential=credential)
    service.create_table_if_not_exists(settings.state_table_name)
    print(f"table={settings.state_table_name} ready=true")


if __name__ == "__main__":
    main()
