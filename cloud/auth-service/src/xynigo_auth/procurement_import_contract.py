"""Validated request contracts for cloud procurement collaboration imports."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcurementImportParseBody(StrictBody):
    filename: str = Field(min_length=1, max_length=255)
    contentBase64: str = Field(min_length=1, max_length=30_000_000)
    allowPartial: bool = Field(default=False, strict=True)


class Xyp2ParseBody(StrictBody):
    remark: str = Field(strict=True, min_length=1, max_length=20_000)


class ProcurementImportTargetInspectBody(StrictBody):
    planId: str = Field(min_length=1, max_length=64)
    spreadsheetUrl: str = Field(min_length=1, max_length=1024)


class ProcurementImportTargetValidateBody(ProcurementImportTargetInspectBody):
    sheetId: str = Field(min_length=1, max_length=128)


class ProcurementImportSyncBody(StrictBody):
    planId: str = Field(min_length=1, max_length=64)
    confirmWrite: bool
    fillOrderBackground: bool = Field(default=True, strict=True)


class ProcurementImportPreferenceBody(StrictBody):
    action: Literal['color', 'remove']
    spreadsheetUrl: str = Field(min_length=1, max_length=1024)
    sheetId: str = Field(min_length=1, max_length=128)
    fillOrderBackground: bool = Field(default=True, strict=True)
