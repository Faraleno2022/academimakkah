from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('salaires', '0014_affectation_unicite_par_matiere')]
    operations = [
        migrations.AddField(
            model_name='etatsalaire', name='heures_cours_corrigees',
            field=models.JSONField(default=dict, blank=True),
        ),
    ]
