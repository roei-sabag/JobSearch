"""
api/schemas.py
--------------
Pydantic request/response schemas for the Jobs API.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator



class JobIngestRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    company_name: Optional[str] = Field(None, max_length=255)
    raw_description: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1, max_length=64, description="e.g. 'manual_whatsapp', 'manual_discord'")
    source_url: Optional[str] = Field(None, max_length=1024)

    @field_validator("title", "raw_description", "source")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


class JobIngestResponse(BaseModel):
    job_id: int
    title: str
    company_name: Optional[str]
    source: str
    status: str
    content_hash: str
    is_duplicate: bool
    created_at: datetime


class CVTailoredOut(BaseModel):
    id: int
    pdf_path: str
    created_at: datetime

    model_config = {"from_attributes": True}


class JobOut(BaseModel):
    id: int
    title: str
    company_name: Optional[str]
    raw_description: str
    source: str
    source_url: Optional[str]
    content_hash: str
    status: str
    created_at: datetime
    updated_at: datetime
    tailored_versions: List[CVTailoredOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class TailorJobResponse(BaseModel):
    status: str
    message: str
    job_id: int


class JobIngestRawRequest(BaseModel):
    raw_text: str = Field(..., min_length=1, description="Raw, unformatted job posting text")

    @field_validator("raw_text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


class JobIngestRawResponse(BaseModel):
    job_id: int
    title: str
    company_name: Optional[str]
    status: str
    content_hash: str
    is_duplicate: bool
    extraction_mode: str
    created_at: datetime


class CourseOption(BaseModel):
    name: str
    grade: int
    tags: List[str] = Field(default_factory=list)
    suggested: bool
    match_percentage: int = 0


class CourseOptionsResponse(BaseModel):
    job_id: int
    options: List[CourseOption]


class SoftSkillOption(BaseModel):
    phrase: str
    suggested: bool
    match_percentage: int = 0


class SoftSkillOptionsResponse(BaseModel):
    job_id: int
    options: List[SoftSkillOption]


class DomainOption(BaseModel):
    domain: str
    suggested: bool
    match_percentage: int = 0


class DomainOptionsResponse(BaseModel):
    job_id: int
    suggested_role: str
    options: List[DomainOption]


class SkillOption(BaseModel):
    category: str
    skill: str
    suggested: bool
    match_percentage: int = 0


class SkillOptionsResponse(BaseModel):
    job_id: int
    options: List[SkillOption]


class FinalizeCoursesRequest(BaseModel):
    course_names: List[str] = Field(
        ..., description="Exact course names (from CourseOption.name) the user has chosen to include."
    )
    soft_skill_phrases: List[str] = Field(
        default_factory=list,
        description=(
            "Exact soft-skill phrases (from SoftSkillOption.phrase) the user has chosen to include "
            "in the CV header's soft_skills_line. If empty, the previously-tailored soft_skills_line "
            "is left unchanged."
        ),
    )
    role_phrase: Optional[str] = Field(
        None,
        description=(
            "User-approved/edited role phrase for the seeking_line's 'Seeking a {role} Student "
            "position...' clause. If None/blank, the previously-tailored seeking_line's role is "
            "left unchanged."
        ),
    )
    selected_domains: List[str] = Field(
        default_factory=list,
        description=(
            "Exact domain names (from DomainOption.domain) the user has chosen to include in the "
            "seeking_line's 'with strong interest in ...' clause. If empty, the previously-tailored "
            "seeking_line's domains are left unchanged."
        ),
    )
    role_qualifier: Optional[str] = Field(
        None,
        description=(
            "'Student' or 'Junior' - the qualifier word attached to the role phrase in seeking_line "
            "(e.g. 'Seeking a Chip Design Junior position...'). If None, defaults to 'Student'."
        ),
    )
    semesters_remaining: Optional[int] = Field(
        None,
        description=(
            "0, 1, 2, or 3 - how many semesters remain until graduation, shown in the header's title "
            "line as '(N semesters remaining)'. When 0, the parenthetical is omitted entirely. If "
            "None, the previous/default value is used."
        ),
    )
    include_student_in_title: Optional[bool] = Field(
        None,
        description=(
            "Whether the word 'Student' appears in the header's title line (e.g. 'Electrical & "
            "Computer Engineering Student' vs. just 'Electrical & Computer Engineering'). If None, "
            "defaults to True."
        ),
    )
    selected_skills: Optional[List[str]] = Field(
        None,
        description=(
            "Exact skill names (from SkillOption.skill) the user has chosen to include in the CV's "
            "Skills section, across all categories combined. If None, the previously-tailored skill "
            "selection is left unchanged."
        ),
    )


class FinalizeCoursesResponse(BaseModel):
    job_id: int
    cv_tailored_id: int
    pdf_path: str
    relevant_courses: List[CourseOption]
    soft_skills_line: str = ""
    seeking_line: str = ""
    title_line: str = ""
    email_sent: bool = False








