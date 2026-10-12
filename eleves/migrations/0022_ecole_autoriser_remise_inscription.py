from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('eleves', '0021_collecte_eleves_enseignants')]
    operations = [
        migrations.AddField(
            model_name='ecole',
            name='autoriser_remise_inscription',
            field=models.BooleanField(
                default=False,
                verbose_name="Autoriser les remises sur l'inscription et la réinscription",
                help_text="Permet de sélectionner les frais d'inscription ou de réinscription lors de l'application d'une remise.",
            ),
        ),
    ]
