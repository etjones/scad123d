// Rung 2: hull() of 8 equal-radius spheres at box corners -> exact analytic
// offset(convex_hull_of_centers, r).
hull() {
    translate([-10,-7.5,-5]) sphere(r=3);
    translate([10,-7.5,-5]) sphere(r=3);
    translate([-10,7.5,-5]) sphere(r=3);
    translate([10,7.5,-5]) sphere(r=3);
    translate([-10,-7.5,5]) sphere(r=3);
    translate([10,-7.5,5]) sphere(r=3);
    translate([-10,7.5,5]) sphere(r=3);
    translate([10,7.5,5]) sphere(r=3);
}
