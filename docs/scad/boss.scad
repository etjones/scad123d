union() {
    cube([20, 20, 10], center = true);
    translate([0, 0, 5]) cylinder(h = 8, r = 6, $fn = 48, center = false);
}
