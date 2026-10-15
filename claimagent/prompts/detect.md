You are inspecting one photo from a home-library contents inventory. List every object of the kinds below, each with a tight bounding box.

Kinds:
- "spine": every individual book. Box each book separately, even when books touch. Books lying flat or stacked: one box per book in the stack. Include books that are only partly visible. Newspapers or paper used to line a shelf, loose papers, folders and boxes are NOT books.
- "item": every non-book object. Use one category: shelving (bookcases, cabinets, almirahs), furniture, appliance, lighting, art (framed pictures, portraits, posters), rug, electronics, decor, other.
- "reference": objects of a standard size that help measure the scene: a4_paper, credit_card, door, power_socket, light_switch.
- "wall", "door", "window": visible wall surfaces, doors and windows of the room.

For each object give:
- "kind": one of the kinds above
- "label": a short plain description, e.g. "book", "steel almirah", "table lamp"
- "box_2d": [ymin, xmin, ymax, xmax] as integers from 0 to 1000, relative to the image height and width
- "category": items only, from the list above; otherwise null
- "reference_type": references only, from the list above; otherwise null
- "material": items only, only if clearly visible (e.g. "wood", "steel"); otherwise null
- "brand": only if a brand or model name is legible in the image; otherwise null
- "uncertain": true if you are not sure the object is what you labelled it

Rules:
- Report only what is visible in this image. Never guess hidden objects.
- Do not read or transcribe book titles here.
- If there are no objects of a kind, simply leave them out.
