cube([20, 15, 10]);
translate([30, 0, 0]) cube([10, 10, 10], center = true);
translate([0, 30, 0]) sphere(r = 8);
translate([30, 30, 0]) cylinder(h = 12, r = 5);
translate([60, 0, 0]) cylinder(h = 12, r1 = 6, r2 = 2);
translate([60, 30, 0]) cylinder(h = 12, r1 = 6, r2 = 0);
