from plainsolid import *

meta(name="board", revision="A", author="Paolo")

# a circuit board on the enclosure's standoffs, with its sensor module and a connector on top
length = 90.0
width = 50.0
thickness = 1.6
hole_d = param(2.7, "M2.5 clearance")
pitch_x = 80.0                  # the enclosure's standoff pitch
pitch_y = 40.0

pcb_sk = sketch("pcb_sk", on=XY)
pcb_sk.rect("pcb", length, width)
pcb = extrude("pcb", pcb_sk, thickness)
corners = fillet("corners", pcb.edges.where(parallel_to="+Z"), 3)

# four mounting holes on the standoff pitch, one drawn and patterned
holes = sketch("holes", on=pcb.faces.top)
holes.circle("hole", hole_d, at=(-pitch_x / 2, -pitch_y / 2))
hole = cut("hole", holes, through=True)
mount_holes = linear_pattern("mount_holes", hole, 2, spacing=pitch_x, direction=X, count2=2, spacing2=pitch_y, direction2=Y)

# the sensor module and the cable connector, as blocks on the top side
sensor_sk = sketch("sensor_sk", on=pcb.faces.top)
sensor_sk.rect("sensor", 24, 16, at=(-18, 0))
sensor = extrude("sensor", sensor_sk, 8)
connector_sk = sketch("connector_sk", on=pcb.faces.top)
connector_sk.rect("connector", 10, 6, at=(28, 14))
connector = extrude("connector", connector_sk, 5)
