from django import template
from synchronisation.realtime import revision_pour_requete

register = template.Library()


@register.simple_tag(takes_context=True)
def sync_page_revision(context):
    request = context.get('request')
    return revision_pour_requete(request) if request is not None else ''
