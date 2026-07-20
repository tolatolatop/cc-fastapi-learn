from pydantic import BaseModel


class ProviderCapabilityResponse(BaseModel):
    id: str
    display_name: str
    capabilities: list[str]


class ProviderCapabilityListResponse(BaseModel):
    items: list[ProviderCapabilityResponse]
    custom_provider_allowed: bool
