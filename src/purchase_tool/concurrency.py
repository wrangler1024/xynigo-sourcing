"""User-selectable concurrency; internal safety caps remain independent."""

CONCURRENCY_CHOICES = (2, 3, 5)
DEFAULT_CONCURRENCY = 2


def require_concurrency(value):
    if type(value) is not int or value not in CONCURRENCY_CHOICES:
        raise ValueError('并发数必须是 2、3、5 中的整数')
    return value


def normalize_concurrency(value):
    """Read old configuration without mutating the persisted file."""
    return value if type(value) is int and value in CONCURRENCY_CHOICES else DEFAULT_CONCURRENCY
