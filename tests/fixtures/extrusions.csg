linear_extrude(height = 10, $fn = 0, $fa = 12, $fs = 2) {
	square(size = [20, 12], center = false);
}
multmatrix([[1, 0, 0, 40], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {
	linear_extrude(height = 10, center = true, $fn = 0, $fa = 12, $fs = 2) {
		circle($fn = 0, $fa = 12, $fs = 2, r = 8);
	}
}
multmatrix([[1, 0, 0, 80], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {
	linear_extrude(height = 10, scale = 0.4, $fn = 0, $fa = 12, $fs = 2) {
		square(size = [16, 16], center = true);
	}
}
multmatrix([[1, 0, 0, 0], [0, 1, 0, 40], [0, 0, 1, 0], [0, 0, 0, 1]]) {
	rotate_extrude(angle = 360, start = 180, convexity = 2, $fn = 0, $fa = 12, $fs = 2) {
		multmatrix([[1, 0, 0, 12], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {
			circle($fn = 0, $fa = 12, $fs = 2, r = 3);
		}
	}
}
multmatrix([[1, 0, 0, 50], [0, 1, 0, 40], [0, 0, 1, 0], [0, 0, 0, 1]]) {
	rotate_extrude(angle = 270, start = 0, convexity = 2, $fn = 0, $fa = 12, $fs = 2) {
		multmatrix([[1, 0, 0, 12], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {
			square(size = [4, 8], center = false);
		}
	}
}

