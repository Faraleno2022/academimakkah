from django.core.validators import RegexValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('eleves', '0022_ecole_autoriser_remise_inscription')]
    operations = [
        migrations.AddField(
            model_name='responsable', name='telephone_secondaire',
            field=models.CharField(max_length=20, blank=True, default='',
                validators=[RegexValidator(r'^\+224\d{8,9}$', 'Format: +224XXXXXXXXX')],
                verbose_name='Deuxième téléphone'),
        ),
    ]
