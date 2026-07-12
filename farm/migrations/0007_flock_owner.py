import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('farm', '0006_flock_pending_flock_size'),
    ]

    operations = [
        migrations.AddField(
            model_name='flock',
            name='owner',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='flocks',
                to=settings.AUTH_USER_MODEL,
                help_text='The farmer this flock (and its farm) belongs to.',
            ),
        ),
        migrations.AlterField(
            model_name='flock',
            name='generation_number',
            field=models.PositiveIntegerField(),
        ),
    ]
