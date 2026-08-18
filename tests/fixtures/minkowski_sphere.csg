minkowski(convexity = 0) {
	cube(size = [20, 15, 10], center = true);
	sphere($fn = 0, $fa = 12, $fs = 2, r = 3);
}
multmatrix([[1, 0, 0, 60], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {
	minkowski(convexity = 0) {
		union() {
			cube(size = [16, 16, 6], center = true);
			cylinder($fn = 0, $fa = 12, $fs = 2, h = 16, r1 = 4, r2 = 4, center = true);
		}
		sphere($fn = 0, $fa = 12, $fs = 2, r = 2);
	}
}

