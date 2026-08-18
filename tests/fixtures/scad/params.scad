// exercises -D overrides
width  = 20;
height = 10;
holes  = 3;
difference() {
    cube([width, 15, height]);
    for (i = [0 : holes - 1])
        translate([width * (i + 0.5) / holes, 7.5, -1])
            cylinder(h = height + 2, r = 2);
}
