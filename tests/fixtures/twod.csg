linear_extrude(height = 4, $fn = 0, $fa = 12, $fs = 2) {
	polygon(points = [[0, 0], [20, 0], [20, 20], [0, 20], [5, 5], [15, 5], [15, 15], [5, 15]], paths = [[0, 1, 2, 3], [4, 5, 6, 7]], convexity = 1);
}
multmatrix([[1, 0, 0, 30], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {
	linear_extrude(height = 4, $fn = 0, $fa = 12, $fs = 2) {
		difference() {
			square(size = [20, 20], center = false);
			multmatrix([[1, 0, 0, 10], [0, 1, 0, 10], [0, 0, 1, 0], [0, 0, 0, 1]]) {
				circle($fn = 0, $fa = 12, $fs = 2, r = 6);
			}
		}
	}
}

