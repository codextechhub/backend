"""A notice the school posts to its pupils, who it is for, and who has read it.

A notice is a message to an audience, not a transaction, so it names no
branch of its own. Its audience is one or more rows: the whole school, a
branch, a level or a class. A pupil sees a published notice when any row
names them (their branch, their level or their class this year, or the whole
school), and a member of staff sees it when any row lies inside their branch
reach.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager

from .constants import AudienceKind, NoticeStatus


class _Owned(models.Model):
    """Tenant-enforcing managers and timestamps, as every school module has them."""

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"


class Notice(_Owned):
    """One notice. Drafted, published once, and possibly withdrawn.

    Publishing stamps who and when, and is what notifies the audience. A
    correction to a published notice keeps its publication date and stamps
    ``edited_at``, so the pupil reads "edited" rather than a new date that
    would float the notice back to the top of their feed. Withdrawing takes it
    out of every pupil's feed; the staff list keeps it, marked withdrawn.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="notices",
    )
    title = models.CharField(max_length=150)
    body = models.TextField()
    status = models.CharField(
        max_length=12, choices=NoticeStatus.choices, default=NoticeStatus.DRAFT,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    edited_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Owned.Meta):
        ordering = ["-published_at", "-created_at", "-pk"]
        indexes = [
            models.Index(fields=["tenant", "status", "-published_at"]),
        ]

    def __str__(self):
        return self.title


class NoticeAudience(models.Model):
    """One part of a notice's audience: the school, a branch, a level or a class.

    Exactly the column its kind names is set, and no other, which the check
    constraint holds the database to.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="+",
    )
    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="audiences")
    kind = models.CharField(max_length=8, choices=AudienceKind.choices)
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    level = models.ForeignKey(
        "vs_academics.Level", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.PROTECT,
        null=True, blank=True, related_name="+",
    )

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.CheckConstraint(
                name="ck_notice_audience_one_target",
                condition=(
                    Q(kind=AudienceKind.SCHOOL, branch__isnull=True,
                      level__isnull=True, school_class__isnull=True)
                    | Q(kind=AudienceKind.BRANCH, branch__isnull=False,
                        level__isnull=True, school_class__isnull=True)
                    | Q(kind=AudienceKind.LEVEL, branch__isnull=True,
                        level__isnull=False, school_class__isnull=True)
                    | Q(kind=AudienceKind.CLASS, branch__isnull=True,
                        level__isnull=True, school_class__isnull=False)
                ),
            ),
        ]


class NoticeRead(models.Model):
    """That one account has opened one notice, and when. Written once."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="+",
    )
    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+",
    )
    read_at = models.DateTimeField(default=timezone.now)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(fields=["notice", "user"], name="uq_notice_read_once"),
        ]
