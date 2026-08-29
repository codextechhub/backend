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
