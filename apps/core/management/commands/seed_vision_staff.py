from django.core.management.base import BaseCommand
from django.db import transaction

from vs_user.models import User


VISION_STAFF = [
    # Original 25
    {"first_name": "Amara",    "last_name": "Okafor",    "email": "amara.okafor@vision.edu"},
    {"first_name": "Chidi",    "last_name": "Eze",        "email": "chidi.eze@vision.edu"},
    {"first_name": "Ngozi",    "last_name": "Adeyemi",    "email": "ngozi.adeyemi@vision.edu"},
    {"first_name": "Emeka",    "last_name": "Nwosu",      "email": "emeka.nwosu@vision.edu"},
    {"first_name": "Fatima",   "last_name": "Bello",      "email": "fatima.bello@vision.edu"},
    {"first_name": "Seun",     "last_name": "Adesanya",   "email": "seun.adesanya@vision.edu"},
    {"first_name": "Kemi",     "last_name": "Olawale",    "email": "kemi.olawale@vision.edu"},
    {"first_name": "Tunde",    "last_name": "Fashola",    "email": "tunde.fashola@vision.edu"},
    {"first_name": "Bola",     "last_name": "Tinubu",     "email": "bola.tinubu@vision.edu"},
    {"first_name": "Ifeanyi",  "last_name": "Obiora",     "email": "ifeanyi.obiora@vision.edu"},
    {"first_name": "Ada",      "last_name": "Nwachukwu",  "email": "ada.nwachukwu@vision.edu"},
    {"first_name": "Dayo",     "last_name": "Adeleke",    "email": "dayo.adeleke@vision.edu"},
    {"first_name": "Zainab",   "last_name": "Musa",       "email": "zainab.musa@vision.edu"},
    {"first_name": "Uche",     "last_name": "Obi",        "email": "uche.obi@vision.edu"},
    {"first_name": "Tobi",     "last_name": "Adewale",    "email": "tobi.adewale@vision.edu"},
    {"first_name": "Halima",   "last_name": "Ibrahim",    "email": "halima.ibrahim@vision.edu"},
    {"first_name": "Femi",     "last_name": "Bankole",    "email": "femi.bankole@vision.edu"},
    {"first_name": "Chisom",   "last_name": "Anyanwu",    "email": "chisom.anyanwu@vision.edu"},
    {"first_name": "Remi",     "last_name": "Afolabi",    "email": "remi.afolabi@vision.edu"},
    {"first_name": "Nnamdi",   "last_name": "Okonkwo",    "email": "nnamdi.okonkwo@vision.edu"},
    {"first_name": "Yetunde",  "last_name": "Salako",     "email": "yetunde.salako@vision.edu"},
    {"first_name": "Bayo",     "last_name": "Ogunleye",   "email": "bayo.ogunleye@vision.edu"},
    {"first_name": "Chibundo", "last_name": "Okeke",      "email": "chibundo.okeke@vision.edu"},
    {"first_name": "Shade",    "last_name": "Coker",      "email": "shade.coker@vision.edu"},
    {"first_name": "Lanre",    "last_name": "Odunbaku",   "email": "lanre.odunbaku@vision.edu"},
    # Additional 15 — brings total to 40 to fill the 7-level organogram
    {"first_name": "Biodun",   "last_name": "Akerele",   "email": "biodun.akerele@vision.edu"},
    {"first_name": "Chiamaka", "last_name": "Ejike",     "email": "chiamaka.ejike@vision.edu"},
    {"first_name": "Dapo",     "last_name": "Adeoye",    "email": "dapo.adeoye@vision.edu"},
    {"first_name": "Ebele",    "last_name": "Ugwu",      "email": "ebele.ugwu@vision.edu"},
    {"first_name": "Fola",     "last_name": "Ayodele",   "email": "fola.ayodele@vision.edu"},
    {"first_name": "Gbenga",   "last_name": "Alabi",     "email": "gbenga.alabi@vision.edu"},
    {"first_name": "Ifeoma",   "last_name": "Garba",     "email": "ifeoma.garba@vision.edu"},
    {"first_name": "Jide",     "last_name": "Abiodun",   "email": "jide.abiodun@vision.edu"},
    {"first_name": "Kunle",    "last_name": "Martins",   "email": "kunle.martins@vision.edu"},
    {"first_name": "Lara",     "last_name": "Badmus",    "email": "lara.badmus@vision.edu"},
    {"first_name": "Musa",     "last_name": "Adamu",     "email": "musa.adamu@vision.edu"},
    {"first_name": "Nkechi",   "last_name": "Okoro",     "email": "nkechi.okoro@vision.edu"},
    {"first_name": "Olumide",  "last_name": "Fagbemi",   "email": "olumide.fagbemi@vision.edu"},
    {"first_name": "Priscilla","last_name": "Ogbonna",   "email": "priscilla.ogbonna@vision.edu"},
    {"first_name": "Rashida",  "last_name": "Sule",      "email": "rashida.sule@vision.edu"},
]

DEFAULT_PASSWORD = "Vision@2025"


class Command(BaseCommand):
    help = (
        "Seeds 40 Vision Staff user accounts. "
        "Safe to run multiple times — uses update_or_create on email."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=DEFAULT_PASSWORD,
            help=f"Password to set for each user (default: {DEFAULT_PASSWORD})",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        password = options["password"]
        self.stdout.write(self.style.MIGRATE_HEADING("Seeding Vision Staff users..."))

        created_count = 0
        updated_count = 0

        for data in VISION_STAFF:
            user, created = User.objects.update_or_create(
                email=data["email"],
                defaults={
                    "first_name": data["first_name"],
                    "last_name": data["last_name"],
                    "user_type": User.UserType.CX_STAFF,
                    "status": User.Status.ACTIVE,
                    "is_active": True,
                    "is_staff": True,
                    "school": None,
                    "branch": None,
                },
            )

            if created:
                user.set_password(password)
                user.save(update_fields=["password"])
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [CREATED]  {data['email']:40s} → {data['first_name']} {data['last_name']}"
                    )
                )
            else:
                updated_count += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  [UPDATED]  {data['email']:40s} → {data['first_name']} {data['last_name']}"
                    )
                )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {created_count} created, {updated_count} updated."
            )
        )
