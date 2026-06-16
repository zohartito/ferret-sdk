// =====================================================================
// Ferret SDK -- 3D-scanning turntable platter (parametric)
// =====================================================================
// A 3D-printable platter that sits on a 100 mm lazy-susan bearing and
// holds a removable marker ring inside a raised outer ledge. The top is
// kept flat/matte (no center bore) so objects sit on a clean surface.
//
// Render STL:  openscad -o turntable.stl turntable.scad
// Render PNG:  openscad --render -o turntable.png turntable.scad
// ---------------------------------------------------------------------

// ---- Parametric variables (edit these) ------------------------------
platter_diameter   = 200;   // overall outer diameter (mm)
platter_thickness  = 8;     // thickness of the main flat disc (mm)

bearing_diameter   = 100;   // lazy-susan bearing pilot diameter (mm)
bearing_recess     = 3;     // depth of the underside recess that seats it (mm)
                            // floor left under recess = platter_thickness - bearing_recess

ledge_width        = 6;     // radial width of the raised outer ledge (mm)
ledge_height       = 4;     // how far the ledge stands above the top face (mm)

edge_chamfer       = 1.5;   // chamfer/fillet on exposed outer corners (mm)
$fn                = 200;   // facet count for circles (smoothness)

// ---- Derived values --------------------------------------------------
platter_r     = platter_diameter / 2;
bearing_r     = bearing_diameter / 2;
ledge_inner_r = platter_r - ledge_width;          // inner wall of the ledge
top_z         = platter_thickness;                 // height of the flat top face

// ---------------------------------------------------------------------
// disc_profile: 2D cross-section (in the XZ half-plane, X = radius) of the
// main flat disc, with its top-outer and bottom-outer corners chamfered so
// the printed rim is not a sharp edge. rotate_extrude sweeps it into a disc.
// ---------------------------------------------------------------------
module disc_profile() {
    c = edge_chamfer;
    r = platter_r;
    t = platter_thickness;
    polygon(points = [
        [0,     0],          // center, bottom
        [r - c, 0],          // bottom, near outer
        [r,     c],          // chamfer bottom-outer
        [r,     t - c],      // up the outer wall
        [r - c, t],          // chamfer top-outer
        [0,     t],          // center, top
    ]);
}

// ---------------------------------------------------------------------
// ledge_profile: 2D cross-section of the raised retaining ledge that sits
// on top of the disc rim. Inner wall is vertical (the marker ring drops
// against it); the top-inner and top-outer corners are chamfered.
// ---------------------------------------------------------------------
module ledge_profile() {
    c  = edge_chamfer;
    ri = ledge_inner_r;      // inner radius of the ledge
    ro = platter_r;          // outer radius (flush with the disc rim)
    h  = ledge_height;       // rise above the top face
    z0 = top_z;              // starts at the disc top face
    polygon(points = [
        [ri,     z0],            // inner wall, bottom
        [ro,     z0],            // outer wall, bottom
        [ro,     z0 + h - c],    // up the outer wall
        [ro - c, z0 + h],        // chamfer top-outer
        [ri + c, z0 + h],        // top face
        [ri,     z0 + h - c],    // chamfer top-inner
    ]);
}

// ---------------------------------------------------------------------
// Main platter solid
// ---------------------------------------------------------------------
module platter() {
    difference() {
        union() {
            rotate_extrude($fn = $fn) disc_profile();   // flat disc
            rotate_extrude($fn = $fn) ledge_profile();  // raised ledge
        }

        // Bearing recess on the underside: a flat-bottomed pocket that
        // pilots the 100 mm lazy-susan, leaving (thickness - recess) of floor.
        translate([0, 0, -0.01])
            cylinder(h = bearing_recess + 0.01, r = bearing_r, $fn = $fn);
    }
}

platter();

// Sanity echoes (printed by openscad, harmless in render):
echo(str("outer_diameter_mm=", platter_diameter));
echo(str("total_height_mm=", platter_thickness + ledge_height));
echo(str("floor_under_bearing_mm=", platter_thickness - bearing_recess));
echo(str("marker_ring_seat_inner_dia_mm=", 2 * ledge_inner_r));
