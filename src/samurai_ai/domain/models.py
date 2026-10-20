from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MODEL_ID = re.compile(
    r"([a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)/([A-Za-z0-9._:-]{1,128})@v([1-9][0-9]*)\Z"
)
_PROVIDER_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")


class ModelError(ValueError):
    pass


class ModelNotFoundError(LookupError):
    pass


class CapabilityError(ModelError):
    def __init__(self, model_id: str, missing: tuple[str, ...]) -> None:
        self.model_id = model_id
        self.missing = missing
        super().__init__(
            f"model {model_id} does not support required capabilities: "
            f"{', '.join(missing)}"
        )


class ProviderUnavailableError(ModelError):
    pass


class ModelCapability(StrEnum):
    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured-output"
    STREAMING = "streaming"
    VISION = "vision"


class ApiFamily(StrEnum):
    OPENAI_COMPATIBLE = "openai-compatible"
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


class ProviderDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    api_family: ApiFamily
    base_url: str
    display_name: str = ""
    secret_name: str | None = None
    default_headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    local: bool = False

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _PROVIDER_ID.fullmatch(value):
            raise ModelError(f"invalid provider id: {value!r}")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ModelError(f"provider base_url must be http(s): {value!r}")
        return value.rstrip("/")


class ModelDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    provider_id: str
    display_name: str = ""
    api_family: ApiFamily = ApiFamily.OPENAI_COMPATIBLE
    context_limit: int = Field(default=8192, ge=1)
    max_output_tokens: int = Field(default=4096, ge=1)
    capabilities: frozenset[ModelCapability] = Field(default_factory=frozenset)
    cost_input_per_m: float = Field(default=0.0, ge=0.0)
    cost_output_per_m: float = Field(default=0.0, ge=0.0)
    local: bool = False

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _MODEL_ID.fullmatch(value):
            raise ModelError(
                f"model id must look like <provider>/<model>@v<N>: {value!r}"
            )
        return value

    @property
    def model_name(self) -> str:
        return self.id.split("/", 1)[1].rsplit("@", 1)[0]

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round(
            prompt_tokens * self.cost_input_per_m / 1_000_000
            + completion_tokens * self.cost_output_per_m / 1_000_000,
            6,
        )


def parse_model_id(model_id: str) -> tuple[str, str]:
    match = _MODEL_ID.fullmatch(model_id)
    if match is None:
        raise ModelError(f"invalid model id {model_id!r}; expected <provider>/<model>@v<N>")
    return match.group(1), match.group(2)
