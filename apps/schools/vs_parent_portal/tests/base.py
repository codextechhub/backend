"""The school shapes every parent-portal test stands on.

Brightfield has three branches (Lekki, Ikeja and Epe), so the branch
dimension is live; Sunrise has one, so it recedes. Mrs. Adaeze Nwosu is a
parent at Brightfield with two children at two branches and a third who has
left; her husband shares the record but holds no account. Mrs. Amina Yusuf is
another family's parent. Mr. Bayo Ade is a parent at Sunrise.
"""
from __future__ import annotations

from schools.vs_students.constants import Relationship, StudentStatus
from schools.vs_students.models import Guardian
from schools.vs_students.tests.base import StudentsFixture
from vs_rbac.tests.helpers import make_branch, make_school_admin

HUSBAND_PHONE = "08035550101"
HUSBAND_EMAIL = "chukwudi.nwosu@example.ng"
HUSBAND_ADDRESS = "14 Admiralty Way, Lekki"


class ParentFixture(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.epe = make_branch(cls.school, name="Epe", is_main=False)

        cls.nwosu_user = cls.account("adaeze.nwosu@example.ng", "Adaeze", "Nwosu")
        cls.nwosu = cls.parent(cls.nwosu_user, "Adaeze", "Nwosu")
        cls.husband = Guardian.all_objects.create(
            tenant=cls.tenant, first_name="Chukwudi", last_name="Nwosu",
            phone=HUSBAND_PHONE, email=HUSBAND_EMAIL, address=HUSBAND_ADDRESS,
        )

        cls.chiamaka = cls.child(
            "Chiamaka", "Nwosu", "BFS/001", cls.lekki, cls.shared_class, dob=(2013, 4, 18),
        )
        cls.tobi = cls.child(
            "Tobi", "Nwosu", "BFS/002", cls.ikeja, cls.ikeja_class, dob=(2015, 2, 9),
        )
        cls.folu = cls.child(
            "Folu", "Nwosu", "BFS/003", cls.epe, None, dob=(2017, 8, 1),
        )
        cls.left = cls.child(
            "Dayo", "Nwosu", "BFS/004", cls.lekki, None, dob=(2011, 1, 5),
            status=StudentStatus.WITHDRAWN,
        )
        for pupil in (cls.chiamaka, cls.tobi, cls.folu, cls.left):
            cls.link(cls, pupil, cls.nwosu, primary=False, relationship=Relationship.MOTHER)
        for pupil in (cls.chiamaka, cls.tobi):
            cls.link(cls, pupil, cls.husband, primary=True, relationship=Relationship.FATHER)

        cls.yusuf_user = cls.account("amina.yusuf@example.ng", "Amina", "Yusuf")
        cls.yusuf = cls.parent(cls.yusuf_user, "Amina", "Yusuf")
        cls.zainab = cls.child(
            "Zainab", "Yusuf", "BFS/101", cls.lekki, cls.lekki_class, dob=(2013, 11, 30),
        )
        cls.link(cls, cls.zainab, cls.yusuf, primary=True, relationship=Relationship.MOTHER)

        cls.ade_user = cls.account(
            "bayo.ade@example.ng", "Bayo", "Ade", tenant=cls.solo.tenant,
        )
        cls.ade = cls.parent(cls.ade_user, "Bayo", "Ade", tenant=cls.solo.tenant)
        cls.bola = cls.student(
            cls, first="Bola", last="Ade", number="SUN/0001",
            tenant=cls.solo.tenant, branch=cls.solo_branch,
        )
        cls.link(cls, cls.bola, cls.ade, relationship=Relationship.FATHER)

    @classmethod
    def account(cls, email, first, last, *, tenant=None):
        return make_school_admin(
            None, email=email, tenant=tenant or cls.tenant, first_name=first, last_name=last,
        )

    @classmethod
    def parent(cls, user, first, last, *, tenant=None):
        return Guardian.all_objects.create(
            tenant=tenant or cls.tenant, first_name=first, last_name=last,
            phone="08110000000", email=user.email, user=user,
        )

    @classmethod
    def child(cls, first, last, number, branch, school_class, *, dob, status=StudentStatus.ACTIVE):
        import datetime as dt

        pupil = cls.student(
            cls, first=first, last=last, number=number, branch=branch,
            dob=dt.date(*dob), status=status,
        )
        if school_class is not None:
            cls.place(cls, pupil, school_class)
        return pupil
