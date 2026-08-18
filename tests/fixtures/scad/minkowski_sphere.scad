// Rung 1: analytic. Both of these become offset(..., kind=ARC).
minkowski() { cube([20, 15, 10], center = true); sphere(r = 3); }
translate([60, 0, 0]) minkowski() {
    union() { cube([16, 16, 6], center = true); cylinder(h = 16, r = 4, center = true); }
    sphere(r = 2);
}
