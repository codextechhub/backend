"""List teachers who hold classes at a branch none of their roles cover.

Each one holds the class on paper and cannot see it: a class teacher who cannot
open the register, a subject teacher who cannot find the score sheet, a lesson
nobody can mark. The appointment and role-change paths refuse new cases; this
finds the ones written before they did, so a school can hand the classes over
or give the teacher a role at that branch.

Read-only. It runs the same check those paths run
(:func:`~schools.vs_staff.services.teaching.assert_still_reaches_own_classes`),
so it cannot disagree with them. ``--tenant`` narrows it to one school's slug.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "List teachers who hold classes at a branch they do not work at."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Only the school with this slug.")

    def handle(self, *args, **options):
        from schools.vs_academics.models import SchoolClass, SessionStatus
        from schools.vs_calendar.models import TimetableSlot
        from vs_tenants.models import Tenant
        from vs_user.models import User

        from ...exceptions import TeacherStillHoldsClasses
        from ...models import TeachingAssignment
        from ...services.teaching import assert_still_reaches_own_classes

        schools = Tenant.objects.filter(kind=Tenant.Kind.SCHOOL).order_by("slug")
        if options["tenant"]:
            schools = schools.filter(slug=options["tenant"])
            if not schools.exists():
                raise CommandError(f"No school with the slug {options['tenant']!r}.")

        found = 0
        for school in schools:
            live = {"tenant": school}
            ids = set(
                SchoolClass.all_objects.filter(class_teacher__isnull=False, **live)
                .exclude(session__status=SessionStatus.ARCHIVED)
                .values_list("class_teacher__user_id", flat=True),
            )
            ids.update(
                TeachingAssignment.all_objects.filter(**live)
                .exclude(session__status=SessionStatus.ARCHIVED)
                .values_list("staff__user_id", flat=True),
            )
            ids.update(
                TimetableSlot.all_objects.filter(teacher__isnull=False, **live)
                .exclude(session__status=SessionStatus.ARCHIVED)
                .values_list("teacher_id", flat=True),
            )
            for user in User.objects.filter(pk__in=ids).order_by("first_name", "last_name"):
                try:
                    assert_still_reaches_own_classes(school, user)
                except TeacherStillHoldsClasses as refusal:
                    found += 1
                    for branch, classes in sorted(refusal.extra["held"].items()):
                        for name, holds in sorted(classes.items()):
                            self.stdout.write(
                                f"{school.slug}: {refusal.extra['teacher']} - {name} "
                                f"at {branch} ({', '.join(holds)})",
                            )

        self.stdout.write(
            f"{found} teacher{'' if found == 1 else 's'} hold classes outside their branches."
            if found else "Every teacher works at the branches of the classes they hold.",
        )
