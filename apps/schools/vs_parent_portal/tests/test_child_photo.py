"""The photograph the parent app shows for a child has to load for that parent.

Mrs Adaeze Nwosu has Tobi at Ikeja and Folu at Epe; Folu has no photograph, so
the portal hands her an empty address for him and the app draws initials. Tobi's
address, fetched as Mrs Nwosu, is the image. Another family's parent holding
the same address is refused. Sunrise has one branch and is held to the same.
"""
from __future__ import annotations

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_students.constants import DocumentType
from schools.vs_students.tests.test_guardian_reads_child_photo import _upload

from .base import ParentFixture

ME = "parent-portal-me"


class ChildPhotoLoadsTests(ParentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        brightfield = cls.client_for(cls, cls.admin)
        _upload(brightfield, cls.tenant, cls.tobi, DocumentType.PASSPORT_PHOTO)
        _upload(brightfield, cls.tenant, cls.zainab, DocumentType.PASSPORT_PHOTO)
        _upload(cls.client_for(cls, cls.solo_admin), cls.solo.tenant, cls.bola,
                DocumentType.PASSPORT_PHOTO)

    def children(self, user):
        data = TenantAPIClient(user).get(reverse(ME)).data["data"]
        return {c["first_name"]: c for c in data["children"]}

    def test_the_address_in_the_payload_loads_for_the_parent(self):
        children = self.children(self.nwosu_user)
        self.assertTrue(children["Tobi"]["photo_url"])
        self.assertEqual(TenantAPIClient(self.nwosu_user).get(children["Tobi"]["photo_url"]).status_code, 200)

    def test_a_child_with_no_photograph_has_no_address(self):
        self.assertEqual(self.children(self.nwosu_user)["Folu"]["photo_url"], "")

    def test_another_familys_parent_holding_the_address_is_refused(self):
        url = self.children(self.yusuf_user)["Zainab"]["photo_url"]
        self.assertEqual(TenantAPIClient(self.yusuf_user).get(url).status_code, 200)
        # The address is bound to the user it was issued to; re-sign it for Mrs Nwosu.
        from core.media import sign
        name = url.split("/media/", 1)[1].split("?", 1)[0]
        stolen = f"/media/{name}?t={sign(name, self.nwosu_user)}&tenant={self.tenant.slug}"
        self.assertEqual(TenantAPIClient(self.nwosu_user).get(stolen).status_code, 404)

    def test_a_one_branch_school_parent_loads_it_too(self):
        url = self.children(self.ade_user)["Bola"]["photo_url"]
        self.assertTrue(url)
        self.assertEqual(TenantAPIClient(self.ade_user).get(url).status_code, 200)
