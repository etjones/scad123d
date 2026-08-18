// $fn below the threshold is intentional geometry: these are real polygons.
linear_extrude(height = 5) circle(r = 10, $fn = 6);
translate([30, 0, 0]) cylinder(h = 10, r = 8, $fn = 8);
translate([60, 0, 0]) cylinder(h = 10, r1 = 8, r2 = 3, $fn = 5);
translate([90, 0, 0]) cylinder(h = 10, r1 = 8, r2 = 0, $fn = 4);
// at or above the threshold, a complexity switch: exact curves
translate([0, 40, 0]) cylinder(h = 10, r = 8, $fn = 64);
