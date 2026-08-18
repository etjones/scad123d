// Rung 2: hull() of 2 (collinear) equal-radius spheres -> exact capsule.
hull() {
    translate([-7,0,0]) sphere(r=3);
    translate([7,0,0]) sphere(r=3);
}
