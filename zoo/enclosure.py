from plainsolid import *

meta(name="enclosure", material="abs", revision="A", author="Paolo")

# parameters
length = 120.0
width = 80.0
height = 40.0
wall = param(2.5, "wall thickness")
corner_r = 6.0
lip = 1.5                       # the outer band of the rim that steps down; the rest is the tongue
boss_d = 7.0
boss_in = 5.5                   # boss centres from the inner walls
screw_d = param(2.5, "M3 self-tapping pilot")
gland_d = param(12.5, "M12 cable gland clearance")
antenna_d = 6.5                 # SMA bulkhead clearance
board_x = 80.0                  # the circuit board's mounting hole pitch
board_y = 40.0

# the box: a rectangle extruded, its vertical corners rounded, then hollowed from the top
outline = sketch("outline", on=XY)
outline.rect("box", length, width)
body = extrude("body", outline, height)
corners = fillet("corners", body.edges.where(parallel_to="+Z"), corner_r)
hollow = shell("hollow", body.faces.top, wall)

# a tongue around the rim: the outer band of the wall steps down, the inner part stands
# proud and seats in the lid's channel
lip_sk = sketch("lip_sk", on=body.faces.top)
lip_sk.project("outer", body.edges.top, construction=False)
lip_sk.project("outer_arcs", corners.edges.top, construction=False)
lip_sk.rect("inner", length - 2 * lip, width - 2 * lip)
rebate = cut("rebate", lip_sk, 2)

# four lid-screw bosses in the corners, one drawn on the floor and patterned, each with a pilot hole
boss_sk = sketch("boss_sk", on=hollow.faces.bottom.inner)
boss_sk.circle("boss", boss_d, at=(-length / 2 + wall + boss_in, -width / 2 + wall + boss_in))
boss = extrude("boss", boss_sk, height - wall - 4)
bosses = linear_pattern("bosses", boss, 2, spacing=length - 2 * wall - 2 * boss_in, direction=X,
                        count2=2, spacing2=width - 2 * wall - 2 * boss_in, direction2=Y)
pilot_sk = sketch("pilot_sk", on=boss.faces.top)
pilot_sk.circle("pilot", screw_d)
pilot = cut("pilot", pilot_sk, 10)
pilots = linear_pattern("pilots", pilot, 2, spacing=length - 2 * wall - 2 * boss_in, direction=X,
                        count2=2, spacing2=width - 2 * wall - 2 * boss_in, direction2=Y)

# four standoffs on the floor for the circuit board, on its hole pitch
standoff_sk = sketch("standoff_sk", on=hollow.faces.bottom.inner)
standoff_sk.circle("standoff", 6, at=(-board_x / 2, -board_y / 2))
standoff = extrude("standoff", standoff_sk, 5)
standoffs = linear_pattern("standoffs", standoff, 2, spacing=board_x, direction=X, count2=2, spacing2=board_y, direction2=Y)
standoff_pilot_sk = sketch("standoff_pilot_sk", on=standoff.faces.top)
standoff_pilot_sk.circle("standoff_pilot", screw_d)
standoff_pilot = cut("standoff_pilot", standoff_pilot_sk, 4)
standoff_pilots = linear_pattern("standoff_pilots", standoff_pilot, 2, spacing=board_x, direction=X,
                                 count2=2, spacing2=board_y, direction2=Y)
board_plane = plane("board_plane", XY, offset=wall + 5)   # the board's underside, for sections and drawings

# two cable glands through the back wall, placed from the wall's projected floor edge
glands = sketch("glands", on=body.faces.from_sketch("box.top"))
glands.project("floor", body.edges.bottom.from_sketch("box.top"))
glands.circle("gland1", gland_d, at=(30, 0))
glands.circle("gland2", gland_d, at=(-30, 0))
glands.equal("same", "gland1", "gland2")
glands.distance("up1", "gland1.center", "floor", 20)
glands.distance("up2", "gland2.center", "floor", 20)
glands.distance("pitch", "gland1.center", "gland2.center", 60, along="x")
glands.distance("centre", "gland1.center", "floor.mid", 30, along="x")
gland_cut = cut("gland_cut", glands, upto=hollow.faces.inner.from_sketch("box.top"))

# the antenna's bulkhead connector through the right wall, on the mid plane at the glands' height
antenna_sk = sketch("antenna_sk", on=body.faces.from_sketch("box.right"))
antenna_sk.project("floor", body.edges.bottom.from_sketch("box.right"))
antenna_sk.circle("antenna", antenna_d, at=(0, 20))
antenna_sk.distance("up", "antenna.center", "floor", 20)
antenna_sk.distance("mid", "antenna.center", "floor.mid", 0, along="x")
antenna_cut = cut("antenna_cut", antenna_sk, upto=hollow.faces.inner.from_sketch("box.right"))

# a row of vent slots low on the front wall, one drawn and placed from the floor edge, then patterned
vent_sk = sketch("vent_sk", on=body.faces.from_sketch("box.bottom"))
vent_sk.project("floor", body.edges.bottom.from_sketch("box.bottom"))
vent_sk.slot("vent", 12, 2.5, at=(-35, 8))
vent_sk.horizontal("level", "vent.axis")
vent_sk.distance("up", "vent.center", "floor", 8)
vent_sk.distance("in", "floor.mid", "vent.center", 35, along="x")
vent = cut("vent", vent_sk, upto=hollow.faces.inner.from_sketch("box.bottom"))
vents = linear_pattern("vents", vent, 6, spacing=14, direction=X)
