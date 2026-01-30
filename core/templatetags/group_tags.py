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
