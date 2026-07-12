import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('farm', '0008_backfill_flock_owner'),
    ]

    operations = [
        migrations.AlterField(
            model_name='flock',
            name='owner',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='flocks',
                to=settings.AUTH_USER_MODEL,
                help_text='The farmer this flock (and its farm) belongs to.',
            ),
        ),
        migrations.AddConstraint(
            model_name='flock',
            constraint=models.UniqueConstraint(
                fields=('owner', 'generation_number'), name='unique_generation_per_owner'
            ),
        ),
    ]
