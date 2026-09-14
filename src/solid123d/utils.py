"""Equivalents of the common ``solid.utils`` directional helpers."""

from collections.abc import Callable

from build123d import Shape

from .transforms import translate

Applier = Callable[..., Shape]


def up(z: float) -> Applier:
    return translate([0, 0, z])


def down(z: float) -> Applier:
    return translate([0, 0, -z])


def right(x: float) -> Applier:
    return translate([x, 0, 0])


def left(x: float) -> Applier:
    return translate([-x, 0, 0])


def forward(y: float) -> Applier:
    return translate([0, y, 0])


def back(y: float) -> Applier:
    return translate([0, -y, 0])
