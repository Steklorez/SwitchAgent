"""Pydantic request models for mutating endpoints only. GET responses are
plain dicts built by services.py -- FastAPI serializes those directly, and
a response schema would just duplicate services.py's dict shape for no
real benefit at this project's size (see docs/WEB-UI.md).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class LibraryDeleteRequest(BaseModel):
    library_item_ids: list[int] = Field(min_length=1, max_length=2000)
    # "recycle" (the Recycle Bin) or "permanent" -- always named by the caller.
    mode: Optional[str] = None


class CreateJobsRequest(BaseModel):
    library_item_ids: list[int] = Field(min_length=1)
    target_device_id: str = Field(min_length=1)
    # AMIIBO items only: {library item id: [amiibo or folder paths under
    # emuiibo/amiibo/]} -- install just those of the collection. An item
    # not named here installs whole.
    amiibo_selection: Optional[dict[str, list[str]]] = None


class RenameDeviceRequest(BaseModel):
    friendly_name: Optional[str] = None


class ConflictPolicyRequest(BaseModel):
    policy: Literal["skip", "override"]


class ScanTriggerResponse(BaseModel):
    started: bool


class HistoryVerificationRequest(BaseModel):
    """UI-003: the user's own console-result confirmation for a
    DONE_UNVERIFIED install_history row. `outcome: null` (or omitting the
    field) clears a previous confirmation back to unconfirmed -- both mean
    the same thing here, there is no separate "leave unchanged" case in
    this API."""
    outcome: Optional[Literal["SUCCESS", "FAILED"]] = None


class DeviceStorageMappingRequest(BaseModel):
    """UI-007 manual override. Further validated in services.py
    (logical_name restricted to SD_CARD/SD_INSTALL, raw_storage_name
    restricted to a storage this device is currently known to actually
    report) -- this schema only enforces "both fields are non-empty
    strings", not the business rules."""
    raw_storage_name: str = Field(min_length=1)
    logical_name: str = Field(min_length=1)


class DeviceStorageMappingClearRequest(BaseModel):
    raw_storage_name: str = Field(min_length=1)


class LibraryDirRequest(BaseModel):
    """W3-002: raw user text-input path, further validated in services.py
    (exists / is a directory / readable / resolved to a canonical path).

    `path` is deliberately NOT constrained to a non-empty string here.
    `paths: []` is a meaningful request -- "remove my last Library folder"
    -- and it has no sensible primary path to carry alongside it; rejecting
    it at the schema would produce FastAPI's structured 422 rather than a
    sentence anyone can read. services.py still refuses an empty path when
    `paths` is absent, with its own "a folder path is required"."""
    path: str = ""
    paths: list[str] | None = None


class PreferencesRequest(BaseModel):
    auto_scan: bool
    scan_interval: int = Field(ge=10, le=3600)
    covers: bool


class AmiiboDeviceRequest(BaseModel):
    device: str = Field(min_length=1)  # the console's fingerprint, never its raw id


class BetaRequest(BaseModel):
    enabled: bool


class AddonInstallRequest(BaseModel):
    # The console to install it on (fingerprint) and the catalog entry's id.
    device: str
    addon: str


class EmuiiboDownloadRequest(BaseModel):
    # The console to install it on (fingerprint); None only downloads it
    # into the Library.
    device: Optional[str] = None


class AmiiboRemoveRequest(BaseModel):
    device: str = Field(min_length=1)
    # amiibo or folders, relative to emuiibo/amiibo/ on the SD card
    paths: list[str] = Field(min_length=1)
    # Must be true when any of them holds game save data (areas/).
    confirm_save_data: bool = False


class RemoveFromQueueRequest(BaseModel):
    """One game card on the Queue page: its jobs and its preparation items."""
    job_ids: list[int] = Field(default_factory=list)
    library_item_ids: list[int] = Field(default_factory=list)
