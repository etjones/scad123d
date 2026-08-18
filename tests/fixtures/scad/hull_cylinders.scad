// Rung 2: hull() of 4 equal-radius, equal-span parallel cylinders (the
// "rounded box from corner posts" idiom) -> 2D hull of axis points, offset,
// extruded.
hull() {
    translate([-10,-7.5,0]) cylinder(h=20, r=3, center=true);
    translate([10,-7.5,0]) cylinder(h=20, r=3, center=true);
    translate([-10,7.5,0]) cylinder(h=20, r=3, center=true);
    translate([10,7.5,0]) cylinder(h=20, r=3, center=true);
}
