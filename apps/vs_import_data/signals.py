from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import ImportBatch


@receiver(post_delete, sender=ImportBatch)
def delete_import_batch_file(sender, instance, **kwargs):
    if instance.file:
        instance.file.delete(save=False)
