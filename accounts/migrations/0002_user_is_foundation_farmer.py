from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='is_foundation_farmer',
            field=models.BooleanField(
                default=False,
                help_text=(
                    "The one farmer whose historical DailyLog data seeds every new "
                    "farmer's bootstrap forecasting model (see forecasting/management/"
                    "commands/train_forecast_model.py). At most one user may ever have "
                    "this set — enforced in save() below, not a DB constraint: MySQL "
                    "doesn't support conditional/partial unique constraints (Django "
                    "system check W036), so this can't be expressed as a "
                    "UniqueConstraint the way unique_generation_per_owner is on Flock."
                ),
            ),
        ),
    ]
