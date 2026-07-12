from django.db import migrations


def backfill_flock_owner(apps, schema_editor):
    Flock = apps.get_model('farm', 'Flock')
    User = apps.get_model('accounts', 'User')
    # Nothing to backfill on a fresh DB (e.g. the test runner's empty test database) —
    # only look up the foundation farmer, which may not exist yet either, if there's
    # actually an ownerless Flock row that needs one.
    if not Flock.objects.filter(owner__isnull=True).exists():
        return
    foundation_farmer = User.objects.get(is_foundation_farmer=True)
    Flock.objects.filter(owner__isnull=True).update(owner=foundation_farmer)


class Migration(migrations.Migration):

    dependencies = [
        ('farm', '0007_flock_owner'),
        ('accounts', '0003_set_foundation_farmer'),
    ]

    operations = [
        migrations.RunPython(backfill_flock_owner, migrations.RunPython.noop),
    ]
