from django import template

register = template.Library()

@register.filter
def has_group_id(user, group_id):
    """Returns True if the user belongs to the group with given numeric id."""
    if not getattr(user, "is_authenticated", False):
        return False
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        return False
    return user.groups.filter(id=group_id).exists()


@register.simple_tag(takes_context=True)
def show_ads_link(context):
    """Devuelve True si se debe mostrar el link de Ads/Pauta en el sidebar.
    - Preview local (DEBUG=True + localhost, usuario anónimo): True.
    - Cualquier otro caso: solo si el usuario es admin según _is_admin_user.
    """
    from django.conf import settings
    from core.views import _is_admin_user, _is_local_request
    request = context.get("request")
    if request is None:
        return False
    if settings.DEBUG and _is_local_request(request):
        return True
    return _is_admin_user(request.user)
