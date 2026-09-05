from fastapi import APIRouter, Depends

from cc_fastapi.api.dependencies import require_token
from cc_fastapi.core.webhook_providers import webhook_provider_registry
from cc_fastapi.schemas.providers import (
    ProviderCapabilityListResponse,
    ProviderCapabilityResponse,
)


router = APIRouter(
    prefix="/v1/providers",
    tags=["providers"],
    dependencies=[Depends(require_token)],
)


@router.get("", response_model=ProviderCapabilityListResponse)
def list_provider_capabilities() -> ProviderCapabilityListResponse:
    return ProviderCapabilityListResponse(
        items=[
            ProviderCapabilityResponse(
                id=definition.id,
                display_name=definition.display_name,
                capabilities=list(definition.capabilities),
            )
            for definition in webhook_provider_registry.list()
        ],
        custom_provider_allowed=True,
    )
