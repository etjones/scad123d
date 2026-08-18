// 2D with a hole via paths, then extruded so the result is a solid
linear_extrude(height = 4)
polygon(
    points = [[0,0],[20,0],[20,20],[0,20], [5,5],[15,5],[15,15],[5,15]],
    paths  = [[0,1,2,3], [4,5,6,7]]
);
translate([30, 0, 0]) linear_extrude(height = 4) difference() {
    square([20, 20]);
    translate([10, 10]) circle(r = 6);
}
