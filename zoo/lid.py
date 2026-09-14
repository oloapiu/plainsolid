from plainsolid import *

meta(name="lid", material="abs", revision="A", author="Paolo")

length = 120.0
width = 80.0
thickness = 3.0
wall = 2.5                      # the enclosure's wall
lip = 1.5                       # the enclosure's rebate band; the tongue is the rest of the wall
clearance = param(0.2, "fit around the enclosure's tongue")
corner_r = 6.0
rim_t = 1.5                     # the inner rim's thickness
skirt_h = 2.0                   # how far the outer skirt drops, the depth of the enclosure's rebate
rim_h = 4.0                     # how far the inner rim drops inside the enclosure
screw_d = param(3.2, "M3 clearance")
boss_in = 5.5                   # the enclosure's boss centres from its inner walls

plate_sk = sketch("plate_sk", on=XY)
plate_sk.rect("plate", length, width)
plate = extrude("plate", plate_sk, thickness)
corners = fillet("corners", plate.edges.where(parallel_to="+Z"), corner_r)

# under the plate, an outer skirt that covers the enclosure's rebate and an inner rim that
# drops inside the wall; the enclosure's tongue seats in the channel between them
skirt_sk = sketch("skirt_sk", on=plate.faces.bottom)
skirt_sk.project("outer", plate.edges.bottom, construction=False)
skirt_sk.project("outer_arcs", corners.edges.bottom, construction=False)
skirt_sk.rect("inner", length - 2 * lip + 2 * clearance, width - 2 * lip + 2 * clearance)
skirt = extrude("skirt", skirt_sk, skirt_h)
rim_sk = sketch("rim_sk", on=plate.faces.bottom)
rim_sk.rect("outer", length - 2 * wall - 2 * clearance, width - 2 * wall - 2 * clearance)
rim = extrude("rim", rim_sk, rim_h)
rim_corners = fillet("rim_corners", [rim.edges.from_sketch("outer.left").where(parallel_to="+Z"), rim.edges.from_sketch("outer.right").where(parallel_to="+Z")],
                     corner_r - wall - clearance)   # the slab's corners follow the enclosure's inner corners
pocket_sk = sketch("pocket_sk", on=rim.faces.top)   # the slab's far face: an extrusion's end cap is its top
pocket_sk.rect("inner", length - 2 * wall - 2 * clearance - 2 * rim_t, width - 2 * wall - 2 * clearance - 2 * rim_t)
pocket = cut("pocket", pocket_sk, rim_h)

# four screw holes over the enclosure's bosses, one drawn and patterned
screw_sk = sketch("screw_sk", on=plate.faces.top)
screw_sk.circle("screw", screw_d, at=(-length / 2 + wall + boss_in, -width / 2 + wall + boss_in))
screw = cut("screw", screw_sk, through=True)
screws = linear_pattern("screws", screw, 2, spacing=length - 2 * wall - 2 * boss_in, direction=X,
                        count2=2, spacing2=width - 2 * wall - 2 * boss_in, direction2=Y)

# break the outer top edges
edge_break = chamfer("edge_break", plate.edges.top, 1)
