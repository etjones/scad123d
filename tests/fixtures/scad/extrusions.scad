linear_extrude(height = 10) square([20, 12]);
translate([40, 0, 0]) linear_extrude(height = 10, center = true) circle(r = 8);
translate([80, 0, 0]) linear_extrude(height = 10, scale = 0.4) square([16, 16], center = true);
translate([0, 40, 0]) rotate_extrude() translate([12, 0]) circle(r = 3);
translate([50, 40, 0]) rotate_extrude(angle = 270) translate([12, 0]) square([4, 8]);
