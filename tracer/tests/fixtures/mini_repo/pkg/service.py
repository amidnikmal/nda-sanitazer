"""Processing pipeline: validation and transformation of input data."""


def process(data):
    """Validate then transform the incoming authentication payload."""
    if validate(data):
        return transform(data)
    return None


def validate(data):
    """Check the data passes the length rule."""
    return check_length(data)


def check_length(data):
    return len(data) > 0


def transform(data):
    return data.upper()
