from plainsolid import *

# An outdoor sensor node: the zoo's enclosure with its lid and the circuit board on the
# standoffs inside, the bracket hung on the left wall, two vendor cable glands screwed
# into the back wall, an antenna on the right wall and a solar panel hinged on the lid's
# front edge.
meta(kind="assembly", name="node", revision="A", author="Paolo")

tilt = param(30.0, "solar panel angle from the lid")
antenna_height = 20.0

box = instance("box", "enclosure.py")
lid = instance("lid", "lid.py", color="#d8d8d0")
board = instance("board", "board.py", color="#2f7a3f", density=1.85)
bracket = instance("bracket", "bracket.py", rotate=(0, 0, 180))
gland1 = instance("gland1", "vendor/gland_m12.step", material="nylon")
gland2 = instance("gland2", "vendor/gland_m12.step", material="nylon")
antenna = instance("antenna", "vendor/antenna.step", material="abs")
panel = instance("panel", "vendor/panel.step", material="glass", rotate=(30, 0, 0))

fixed("anchor", box)

# the circuit board sits on the standoffs, centred in the box
coincident("board_down", board.faces.of("pcb").bottom, box.faces.of("standoff").top)
coincident("board_x", board.planes.YZ, box.planes.YZ)
coincident("board_y", board.planes.XZ, box.planes.XZ)

# the lid sits on the rim, centred on the box
coincident("lid_down", lid.faces.of("plate").bottom.largest(), box.faces.of("body").top)
coincident("lid_x", lid.planes.YZ, box.planes.YZ)
coincident("lid_y", lid.planes.XZ, box.planes.XZ)

# the bracket hangs on the left wall, flush with the floor, its back on the mid plane
coincident("bracket_wall", bracket.faces.of("body").from_sketch("outer_wall"), box.faces.of("body").from_sketch("box.left"))
coincident("bracket_floor", bracket.faces.of("body").from_sketch("bottom"), box.faces.of("body").bottom, flip=True)
coincident("bracket_mid", bracket.planes.XZ, box.planes.XZ, flip=True)

# the glands screw into the back wall: on the axis of their hole, the nut seated on the wall
concentric("gland1_axis", gland1.faces.where(kind="cylinder").largest(), box.faces.of("gland_cut").from_sketch("gland1"))
coincident("gland1_seat", gland1.faces.where(normal="-Z").nearest((0, 0, 0)), box.faces.of("body").from_sketch("box.top"))
concentric("gland2_axis", gland2.faces.where(kind="cylinder").largest(), box.faces.of("gland_cut").from_sketch("gland2"))
coincident("gland2_seat", gland2.faces.where(normal="-Z").nearest((0, 0, 0)), box.faces.of("body").from_sketch("box.top"))

# the antenna stands off the right wall, on the mid plane, a set height above the floor
coincident("antenna_seat", antenna.faces.where(normal="-Z").largest(), box.faces.of("body").from_sketch("box.right"))
coincident("antenna_mid", antenna.axes.Z, box.planes.XZ)
distance("antenna_up", antenna.axes.Z, box.planes.XY, antenna_height)

# the panel hinges on the lid's front top edge, tilted, centred on the box
coincident("panel_hinge", panel.edges.where(parallel_to="+X").nearest((0, -50, 0)),
           lid.edges.of("edge_break").from_sketch("plate.bottom").where(parallel_to="+X").nearest((0, -39, 50)))  # the upper of the chamfer's two long edges
angle("panel_tilt", panel.faces.where(normal="-Z").largest(), lid.faces.of("plate").top, tilt)
distance("panel_mid", panel.vertices.nearest((75, -50, 0)), box.planes.YZ, 75)
