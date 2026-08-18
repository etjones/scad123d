// Exercises scad123d.import_module(): keyword and positional arguments, and
// a default value used when an argument is omitted.
module sized_box(size = [10, 10, 10], rounded = false) {
    if (rounded)
        minkowski() { cube(size, center = true); sphere(r = 1); }
    else
        cube(size, center = true);
}
