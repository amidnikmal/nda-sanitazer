"""Entry point of the mini fixture app."""

from pkg.service import process


def main():
    """Load input and run it through the processing pipeline."""
    data = load_input()
    return process(data)


def load_input():
    # read the raw input file
    return read_file("in.txt")


def read_file(path):
    with open(path) as fh:
        return fh.read()
