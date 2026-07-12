from django.db import migrations


def set_foundation_farmer(apps, schema_editor):
    # This is the one place in the whole codebase the founding farmer is identified by
    # literal username — everywhere else in application code, use
    # User.get_foundation_farmer() / User.is_foundation_farmer instead.
    User = apps.get_model('accounts', 'User')
    User.objects.filter(username='Mario').update(is_foundation_farmer=True)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_user_is_foundation_farmer'),
    ]

    operations = [
        migrations.RunPython(set_foundation_farmer, migrations.RunPython.noop),
    ]
