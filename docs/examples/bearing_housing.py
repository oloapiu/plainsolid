from plainsolid import *

meta(name="bearing_housing", material="al6061", revision="B")

flange_d = param(140.0, "mounting flange diameter")
flange_t = param(10.0, "flange thickness")
hub_d = param(76.0, "bearing housing diameter")
hub_h = param(32.0, "housing height above flange")
bearing_d = param(47.0, "bearing seat diameter")
bolt_d = param(8.5, "M8 clearance")
bolt_pcd = param(116.0, "mounting bolt circle")

flange_profile = sketch("flange_profile", on=XY)
flange_profile.circle("rim", flange_d)
flange = extrude("flange", flange_profile, flange_t)
rim_break = chamfer("rim_break", flange.edges, 1.0)

hub_profile = sketch("hub_profile", on=XY)
hub_profile.circle("hub", hub_d)
hub = extrude("hub", hub_profile, flange_t + hub_h)
hub_round = fillet("hub_round", hub.edges.top, 2.0)

bearing_seat = sketch("bearing_seat", on=hub.faces.top)
bearing_seat.circle("bearing", bearing_d)
seat = cut("seat", bearing_seat, 24.0)
shaft_profile = sketch("shaft_profile", on=XY)
shaft_profile.circle("shaft", 32.0)
shaft_bore = cut("shaft_bore", shaft_profile, through=True)

bolt_profile = sketch("bolt_profile", on=XY)
bolt_profile.circle("bolt", bolt_d, at=(bolt_pcd / 2, 0))
bolt_hole = cut("bolt_hole", bolt_profile, through=True)
mounting_holes = circular_pattern("mounting_holes", bolt_hole, 8, axis=Z)

mounting_plane = plane("mounting_plane", XY, offset=flange_t)
counterbore_profile = sketch("counterbore_profile", on=mounting_plane)
counterbore_profile.circle("head", 14.0, at=(bolt_pcd / 2, 0))
head_recess = cut("head_recess", counterbore_profile, 3.0, flip=True)
head_recesses = circular_pattern("head_recesses", head_recess, 8, axis=Z)

relief_profile = sketch("relief_profile", on=XY)
relief_profile.slot("vent", 19.0, 6.0, at=(44.3462, 18.3688), angle=112.5)
relief = cut("relief", relief_profile, through=True)
relief_ring = circular_pattern("relief_ring", relief, 8, axis=Z)
