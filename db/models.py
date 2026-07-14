"""
db/models.py
------------
Core ORM models for the job pipeline: Company and Job.

Status lifecycle for Job.status (free-text but conventionally one of):
    'scraped' -> 'tailored' -> 'emailed' -> 'approved' -> 'applied'
"""

import hashlib
import re
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    career_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())

    jobs: Mapped[list["Job"]] = relationship(back_populates="company")

    def __repr__(self) -> str:
        return f"<Company id={self.id} name={self.name!r}>"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_jobs_content_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    raw_description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # e.g. 'manual_whatsapp'
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="scraped", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    company: Mapped["Company | None"] = relationship(back_populates="jobs")
    tailored_versions: Mapped[list["CVTailored"]] = relationship(back_populates="job")

    def __repr__(self) -> str:
        return f"<Job id={self.id} title={self.title!r} status={self.status!r}>"



class CVMaster(Base):
    __tablename__ = "cv_master"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_label: Mapped[str] = mapped_column(String(128), nullable=False, default="v1-frozen-layout")
    html_template_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())

    tailored_versions: Mapped[list["CVTailored"]] = relationship(back_populates="cv_master")

    def __repr__(self) -> str:
        return f"<CVMaster id={self.id} version_label={self.version_label!r}>"


class CVTailored(Base):
    __tablename__ = "cv_tailored"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    cv_master_id: Mapped[int] = mapped_column(ForeignKey("cv_master.id"), nullable=False)
    tailored_fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())

    job: Mapped["Job"] = relationship(back_populates="tailored_versions")
    cv_master: Mapped["CVMaster"] = relationship(back_populates="tailored_versions")

    def __repr__(self) -> str:
        return f"<CVTailored id={self.id} job_id={self.job_id} pdf_path={self.pdf_path!r}>"


def _normalize_for_hash(text: str) -> str:

    """Lowercase, collapse whitespace, strip -- so trivial formatting
    differences (extra spaces, case) never produce different hashes for
    what is really the same job posting."""
    return re.sub(r"\s+", " ", text.strip().lower())


def compute_content_hash(title: str, company_name: str | None, raw_description: str) -> str:
    """
    Deterministic SHA-256 fingerprint of (title + company + description),
    used to detect duplicate job postings (e.g. re-scraped or re-pasted
    from WhatsApp) before they're inserted into the DB.
    """
    normalized = "|".join(
        _normalize_for_hash(part) for part in (title, company_name or "", raw_description)
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
