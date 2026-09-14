from plainsolid import *

meta(name="bracket", material="al6061", revision="A", author="Paolo")

# parameters
thickness = 4.0
width = 60.0        # along X
base_depth = 40.0   # along Y
height = 50.0       # along Z
hole_d = param(5.0, "clearance hole for M5")
slot_spacing = 24.0

# L profile on the XZ plane, drawn loosely and pinned down by constraints; the inner
# corner is rounded and the outer one bevelled from the edges the profile lines made
profile = sketch("profile", on=XZ)
profile.line("bottom", (-30, 0), (30, 0))
profile.line("right", (30, 0), (30, 4))
profile.line("inner_bottom", (30, 4), (-26, 4))
profile.line("inner_wall", (-26, 4), (-26, 50))
profile.line("top", (-26, 50), (-30, 50))
profile.line("outer_wall", (-30, 50), (-30, 0))
profile.coincident("c1", "bottom.end", "right.start")
profile.coincident("c2", "right.end", "inner_bottom.start")
profile.coincident("c3", "inner_bottom.end", "inner_wall.start")
profile.coincident("c4", "inner_wall.end", "top.start")
profile.coincident("c5", "top.end", "outer_wall.start")
profile.coincident("c6", "outer_wall.end", "bottom.start")
profile.horizontal("h1", "bottom")
profile.horizontal("h2", "inner_bottom")
profile.horizontal("h3", "top")
profile.vertical("v1", "right")
profile.vertical("v2", "inner_wall")
profile.vertical("v3", "outer_wall")
profile.fix("origin", "bottom.start")
profile.length("w", "bottom", width)
profile.length("t", "right", thickness)
profile.length("h", "outer_wall", height)
profile.equal("wall_t", "top", "right")
body = extrude("body", profile, base_depth)
inner = fillet("inner", body.edges.from_sketch("inner_bottom").from_sketch("inner_wall"), 3)
outer = chamfer("outer", body.edges.from_sketch("bottom").from_sketch("outer_wall"), 2)

# two mounting holes through the base, placed from the projected base edges
holes = sketch("holes", on=XY)
holes.project("back_edge", body.edges.where(parallel_to="+X").nearest((0, 0, 0)))
holes.project("side_edge", body.edges.where(parallel_to="+Y").nearest((30, -20, 0)))
holes.circle("hole1", hole_d, at=(-10, -20))
holes.circle("hole2", hole_d, at=(20, -20))
holes.diameter("d1", "hole1", hole_d)
holes.equal("same", "hole1", "hole2")
holes.distance("y1", "hole1.center", "back_edge", base_depth / 2)
holes.distance("y2", "hole2.center", "back_edge", base_depth / 2)
holes.distance("x1", "hole1.center", "side_edge", 40)
holes.distance("x2", "hole2.center", "side_edge", 10)
cut("hole_cut", holes, through=True)

# two vertical slots through the wall, sized by their overall length and width and
# placed from the wall's top edge and the base's far edge
slots = sketch("slots", on=YZ)
slots.project("wall_top", body.edges.from_sketch("top").from_sketch("outer_wall"))
slots.project("wall_end", body.edges.top.from_sketch("outer_wall"))
slots.slot("slot1", 16, 5, at=(-32, 30), angle=90)
slots.slot("slot2", 16, 5, at=(-8, 30), angle=90)
slots.vertical("upright1", "slot1.axis")
slots.length("slot_len", "slot1.length", 16)
slots.length("slot_w", "slot1.width", 5)
slots.distance("down", "slot1.center", "wall_top", 20)
slots.distance("in", "slot1.center", "wall_end", 8)
slots.distance("pitch", "slot1.center", "slot2.center", slot_spacing, along="x")
slots.distance("level", "slot1.center", "slot2.center", 0, along="y")
slots.equal("same_length", "slot1.axis", "slot2.axis")
slots.equal("same_width", "slot1.start_arc", "slot2.start_arc")
slots.parallel("upright", "slot1.axis", "slot2.axis")
cut("slot_cut", slots, through=True, flip=True)
