"""
schools.core.fal.models
=======================

The one table the FAL owns: the link between a fee structure and the academic
term it bills for.

Why the FAL owns it. ``vs_finance.FeeStructure`` is domain-neutral - a named
catalogue of charges for an entity, with no idea what a term is - and it must
stay that way, because the same engine bills a hospital's outpatient tariffs. An
academic term is a school concept. The link between them is exactly the kind of
fact the FAL exists to hold: school vocabulary on one side, a neutral engine row
on the other, and the join living at the boundary rather than inside either.

Decision 2 (2026-07-04) called for this table, and it still stands. What changed
in 1.1.2 is the reference type. The decision put session and term ids in
``CharField`` s because "there is no academic-calendar app", and that reason has
expired: ``schools.vs_academics`` ships ``AcademicSession`` and ``AcademicTerm``,
both tenant-scoped with integer primary keys. Since the FAL lives inside
``apps/schools/``, naming them is not a leak - a schools package importing a
schools app is the direction the architecture allows.

The practical difference is that a term cannot be deleted out from under a
billing link any more: ``PROTECT`` refuses, and the school is told which fee
structures still bill it, instead of the link quietly pointing at a row that no
longer exists.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class FeeStructureTermLink(models.Model):
    """Which academic term a fee structure bills for.

    One structure bills one term, so the link is a ``OneToOneField`` and
    re-linking updates in place rather than accumulating history. A structure
    linked to a session but no term is legitimate: a school with a single annual
    fee bills the session as a whole.

    Tenant integrity is enforced by the service that writes this
    (``FeeTermBridgePort.link_term`` compares ``fee_structure.entity.tenant``
    with ``session.tenant``), not by a database constraint, because the two sides
    reach their tenant through different paths and no single column expresses the
    rule.
    """

    fee_structure = models.OneToOneField(
        "vs_finance.FeeStructure",
        on_delete=models.CASCADE,
        related_name="fal_term_link",
        help_text="The neutral billing template this link gives a term to.",
    )
    session = models.ForeignKey(
        "vs_academics.AcademicSession",
        on_delete=models.PROTECT,
        related_name="fee_structure_links",
        help_text="The academic year the structure bills for.",
    )
    term = models.ForeignKey(
        "vs_academics.AcademicTerm",
        on_delete=models.PROTECT,
        related_name="fee_structure_links",
        null=True, blank=True,
        help_text="The term inside that session, or empty for a whole-session fee.",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["session", "term"]),
        ]
        ordering = ["session", "term", "fee_structure"]

    def __str__(self) -> str:
        return f"{self.fee_structure_id} -> {self.term or self.session}"

    @property
    def label(self) -> str:
        """The human period label an invoice row shows (``2026/2027 First Term``)."""
        if self.term_id:
            return f"{self.session.name} {self.term.name}"
        return self.session.name


class FeeDueBasis(models.TextChoices):
    """How a school wants a fee bill's due date worked out.

    Schools do not think in payment terms. A supplier invoice is due "30 days
    net" because that is a credit arrangement between two businesses; school
    fees are due by a date in the school's own calendar, and every school states
    it differently. These four cover what schools actually say.
    """

    TERM_END = "TERM_END", "End of the term billed"
    SESSION_END = "SESSION_END", "End of the session billed"
    MONTH_END = "MONTH_END", "End of the month the bill is raised"
    DAYS_AFTER = "DAYS_AFTER", "A set number of days after the bill"


class SchoolFeeDuePolicy(models.Model):
    """When a school's fee bills fall due, for the school as a whole.

    One row per school, and the school owns it: this is the setting a bursar
    changes on a settings screen, not a per-run argument. A school that has
    never set one bills on :attr:`FeeDueBasis.TERM_END`, because a term's fees
    being due by the end of that term is what a school means when it says
    nothing.

    It lives in the FAL rather than in ``vs_finance`` because three of the four
    bases are academic-calendar facts. ``vs_finance`` prices and posts invoices
    for hospitals as readily as for schools and must never learn what a term is;
    the FAL resolves the basis to a real date and hands the engine that date.

    There is deliberately no branch column. A due date is a rule about the
    school's fee calendar, and a school running Ikeja and Lekki bills the same
    term at both; a per-branch rule would mean one child's fees falling due on a
    different day from their sibling's at the other site.
    """

    tenant = models.OneToOneField(
        "vs_tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="fee_due_policy",
        help_text="The school this rule belongs to.",
    )
    basis = models.CharField(
        max_length=16, choices=FeeDueBasis.choices, default=FeeDueBasis.TERM_END,
        help_text="Which date the school wants its fee bills to fall due on.",
    )
    #: Read only when ``basis`` is ``DAYS_AFTER``; kept rather than cleared when
    #: the basis changes, so a school that switches to term end and back does not
    #: lose the number it had chosen.
    days_after = models.PositiveSmallIntegerField(
        default=30,
        help_text="Days after the bill date, used only when the basis is DAYS_AFTER.",
    )
    updated_by = models.ForeignKey(
        "vs_user.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fee_due_policy_updates",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "school fee due policies"

    def __str__(self) -> str:
        return f"{self.tenant_id}: {self.get_basis_display()}"
