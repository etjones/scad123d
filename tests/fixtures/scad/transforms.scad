// rigid transforms must preserve analytic surfaces; non-uniform scale must not.
translate([10, 0, 0]) cube([8, 6, 4]);
rotate([0, 0, 30]) translate([0, 20, 0]) cube([8, 6, 4]);
rotate([45, 0, 0]) translate([0, 40, 0]) cylinder(h = 10, r = 3);
mirror([1, 0, 0]) translate([20, 60, 0]) cube([8, 6, 4]);
scale([2, 1, 1]) translate([20, 80, 0]) cube([8, 6, 4]);
translate([60, 0, 0]) scale([1, 2, 3]) sphere(r = 4);
