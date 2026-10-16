You are reading book spines for a home-library contents inventory. Each image shows exactly one book's spine, and carries its number in the top-left corner; the images are given in that order, starting at 1. Each image holds the same spine two or three times, rotated differently, so that one copy reads the right way up: read the copy whose text is upright and ignore the others. Different words may be upright in different copies (for example a title running along the spine and an author printed across it); combine them. Read every image on its own; text from one image never belongs to another.

For each image, report only text you can actually see on that book:
- "index": the image number
- "legible": true only if you can read at least the main title words with confidence. Blurry, tiny, cut-off or glare-covered text is not legible.
- "text": every word you can read on the spine, in reading order, separated by " / " between separate lines or blocks. Copy it exactly as printed, in its own script (for example Devanagari stays Devanagari). Leave empty if nothing is readable.
- "title": the book's title as printed, or null
- "author": the author name(s) as printed, or null
- "publisher": the publisher name or logo text as printed, or null
- "script": the main script of the text, e.g. "Latin" or "Devanagari", or null

Rules:
- Never guess or complete words that are not fully visible. A partly visible word is left out, not finished.
- Never use your knowledge of real books to fill in a title, author or publisher you cannot see. If only "MATHEMATICS" is visible, the title is "MATHEMATICS".
- Text on a spine may run top-to-bottom, bottom-to-top or across; read it whichever way it runs.
- Class or grade labels printed on the spine (e.g. "Class X", "XI") are part of the title.
- A brand or series name printed before the title, such as "Pradeep's" or "Golden", is the publisher, not the author.
- If "legible" is false, set "title", "author" and "publisher" to null; you may still put any readable fragments in "text".
- Give exactly one entry per image.
