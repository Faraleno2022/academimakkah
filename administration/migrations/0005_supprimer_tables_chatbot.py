"""Nettoyage après le retrait du module chatbot.

L'application ``chatbot`` n'est plus installée : ses propres migrations ne
peuvent donc plus supprimer ses tables. Cette migration le fait à sa place,
sur les bases qui les contiennent encore (SQLite des postes Desktop comme
MySQL en ligne), puis efface les traces de l'application (types de contenu,
permissions, historique de migrations). Sans effet sur une base neuve.
"""
from django.db import migrations
from django.db.migrations.recorder import MigrationRecorder

# Tables enfants avant les tables parentes (contraintes de clés étrangères MySQL).
TABLES_CHATBOT = [
    'chatbot_messagechat_documents_references',
    'chatbot_messagechat',
    'chatbot_conversationchat',
    'chatbot_recherchepopulaire',
    'chatbot_documentcours',
    'chatbot_matiere',
]


def supprimer_chatbot(apps, schema_editor):
    connexion = schema_editor.connection
    existantes = set(connexion.introspection.table_names())
    for table in TABLES_CHATBOT:
        if table in existantes:
            schema_editor.execute(f'DROP TABLE {schema_editor.quote_name(table)}')

    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    LogEntry = apps.get_model('admin', 'LogEntry')
    alias = connexion.alias
    types = ContentType.objects.using(alias).filter(app_label='chatbot')
    LogEntry.objects.using(alias).filter(content_type__in=types).update(content_type=None)
    Permission.objects.using(alias).filter(content_type__in=types).delete()
    types.delete()

    MigrationRecorder(connexion).migration_qs.filter(app='chatbot').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('administration', '0004_corbeilleelement'),
        ('admin', '0003_logentry_add_action_flag_choices'),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    # MySQL ne sait pas annuler un DROP TABLE : Django y refuse tout DDL dans
    # une transaction. L'opération tourne donc hors transaction ; chaque étape
    # est rejouable (tables testées avant suppression, filtres sur 'chatbot').
    atomic = False

    operations = [
        migrations.RunPython(supprimer_chatbot, migrations.RunPython.noop, atomic=False),
    ]
