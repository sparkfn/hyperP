"""Pydantic models for the stretchy-reports feature."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --- Parameter type literal ---

ReportParamType = Literal["string", "integer", "float", "date", "boolean"]

# --- Domain models ---


class ReportParameterDef(BaseModel):
    """Schema for a single named parameter accepted by a report query."""

    name: str
    label: str
    param_type: ReportParamType = "string"
    required: bool = False
    default_value: str | None = None


class ReportSummary(BaseModel):
    report_key: str
    display_name: str
    description: str | None = None
    category: str | None = None


class ReportDetail(ReportSummary):
    cypher_query: str
    parameters: list[ReportParameterDef] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class ReportResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, str | int | float | bool | None]]
    row_count: int


# --- Request bodies ---


class CreateReportRequest(BaseModel):
    report_key: str
    display_name: str
    description: str | None = None
    category: str | None = None
    cypher_query: str
    parameters: list[ReportParameterDef] = Field(default_factory=list)


class UpdateReportRequest(BaseModel):
    display_name: str | None = None
    description: str | None = None
    category: str | None = None
    cypher_query: str | None = None
    parameters: list[ReportParameterDef] | None = None


class ExecuteReportRequest(BaseModel):
    parameters: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
    )
