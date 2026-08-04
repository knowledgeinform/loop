from django import template

register = template.Library()


@register.filter
def compact_number(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return value
    if n >= 1000:
        formatted = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}k"
    return str(n)
