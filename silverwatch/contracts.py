from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Region = Literal["urban", "suburban", "rural"]
ServiceKind = Literal["home_care", "meal", "medical", "day_center", "transport", "financial_guidance"]
IssueKind = Literal["misleading_claim", "payment_dispute", "service_failure", "suspected_fraud", "access_barrier"]
Finding = Literal["pending", "substantiated", "not_substantiated", "inconclusive"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def aware_time(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("时间必须包含时区")
            return value.astimezone(timezone.utc).replace(microsecond=0)
        return value


class Provider(Strict):
    provider_id: Key
    display_name: Text
    region: Region
    district: Text
    service_kind: ServiceKind
    capacity_per_month: Annotated[int, Field(ge=0, le=10_000_000)]
    active_from: datetime
    active_until: datetime | None = None
    status: Literal["active", "suspended", "closed"] = "active"
    updated_at: datetime

    @model_validator(mode="after")
    def period(self):
        if self.active_until and self.active_until <= self.active_from:
            raise ValueError("供给结束时间必须晚于生效时间")
        return self


class TouchBatch(Strict):
    batch_id: Key
    campaign_id: Key
    provider_id: Key
    region: Region
    district: Text
    service_kind: ServiceKind
    channel: Literal["in_person", "phone", "web", "community"]
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"]
    period_start: datetime
    period_end: datetime
    exposed_people: Annotated[int, Field(ge=0, le=100_000_000)]
    completed_services: Annotated[int, Field(ge=0, le=100_000_000)]
    report_count: Annotated[int, Field(ge=0, le=10_000_000)]
    nonresponse_people: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    coverage_complete: bool = True
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    source_ref: Text
    observed_at: datetime

    @model_validator(mode="after")
    def bounds(self):
        if self.period_end <= self.period_start:
            raise ValueError("触达统计期必须为正区间")
        if self.exposed_people + self.nonresponse_people < self.report_count:
            raise ValueError("触达及未回应总人数不能小于线索数")
        return self


class Case(Strict):
    case_id: Key
    provider_id: Key
    batch_id: Key
    kind: IssueKind
    severity: Literal["low", "medium", "high", "critical"]
    reported_at: datetime
    allegation: Text
    source_ref: Text
    affected_people: Annotated[int, Field(ge=0, le=1_000_000)]
    status: Literal["open", "investigating", "closed"] = "open"


class NeedBatch(Strict):
    estimate_id: Key
    region: Region
    district: Text
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"]
    service_kind: ServiceKind
    estimated_people: Annotated[int, Field(ge=0, le=100_000_000)]
    period_start: datetime
    period_end: datetime
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    source_ref: Text

    @model_validator(mode="after")
    def estimate_period(self):
        if self.period_end <= self.period_start:
            raise ValueError("需求估计周期必须为正区间")
        return self


class CaseEvidence(Strict):
    evidence_id: Key
    case_id: Key
    evidence_type: Literal["contract", "recording", "receipt", "interview", "inspection", "other"]
    source_ref: Text
    content_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    observed_at: datetime
    verified: bool = False


class ReviewDecision(Strict):
    decision_id: Key
    case_id: Key
    reviewer: Key
    role: Literal["investigator", "compliance"]
    finding: Finding
    decided_at: datetime
    rationale: Text
    evidence_refs: list[Text] = Field(min_length=1, max_length=30)


class Remediation(Strict):
    action_id: Key
    case_id: Key
    provider_id: Key
    owner: Key
    due_at: datetime
    action: Text
    evidence_ref: Text | None = None
    verified_by: Key | None = None
    verified_at: datetime | None = None
    status: Literal["planned", "in_progress", "submitted", "verified", "rejected"] = "planned"

    @model_validator(mode="after")
    def verification(self):
        if self.status == "verified" and (not self.verified_by or not self.verified_at or not self.evidence_ref):
            raise ValueError("已核验整改需要独立人员、时间和证据")
        if self.verified_by == self.owner:
            raise ValueError("整改责任人不能自行核验")
        return self


class CaseTransfer(Strict):
    transfer_id: Key
    case_id: Key
    from_region: Region
    to_region: Region
    reason: Text
    requested_by: Key
    requested_at: datetime
    status: Literal["requested", "accepted", "rejected"] = "requested"
    decided_by: Key | None = None
    decided_at: datetime | None = None
    decision_note: Text | None = None
    supplement_refs: list[Text] = Field(default_factory=list, max_length=30)
    supersedes: Key | None = None

    @model_validator(mode="after")
    def lifecycle(self):
        if self.from_region == self.to_region:
            raise ValueError("转交目标地区必须与当前责任地区不同")
        if self.status == "requested":
            if self.decided_by or self.decided_at or self.decision_note:
                raise ValueError("等待接收的转交不能包含接收结论")
        else:
            if not (self.decided_by and self.decided_at and self.decision_note):
                raise ValueError("接收或拒收必须记录经办人、决定时间与说明")
            if self.decided_at < self.requested_at:
                raise ValueError("接收决定时间不能早于转交发起时间")
            if self.decided_by == self.requested_by:
                raise ValueError("转交发起人与接收决定人必须不同")
        if self.supersedes and not self.supplement_refs:
            raise ValueError("补充资料后再次转交必须引用补充材料")
        return self


class TransferResolution(Strict):
    decision: Literal["accepted", "rejected"]
    decided_by: Key
    decided_at: datetime
    note: Text


class AnalysisPolicy(Strict):
    policy_id: Key
    minimum_exposure: Annotated[int, Field(ge=1, le=1_000_000_000)] = 100
    minimum_supply_coverage_bp: Annotated[int, Field(ge=0, le=10_000)] = 8_000
    max_substantiated_rate_bp: Annotated[int, Field(ge=0, le=10_000)] = 50
    severe_open_cases_block: bool = True
    require_verified_remediation: bool = True
    minimum_touch_coverage_bp: Annotated[int, Field(ge=0, le=10_000)] = 8_000


class AnalysisRequest(Strict):
    analysis_id: Key
    region: Region
    service_kind: ServiceKind | None = None
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"] | None = None
    policy: AnalysisPolicy
    through_cut: Annotated[int, Field(ge=0, le=2**63 - 1)]
    as_of: datetime


class Approval(Strict):
    approval_id: Key
    analysis_id: Key
    reviewer: Key
    role: Literal["service_planning", "consumer_protection"]
    approved: bool
    analysis_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    rationale: Text


class PublishRequest(Strict):
    analysis_id: Key
    expected_revision: Annotated[int, Field(ge=0, le=1_000_000)]
    requested_at: datetime
