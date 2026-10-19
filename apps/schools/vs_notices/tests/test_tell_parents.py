"""Telling parents that a notice has reached their children.

At Brightfield, Mrs. Ngozi Pupil is the mother of Tunde (Lekki, JSS1 B) and
Kemi (Ikeja, JSS1 C), and Mr. Emeka Pupil is Tunde's father. Mrs. Funke Adeyemi
teaches JSS1 B and is also Kemi's aunt and guardian, so she holds a staff
account and a guardian record at once. Chiamaka's mother has no account and
Chiamaka's father has a deactivated one. Mr. Bayo Ade is the one parent at
Sunrise, a school with a single branch.
"""
from __future__ import annotations

from unittest import mock

from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification

from schools.vs_notices.constants import CFG_TELL_PARENTS, EVENT_PUBLISHED_PARENT
from schools.vs_notices.links import parent_app_url
from schools.vs_notices.services import notices as service
from schools.vs_notices.tasks import dispatch_published
from schools.vs_students.constants import Relationship, StudentStatus
from schools.vs_students.models import Guardian
from vs_rbac.tests.helpers import make_school_admin
from vs_user.models import User

from .test_notices import NoticesFixture

SEND = "vs_notifications.notify.send_notification"


class ParentsFixture(NoticesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from django.core.management import call_command

        call_command("seed_config_catalogue", verbosity=0)

        cls.ngozi_user = cls.account("ngozi.pupil@example.ng", "Ngozi")
        ngozi = cls.guardian_of(cls.ngozi_user, "Ngozi")
        cls.link(cls, cls.tunde, ngozi, primary=False, relationship=Relationship.MOTHER)
        cls.link(cls, cls.kemi, ngozi, primary=False, relationship=Relationship.MOTHER)

        cls.emeka_user = cls.account("emeka.pupil@example.ng", "Emeka")
        cls.link(
            cls, cls.tunde, cls.guardian_of(cls.emeka_user, "Emeka"),
            primary=True, relationship=Relationship.FATHER,
        )

        cls.link(
            cls, cls.kemi, cls.guardian_of(cls.teacher, "Funke"),
            primary=False, relationship=Relationship.OTHER,
        )

        no_account = Guardian.all_objects.create(
            tenant=cls.tenant, first_name="Ada", last_name="Okoro", phone="08110000020",
        )
        cls.link(cls, cls.chiamaka, no_account, primary=True, relationship=Relationship.MOTHER)
        cls.gone_user = cls.account("tayo.okoro@example.ng", "Tayo")
        cls.link(
            cls, cls.chiamaka, cls.guardian_of(cls.gone_user, "Tayo"),
            primary=False, relationship=Relationship.FATHER,
        )
        cls.gone_user.status = User.Status.DEACTIVATED
        cls.gone_user.save()

    @classmethod
    def account(cls, email, first, *, tenant=None):
        return make_school_admin(
            None, email=email, tenant=tenant or cls.tenant, first_name=first, last_name="Pupil",
        )

    @classmethod
    def guardian_of(cls, user, first, *, tenant=None):
        return Guardian.all_objects.create(
            tenant=tenant or cls.tenant, first_name=first, last_name="Pupil",
            phone="08110000010", user=user,
        )

    def publish(self, audience, *, tenant=None, actor=None, title="Sports day"):
        return service.create(
            tenant or self.tenant, actor=actor or self.admin, title=title,
            body="Please read.", audience=audience, publish_now=True,
        )

    def parent_calls(self, send):
        return [
            call.kwargs for call in send.call_args_list
            if call.kwargs["event_key"] == EVENT_PUBLISHED_PARENT
        ]

    def told(self, send):
        """``{user: child_names}`` for every parent the dispatch addressed."""
        out = {}
        for call in self.parent_calls(send):
            for user in call["recipients"]:
                self.assertNotIn(user, out, "a parent was told twice")
                out[user] = call["context"]["child_names"]
        return out

    def dispatch(self, notice):
        with mock.patch(SEND) as send:
            dispatch_published(notice.pk)
        return send


class TellParentsTests(ParentsFixture):
    def test_the_parent_of_a_pupil_in_the_audience_is_told(self):
        notice = self.publish([{"kind": "CLASS", "id": self.lekki_class.pk}])
        send = self.dispatch(notice)
        self.assertEqual(
            self.told(send),
            {self.ngozi_user: "Tunde", self.emeka_user: "Tunde"},
        )
        call = self.parent_calls(send)[0]
        self.assertEqual(call["metadata"], {"notice_id": notice.pk})
        self.assertEqual(call["tenant"], self.tenant)
        self.assertEqual(call["context"]["notice_title"], "Sports day")

    def test_a_parent_of_two_children_in_the_audience_is_told_once_naming_both(self):
        send = self.dispatch(self.publish([{"kind": "SCHOOL"}]))
        told = self.told(send)
        self.assertEqual(told[self.ngozi_user], "Tunde and Kemi")
        self.assertEqual(told[self.emeka_user], "Tunde")

    def test_a_parent_is_not_told_of_a_child_outside_the_audience(self):
        send = self.dispatch(self.publish([{"kind": "BRANCH", "id": self.ikeja.pk}]))
        self.assertEqual(
            self.told(send),
            {self.ngozi_user: "Kemi", self.teacher: "Kemi"},
        )

    def test_nobody_is_told_when_no_child_of_theirs_is_in_the_audience(self):
        send = self.dispatch(self.publish([{"kind": "CLASS", "id": self.shared_class.pk}]))
        self.assertEqual(self.told(send), {})

    def test_a_guardian_with_no_account_or_a_deactivated_one_is_not_told(self):
        send = self.dispatch(self.publish([{"kind": "SCHOOL"}]))
        self.assertNotIn(self.gone_user, self.told(send))
        recipients = {u for call in self.parent_calls(send) for u in call["recipients"]}
        self.assertEqual(len(recipients), len(self.told(send)))
        self.assertEqual(len(recipients), 3)

    def test_a_teacher_who_is_also_a_parent_is_told_once_as_a_parent(self):
        send = self.dispatch(self.publish([{"kind": "SCHOOL"}]))
        self.assertEqual(self.told(send)[self.teacher], "Kemi")
        pupil_calls = [
            call.kwargs for call in send.call_args_list
            if call.kwargs["event_key"] != EVENT_PUBLISHED_PARENT
        ]
        self.assertNotIn(
            self.teacher, [u for call in pupil_calls for u in call["recipients"]],
        )

    def test_a_child_who_has_left_the_roll_is_not_named(self):
        self.kemi.status = StudentStatus.WITHDRAWN
        self.kemi.save(update_fields=["status"])
        told = self.told(self.dispatch(self.publish([{"kind": "SCHOOL"}])))
        self.assertEqual(told[self.ngozi_user], "Tunde")
        self.assertNotIn(self.teacher, told)

    def test_an_account_from_another_school_is_not_told(self):
        stranger = self.account("stranger@sunrise.test", "Stranger", tenant=self.solo.tenant)
        self.link(
            self.chiamaka, self.guardian_of(stranger, "Stranger"),
            primary=False, relationship=Relationship.OTHER,
        )
        self.assertNotIn(stranger, self.told(self.dispatch(self.publish([{"kind": "SCHOOL"}]))))

    def test_the_pupils_are_still_told(self):
        send = self.dispatch(self.publish([{"kind": "SCHOOL"}]))
        pupils = {
            u for call in send.call_args_list
            if call.kwargs["event_key"] != EVENT_PUBLISHED_PARENT
            for u in call.kwargs["recipients"]
        }
        self.assertEqual(pupils, {p.user for p in (self.chiamaka, self.tunde, self.kemi)})

    def test_one_failing_send_does_not_stop_the_other_parents(self):
        notice = self.publish([{"kind": "SCHOOL"}])
        real = {"calls": 0}

        def flaky(**kwargs):
            if kwargs["event_key"] == EVENT_PUBLISHED_PARENT:
                real["calls"] += 1
                if real["calls"] == 1:
                    raise RuntimeError("mail server down")

        with mock.patch(SEND, side_effect=flaky) as send:
            dispatch_published(notice.pk)
        self.assertEqual(len(self.parent_calls(send)), 3)

    def test_a_school_with_one_branch_tells_its_parent(self):
        from schools.vs_students.models import Student

        pupil = Student.all_objects.create(
            tenant=self.solo.tenant, branch=self.solo_branch, first_name="Bola",
            last_name="Ade", student_number="SUN/0001", date_of_birth=self.tunde.date_of_birth,
            gender=self.tunde.gender, status=StudentStatus.ACTIVE,
            enrolment_date=self.tunde.enrolment_date,
        )
        parent = self.account("bayo.ade@example.ng", "Bayo", tenant=self.solo.tenant)
        self.link(
            pupil, self.guardian_of(parent, "Bayo", tenant=self.solo.tenant),
            primary=True, relationship=Relationship.FATHER,
        )
        notice = self.publish(
            [{"kind": "SCHOOL"}], tenant=self.solo.tenant, actor=self.solo_admin,
        )
        self.assertEqual(self.told(self.dispatch(notice)), {parent: "Bola"})


class SettingTests(ParentsFixture):
    def turn_off(self, tenant=None):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=CFG_TELL_PARENTS),
            value=False, actor=self.admin, tenant=tenant or self.tenant,
        )

    def test_a_school_that_has_chosen_nothing_tells_parents(self):
        definition = ConfigurationDefinition.objects.get(key=CFG_TELL_PARENTS)
        self.assertIs(definition.default_value, True)
        self.assertTrue(self.told(self.dispatch(self.publish([{"kind": "SCHOOL"}]))))

    def test_with_the_setting_off_no_parent_is_told_and_the_pupils_still_are(self):
        self.turn_off()
        send = self.dispatch(self.publish([{"kind": "SCHOOL"}]))
        self.assertEqual(self.parent_calls(send), [])
        pupils = {u for call in send.call_args_list for u in call.kwargs["recipients"]}
        self.assertEqual(pupils, {p.user for p in (self.chiamaka, self.tunde, self.kemi)})

    def test_one_schools_setting_does_not_silence_another(self):
        self.turn_off(self.solo.tenant)
        self.assertTrue(self.told(self.dispatch(self.publish([{"kind": "SCHOOL"}]))))

    def test_a_withdrawn_notice_tells_no_parent(self):
        notice = self.publish([{"kind": "SCHOOL"}])
        service.withdraw(notice, actor=self.admin)
        with mock.patch(SEND) as send:
            self.assertEqual(dispatch_published(notice.pk), 0)
        send.assert_not_called()


class RenderedParentNoticeTests(ParentsFixture):
    """The real templates, rendered for the real recipient."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_notifications.services.seed import (
            seed_notification_templates,
            seed_platform_settings,
        )

        seed_notification_templates()
        seed_platform_settings()

    def test_the_parent_reads_the_notice_in_the_app_and_by_email(self):
        notice = self.publish([{"kind": "SCHOOL"}], title="Mid-term break begins Oct 25")
        with self.settings(SCHOOL_APP_BASE_URL="https://xvs.codexng.com"):
            dispatch_published(notice.pk)

        rows = Notification.all_objects.filter(
            event_type__key=EVENT_PUBLISHED_PARENT, recipient=self.ngozi_user,
        )
        self.assertEqual(
            {row.channel for row in rows}, {ChannelChoices.IN_APP, ChannelChoices.EMAIL},
        )
        in_app = rows.get(channel=ChannelChoices.IN_APP)
        self.assertEqual(
            in_app.subject, "A new notice for Tunde and Kemi: Mid-term break begins Oct 25",
        )
        self.assertNotIn("Please read.", in_app.body)
        email = rows.get(channel=ChannelChoices.EMAIL)
        self.assertIn("Dear Parent or Guardian,", email.body)
        self.assertIn("Tunde and Kemi", email.body)
        self.assertIn(f"https://brightfield.xvs.codexng.com/parent/notices/{notice.pk}", email.body)
        self.assertNotIn("Please read.", email.body)

    def test_the_bell_entry_opens_the_notice_in_the_parent_app_and_clears_on_reading(self):
        from vs_notifications.serializers import notification_action_url

        notice = self.publish([{"kind": "CLASS", "id": self.lekki_class.pk}])
        dispatch_published(notice.pk)
        row = Notification.all_objects.get(
            event_type__key=EVENT_PUBLISHED_PARENT, recipient=self.emeka_user,
            channel=ChannelChoices.IN_APP,
        )
        self.assertEqual(notification_action_url(row), f"/notices/{notice.pk}")
        self.assertFalse(row.is_read)

        service.mark_read(notice, self.emeka_user)
        row.refresh_from_db()
        self.assertTrue(row.is_read)

    def test_the_link_is_blank_where_no_host_is_configured(self):
        with self.settings(SCHOOL_APP_BASE_URL=""):
            self.assertEqual(parent_app_url(self.tenant, "notices/1"), "")
