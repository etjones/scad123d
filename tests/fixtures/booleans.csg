difference() {
	cube(size = [30, 20, 10], center = true);
	cylinder($fn = 0, $fa = 12, $fs = 2, h = 40, r1 = 4, r2 = 4, center = true);
}
multmatrix([[1, 0, 0, 50], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {
	intersection() {
		cube(size = [20, 20, 20], center = true);
		sphere($fn = 0, $fa = 12, $fs = 2, r = 13);
	}
}
multmatrix([[1, 0, 0, 100], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {
	union() {
		cube(size = [20, 20, 5], center = true);
		cylinder($fn = 0, $fa = 12, $fs = 2, h = 20, r1 = 4, r2 = 4, center = true);
	}
}

