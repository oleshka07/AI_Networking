"""Tasks API schemas."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import TaskKind, TaskStatus


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    status: TaskStatus = TaskStatus.todo
    kind: TaskKind = TaskKind.task
    project: str | None = None
    due_date: date | None = None
    duration_min: int | None = Field(default=None, ge=1)
    tags: str | None = None
    notes: str | None = None
    parent_id: int | None = None
    goal_id: int | None = None
    item_type: str | None = None
    counterpart: str | None = None
    recur: str | None = None
    blocks_work: bool = False
    external_url: str | None = Field(default=None, max_length=500)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    status: TaskStatus | None = None
    kind: TaskKind | None = None
    project: str | None = None
    due_date: date | None = None
    duration_min: int | None = Field(default=None, ge=1)
    tags: str | None = None
    notes: str | None = None
    parent_id: int | None = None
    goal_id: int | None = None
    item_type: str | None = None
    counterpart: str | None = None
    recur: str | None = None
    blocks_work: bool | None = None
    # «⏰ Завтра» на сайті; null — зняти відкладення.
    snoozed_until: datetime | None = None
    external_url: str | None = Field(default=None, max_length=500)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    uid: str
    title: str
    status: TaskStatus
    kind: TaskKind
    project: str | None
    project_id: int | None
    item_type: str | None
    counterpart: str | None
    recur: str | None
    due_date: date | None
    duration_min: int | None
    tags: str | None
    notes: str | None
    parent_id: int | None
    goal_id: int | None
    source: str | None = "manual"
    external_id: str | None = None
    external_url: str | None = None
    linked_ref: str | None = None
    blocks_work: bool | None = False
    snoozed_until: datetime | None = None
    done_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime
    deleted_at: datetime | None


class TaskSyncIn(BaseModel):
    """Одна задача з ПК. uid порожній → задача нова, сервер видасть свій."""

    uid: str | None = None
    title: str
    status: TaskStatus = TaskStatus.todo
    kind: TaskKind = TaskKind.task
    project: str | None = None
    due_date: date | None = None
    duration_min: int | None = None
    tags: str | None = None
    parent_uid: str | None = None   # підзадача: uid батьківської
    item_type: str | None = None
    counterpart: str | None = None
    recur: str | None = None
    goal_id: int | None = None


class SyncRequest(BaseModel):
    tasks: list[TaskSyncIn] = Field(default_factory=list)
    updated_at: datetime | None = None  # час правки focus.md (для «хто новіший»)
    authoritative: bool = False         # так → відсутні uid вважаємо видаленими


class SyncResponse(BaseModel):
    tasks: list[TaskOut]
    applied: int
    deleted: int


_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class SyncStatusOut(BaseModel):
    """Панель «Синк з чек-листом»: коли Claude звертався і що з цього вийшло."""

    last_at: datetime | None
    last_tool: str | None
    error: str | None
    error_at: datetime | None
    open: int
    closed: int


class TaskSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    enabled: bool
    plan_enabled: bool
    blockers_enabled: bool
    blocker_every_hours: int
    blocker_from_hour: int
    blocker_to_hour: int
    deadlines_enabled: bool
    prep_enabled: bool
    prep_hour: int
    quiet_from: str
    quiet_to: str
    daily_cap: int
    tg_suggest_mode: str
    sync: SyncStatusOut | None = None


class TaskSettingsUpdate(BaseModel):
    enabled: bool | None = None
    plan_enabled: bool | None = None
    blockers_enabled: bool | None = None
    blocker_every_hours: int | None = Field(default=None, ge=1, le=72)
    blocker_from_hour: int | None = Field(default=None, ge=0, le=23)
    blocker_to_hour: int | None = Field(default=None, ge=1, le=24)
    deadlines_enabled: bool | None = None
    prep_enabled: bool | None = None
    prep_hour: int | None = Field(default=None, ge=0, le=23)
    quiet_from: str | None = Field(default=None, pattern=_HHMM)
    quiet_to: str | None = Field(default=None, pattern=_HHMM)
    daily_cap: int | None = Field(default=None, ge=0, le=50)
    tg_suggest_mode: str | None = Field(default=None, pattern=r"^(suggest|auto|off)$")
