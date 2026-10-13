"""The gradebook fields an administrator may restrict per role.

Four resources, because a role is given a sheet and not a hold, a family's
result and not a programme's grading scale:

* ``gradebook.sheet`` is the score sheet and what is worked out on it.
* ``gradebook.result`` is a student's result for a term: the figures that are
  compiled across subjects, the comments and ratings, and the release record.
* ``gradebook.hold`` is a fee hold and the balance beside it.
* ``gradebook.setup`` is assessment setup: structures, scales and remark
  templates.

A resource permission answers whether somebody may open the screen; these
switches answer which of its fields they read and change. **Every field is
included and open by default**, except the balance a hold shows, which is the
school's money position for a family and starts closed to everyone but the
roles that work holds.

**The sheet.** The mark in a cell travels as ``mark`` and ``marker`` (ABS or
Excused), and as the list of ``cells`` that carries them, so one switch hides
the marks wherever they are printed. The row's worked-out figures (total,
obtainable, percentage, grade, remark, whether it is a fail, whether it is
complete, the note about excused components) are one derived group and
read-only; the sheet's progress count (students complete of students on the
sheet) is the same group, because it states how many rows are complete. The class
average, highest, lowest and the pass rate are another; that switch also governs
the figures the moderation queue and analytics print. The note a sheet is
returned with is a text field that is read and written, and travels as ``note``
inside the sheet's ``returned`` block and in the body of a return.

**The batch save and the return are write paths of this resource.** The body of a
save carries ``cells`` (and ``mark`` and ``marker`` inside each), and the body of
a return carries ``note``; a role that may not write them is refused the whole
request and nothing is saved. A null mark is a write too (it blanks a cell), so
the views judge the submitted names by presence before the serializers run.

**The result.** The overall average and the position in class are derived and
each has its own switch, so a school can hide the position from a role that
reads the average. Ratings, the class teacher's comment and the principal's
comment are read and written. The guardian on the release list is read-only here
and **also follows the Student Management guardian switches**: when those hide
a guardian's name or email, the release list and the Results tab hide them too,
whatever this switch says. The email state
(emailed, no email on file, held) and when it was sent are facts the system
records, so they are read-only.

**The broadsheet, the compile drawer and the principal's comments** read these
same switches under the names they print. The broadsheet and the drawer send the
overall average as ``average``, the drawer sends the school's traits beside the
ratings as ``traits``, and the broadsheet's ``comment_done`` says whether the
class teacher's comment is written, so it follows that comment's switch. The
principal's comments screen sends a class's own comment as ``override`` and takes
it as ``comments``. The subject totals and grades on both screens are the sheet's
worked-out figures and follow ``gradebook.sheet.row_result``. The template text
printed beside the principal's comment is the remark template and follows
``gradebook.setup.remark_templates``. The term's attendance counts on the drawer
follow ``attendance.register.counts`` and the broadsheet's warning about days
with no register follows ``attendance.register.status``: each states a number
the register switch withholds.

**The hold.** The reason a hold was placed and the note a lift carries are read
and written. The body of a lift sends its note as ``note`` and the hold carries
it back as ``lift_note``, which is the registered name. The balance, the date of
the oldest unpaid invoice and the days that date makes it (``days_owed``) come
from Finance, are never typed here, and are sensitive: they are closed until a
role is given Read, and the roles whose work is holds read them by default.

**Setup.** The components of a structure, the maximum of each (and the total
they add up to, with the verdict on it), the bands of a scale (with the verdict
on them), a band's remark and the principal's remark templates are read and
written. The names of structures and scales, their status and the programme they
belong to are not school data and have no switch.

**The report card, the class print and the Results tab are staff screens and
are filtered.** The card is rendered from the published snapshot through the
same switches as every other screen. Its subject cells follow the mark switch;
the subject totals, grades and remarks follow the figures switch, and so do the
"Total obtained" and "Total obtainable" lines, which add those totals up. The
class average, highest and lowest follow the class figures switch. The "Overall
average" line follows the overall average switch and the "Position in class" line
the position switch, and a line is dropped whole, label and value together, when
its switch is closed. The ratings, the rating key that explains them, each
comment and the name that signs it follow their own switches. The term's
attendance lines follow the register's counts switch. The grading key is setup
text printed on every card and has no switch. The Results tab renders its
subjects the same way, and a hold in its history carries its reason (or its lift
note) as a field of its own that the hold-reason switch removes. The primary guardian
on the release list and the Results tab is one entry (``guardian``) governed by
the guardian-contact switch and, as its parts, by the Student Management name and
email switches: the stricter of the two wins, and a part those switches hide is
left out alone.

**Not registered here.** The reason an amendment is raised and the note it is
decided with belong to the amendment record and are registered with it. The
class's returned-with note on the broadsheet uses the sheet's return note
switch where it is rendered.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"

SHEET_SURFACES = (
    "schools.vs_gradebook.serializers.SheetSerializer",
    "schools.vs_gradebook.serializers.SheetRowSerializer",
    "schools.vs_gradebook.serializers.SheetCellSerializer",
    "schools.vs_gradebook.serializers.SheetStatsSerializer",
    "schools.vs_gradebook.serializers.SheetReturnedSerializer",
    "schools.vs_gradebook.serializers.SheetProgressSerializer",
    "schools.vs_gradebook.serializers.ModerationRowSerializer",
    "schools.vs_gradebook.serializers.BroadsheetCellSerializer",
    "schools.vs_gradebook.serializers.CompileSubjectSerializer",
    "schools.vs_gradebook.serializers.CellWriteSerializer",
    "schools.vs_gradebook.serializers.ScoresWriteSerializer",
    "schools.vs_gradebook.serializers.ReturnBodySerializer",
    "schools.vs_gradebook.serializers.ClassReturnBodySerializer",
    "schools.vs_gradebook.serializers.CardSubjectSerializer",
    "schools.vs_gradebook.serializers.ResultSubjectSerializer",
)

RESULT_SURFACES = (
    "schools.vs_gradebook.serializers.ResultRowSerializer",
    "schools.vs_gradebook.serializers.ReleaseRowSerializer",
    "schools.vs_gradebook.serializers.BroadsheetRowSerializer",
    "schools.vs_gradebook.serializers.CompileSerializer",
    "schools.vs_gradebook.serializers.CompileWriteSerializer",
    "schools.vs_gradebook.serializers.PrincipalBandSerializer",
    "schools.vs_gradebook.serializers.PrincipalCommentsWriteSerializer",
    "schools.vs_gradebook.serializers.ReportCardSerializer",
    "schools.vs_gradebook.serializers.ResultTermSerializer",
    "schools.vs_gradebook.serializers.StudentResultsSerializer",
)

HOLD_SURFACES = (
    "schools.vs_gradebook.serializers.HoldRowSerializer",
    "schools.vs_gradebook.serializers.HoldRecordSerializer",
    "schools.vs_gradebook.serializers.HoldLiftedSerializer",
    "schools.vs_gradebook.serializers.HoldPlaceBodySerializer",
    "schools.vs_gradebook.serializers.HoldLiftBodySerializer",
    "schools.vs_gradebook.serializers.ResultHistorySerializer",
)

SETUP_SURFACES = (
    "schools.vs_gradebook.serializers.StructureSerializer",
    "schools.vs_gradebook.serializers.ComponentSerializer",
    "schools.vs_gradebook.serializers.StructureWriteSerializer",
    "schools.vs_gradebook.serializers.ComponentWriteSerializer",
    "schools.vs_gradebook.serializers.ScaleSerializer",
    "schools.vs_gradebook.serializers.BandSerializer",
    "schools.vs_gradebook.serializers.ScaleWriteSerializer",
    "schools.vs_gradebook.serializers.BandWriteSerializer",
    "schools.vs_gradebook.serializers.RemarkTemplatesSerializer",
    "schools.vs_gradebook.serializers.RemarkTemplatesWriteSerializer",
)


def register():
    """Publish the gradebook's field declarations to the Field Access registry."""
    register_fields(
        "gradebook",
        "sheet",
        surfaces=SHEET_SURFACES,
        fields=(
            FieldSpec("mark", "Component mark", group="Marks", scope=_TENANT,
                      sort_order=10, api_names=("mark", "marker", "cells"),
                      description="A student's mark in each component, with ABS "
                                  "(absent) and Excused where they apply."),
            FieldSpec("row_result", "Total, percentage, grade and remark",
                      group="Figures", scope=_TENANT, sort_order=10, writable=False,
                      api_names=("total", "obtainable", "percentage", "grade",
                                 "remark", "is_fail", "complete", "excused_note"),
                      description="What a student's marks add up to in the subject, "
                                  "the percentage, the grade and remark it earns."),
            FieldSpec("class_stats", "Class average, highest and lowest",
                      group="Figures", scope=_TENANT, sort_order=20, writable=False,
                      api_names=("class_average", "highest", "lowest", "pass_rate"),
                      description="The subject's average, highest and lowest across "
                                  "the class, and the share who passed, on the sheet, "
                                  "the moderation queue and analytics."),
            FieldSpec("return_note", "Return note", group="Review", scope=_TENANT,
                      sort_order=10, api_names=("note",),
                      description="Why a sheet or a class was sent back for correction."),
        ),
    )
    register_fields(
        "gradebook",
        "result",
        surfaces=RESULT_SURFACES,
        fields=(
            FieldSpec("overall_average", "Overall average", group="Figures",
                      scope=_TENANT, sort_order=10, writable=False,
                      api_names=("overall_average", "average"),
                      description="The mean of a student's percentages across the "
                                  "subjects they take."),
            FieldSpec("position", "Position in class", group="Figures",
                      scope=_TENANT, sort_order=20, writable=False,
                      description="Where a student ranks in the class by overall "
                                  "average. A school that does not rank shows none."),
            FieldSpec("ratings", "Conduct and skills ratings", group="Comments",
                      scope=_TENANT, sort_order=10,
                      api_names=("ratings", "traits"),
                      description="The class teacher's 1 to 5 rating of each trait, "
                                  "and the list of traits a school rates."),
            FieldSpec("teacher_comment", "Class teacher's comment", group="Comments",
                      scope=_TENANT, sort_order=20,
                      api_names=("teacher_comment", "comment_done", "teacher_name"),
                      description="The class teacher's comment on a student's term, "
                                  "whether one has been written, and the name that "
                                  "signs it on a report card."),
            FieldSpec("principal_comment", "Principal's comment", group="Comments",
                      scope=_TENANT, sort_order=30,
                      api_names=("principal_comment", "comments", "override",
                                 "principal_name"),
                      description="The principal's comment for the band a student's "
                                  "overall average falls in, as the principal writes "
                                  "it for a class, and the name that signs it on a "
                                  "report card."),
            FieldSpec("guardian_contact", "Guardian name and email", group="Release",
                      scope=_TENANT, sort_order=10, writable=False,
                      api_names=("guardian",),
                      description="The primary guardian a report card is released "
                                  "to. Follows the guardian switches of Student "
                                  "Management: where those hide a name or an email, "
                                  "it is hidden here too."),
            FieldSpec("email_state", "Release email state", group="Release",
                      scope=_TENANT, sort_order=20, writable=False,
                      api_names=("email_state", "emailed_at"),
                      description="Whether the guardian was emailed that the report "
                                  "card is ready, and when."),
        ),
    )
    register_fields(
        "gradebook",
        "hold",
        surfaces=HOLD_SURFACES,
        fields=(
            FieldSpec("reason", "Hold reason and lift note", group="Hold",
                      scope=_TENANT, sort_order=10,
                      api_names=("reason", "lift_note"),
                      description="Why a result was held, and the note a hold was "
                                  "lifted with."),
            FieldSpec("balance", "Balance and oldest unpaid", group="Finance",
                      scope=_TENANT, sort_order=10, writable=False, sensitive=True,
                      read_by=("school_admin", "finance_admin"),
                      api_names=("balance", "oldest_unpaid_on", "days_owed"),
                      description="What a student's family owes and since when, "
                                  "read from Finance at the time shown."),
        ),
    )
    register_fields(
        "gradebook",
        "setup",
        surfaces=SETUP_SURFACES,
        fields=(
            FieldSpec("components", "Assessment components", group="Structure",
                      scope=_TENANT, sort_order=10,
                      description="The columns a result is made of, such as two "
                                  "continuous assessments and an examination."),
            FieldSpec("max_score", "Component maximums", group="Structure",
                      scope=_TENANT, sort_order=20,
                      api_names=("max_score", "total", "total_check"),
                      description="The most a student can score in each component, "
                                  "the total they add up to and whether it is valid."),
            FieldSpec("bands", "Grading bands", group="Scale", scope=_TENANT,
                      sort_order=10, api_names=("bands", "check"),
                      description="The lowest mark and grade of each band of a "
                                  "grading scale, and whether the bands are valid."),
            FieldSpec("remark", "Band remarks", group="Scale", scope=_TENANT,
                      sort_order=20,
                      description="The remark each band of a grading scale prints."),
            FieldSpec("remark_templates", "Principal's remark templates",
                      group="Remarks", scope=_TENANT, sort_order=10,
                      api_names=("templates",),
                      description="The sentences the principal starts a class's "
                                  "comments from, one for each band of overall "
                                  "average."),
        ),
    )
