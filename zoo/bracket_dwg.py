from plainsolid import *

meta(kind="drawing", name="bracket_dwg", of="bracket.py", sheet="A4", revision="A", author="Paolo")

# views: third-angle layout, an isometric at a smaller scale, a section through hole1
front = view("front", direction=FRONT, at=(65, 140))
top = view("top", direction=TOP, at=(65, 62))
right = view("right", direction=RIGHT, at=(150, 140))
iso = view("iso", direction=ISO, at=(240, 140), scale=0.75, hidden=False)
section = view("section", section=YZ, offset=-10, at=(150, 66))

# dimensions reference the model's labelled entities through the view they sit in;
# `at` is relative to the view's centre
dimension("width", front.edges.of("body").bottom.from_sketch("outer_wall"), front.edges.of("body").bottom.from_sketch("right"), at=(0, -38))
dimension("height", front.edges.of("body").bottom.from_sketch("bottom"), front.edges.of("body").bottom.from_sketch("top"), at=(-42, 0))
dimension("fillet", front.faces.of("inner"), at=(-10, -8), kind="radius")
dimension("chamfer", front.faces.of("outer"), front.faces.of("body").from_sketch("bottom"), at=(-36, -22), kind="angle")
dimension("depth", top.edges.of("body").top.from_sketch("bottom"), top.edges.of("body").bottom.from_sketch("bottom"), at=(42, 0))
dimension("pitch", top.faces.of("hole_cut").from_sketch("hole1"), top.faces.of("hole_cut").from_sketch("hole2"), at=(0, 27), along="x")
dimension("hole", top.faces.of("hole_cut").from_sketch("hole1"), at=(-24, 16), text="2× Ø5")
dimension("wall", top.faces.of("body").from_sketch("outer_wall"), top.faces.of("body").from_sketch("inner_wall"), at=(-28, 27))
dimension("base", section.faces.of("body").from_sketch("bottom"), section.faces.of("body").from_sketch("inner_bottom"), at=(26, -20))

note("finish", "Break all edges 0.5 mm\nAnodize clear", at=(12, 28))
