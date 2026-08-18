difference() {
    cube([30, 20, 10], center = true);
    cylinder(h = 40, r = 4, center = true);
}
translate([50, 0, 0]) intersection() {
    cube([20, 20, 20], center = true);
    sphere(r = 13);
}
translate([100, 0, 0]) union() {
    cube([20, 20, 5], center = true);
    cylinder(h = 20, r = 4, center = true);
}
