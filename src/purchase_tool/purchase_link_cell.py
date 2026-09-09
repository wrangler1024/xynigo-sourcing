"""Verify complete URL values across native Sheets and CLI rich-text cells."""


def _display_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = [_display_text(item) for item in value]
        return None if any(part is None for part in parts) else ''.join(parts)
    if isinstance(value, dict):
        parts = [_display_text(value[key])
                 for key in ('text', 'value', 'rich_text', 'richText')
                 if key in value and value[key] is not None]
        if parts and all(part == parts[0] for part in parts):
            return parts[0]
    return None


def _targets_match(value, expected):
    if isinstance(value, (list, tuple)):
        return all(_targets_match(item, expected) for item in value)
    if not isinstance(value, dict):
        return True
    fields = {str(key).replace('_', '-').casefold(): child
              for key, child in value.items()}
    kind = str(fields.get('type') or fields.get('kind') or '').casefold()
    if fields.get('formula') or 'formula' in kind or 'image' in kind:
        return False
    targets = [fields[key] for key in ('link', 'href', 'url') if fields.get(key)]
    if kind in {'url', 'link'} and not targets:
        return False
    if any(target != expected for target in targets):
        return False
    return all(_targets_match(child, expected) for child in value.values())


def purchase_url_cell_matches(value, expected):
    """Accept text or automatic links only when all visible/target URLs agree.

    Feishu can convert an explicitly written text URL back into a URL segment.
    That representation is safe when its entire text and every target preserve
    the requested URL, including its query and fragment. Never trust a write
    cache, shortened labels, formulas, or a correct label with another target.
    """
    return (isinstance(expected, str) and bool(expected)
            and _display_text(value) == expected
            and _targets_match(value, expected))
