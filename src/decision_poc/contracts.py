from pydantic import BaseModel, ConfigDict, Field, field_validator


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    alert: str = Field(min_length=1)
    service: str = Field(min_length=1)

    @field_validator("incident_id", "alert", "service")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class DecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    diagnosis: str = Field(min_length=1)
    recommended_action: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    risks: list[str] = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)

    @field_validator("incident_id", "diagnosis", "recommended_action")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value
