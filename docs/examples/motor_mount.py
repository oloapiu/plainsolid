from plainsolid import *

meta(name="motor_mount", material="al6061", revision="A")

# A fully constrained mounting profile: bearing bore, four equal bolt holes,
# two equal cooling slots, and a construction circle for the motor envelope.
mount_profile = sketch("mount_profile", on=XY)
mount_profile.slot("outline", 160, 94)
mount_profile.fix("plate_center", "outline.center")
mount_profile.horizontal("plate_axis", "outline.axis")
mount_profile.length("plate_length", "outline.length", 160)
mount_profile.length("plate_width", "outline.width", 94)

mount_profile.circle("bearing", 47)
mount_profile.fix("bearing_center", "bearing.center")
mount_profile.diameter("bearing_diameter", "bearing", 47)
mount_profile.circle("motor_envelope", 76, construction=True)
mount_profile.concentric("motor_center", "motor_envelope", "bearing")
mount_profile.diameter("motor_diameter", "motor_envelope", 76)

mount_profile.circle("bolt_ne", 8.5, at=(52, 22))
mount_profile.circle("bolt_nw", 8.5, at=(-52, 22))
mount_profile.circle("bolt_sw", 8.5, at=(-52, -22))
mount_profile.circle("bolt_se", 8.5, at=(52, -22))
mount_profile.distance("bolt_pitch_x", "bolt_nw.center", "bolt_ne.center", 104, along="x")
mount_profile.distance("bolt_pitch_y", "bolt_se.center", "bolt_ne.center", 44, along="y")
mount_profile.fix("bolt_nw_position", "bolt_nw.center")
mount_profile.fix("bolt_sw_position", "bolt_sw.center")
mount_profile.fix("bolt_se_position", "bolt_se.center")
mount_profile.diameter("bolt_diameter", "bolt_ne", 8.5)
mount_profile.equal("bolt_equal_nw", "bolt_nw", "bolt_ne")
mount_profile.equal("bolt_equal_sw", "bolt_sw", "bolt_ne")
mount_profile.equal("bolt_equal_se", "bolt_se", "bolt_ne")

mount_profile.slot("vent_left", 22, 6, at=(-54, 0), angle=90)
mount_profile.slot("vent_right", 22, 6, at=(54, 0), angle=90)
mount_profile.fix("left_vent_center", "vent_left.center")
mount_profile.fix("right_vent_center", "vent_right.center")
mount_profile.vertical("left_vent_axis", "vent_left.axis")
mount_profile.parallel("vent_axes", "vent_left.axis", "vent_right.axis")
mount_profile.length("vent_length", "vent_left.length", 22)
mount_profile.length("vent_width", "vent_left.width", 6)
mount_profile.equal("vent_lengths", "vent_left.axis", "vent_right.axis")
mount_profile.equal("vent_widths", "vent_left.start_arc", "vent_right.start_arc")

plate = extrude("plate", mount_profile, 8)
