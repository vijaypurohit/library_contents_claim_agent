# Evaluation: sweep `rush`

Commit `4dd517e` · 2026-10-16 13:06 UTC · ground truth verified

| Measure | Result | Target | |
|---|---|---|---|
| Book count | 10 counted, 10 true (0% error) | within 5% | met |
| Titles correct (legible spines) | 3 of 9 (33%) | at least 70% | **missed** |
| Titles confidently wrong | 0 of 10 (0%); 3 other wrong (sent to review), 4 blank | at most 3% | met |
| Spine dimensions | not measured: no ground truth | within 15% | – |
| Book prices (hand-checked) | 0 of 6 within target; 6 not priced | within 25% | **missed** |
| Books with no listing anywhere left unpriced | 4 of 4 | all | met |
| Non-book items found (right category) | 1 of 1 (100%) | at least 80% | met |
| Floor area | not measured: no ground truth (system: empty) | within 10% | – |
| Wall area | not measured: no ground truth (system: empty) | within 15% | – |

## Books

| Position | True title | Legible | System title | Outcome | True price | System price |
|---|---|---|---|---|---|---|
| A1-1 | Mathematics Class X | yes | I.C.S.E.Mathematics | wrong | – | – |
| A1-2 | Mathematics XI | yes | MATHEMATICS | correct | 396.0 | – |
| A1-3 | Social Science Class X | yes | SOCIAL SCIENCE | correct | 520.0 | – |
| A1-4 | Word Power Made Easy | yes | – | blank | 182.0 | – |
| A1-5 | English Language and Literature Class X | yes | ENGLISH | wrong | 460.0 | – |
| A1-6 | Pradeep's Fundamental Physics Class XII | yes | PHYSICS | wrong | 834.0 | – |
| A2-1 | Secondary School Mathematics for Class 10 | no | – | blank | 621.0 | – |
| A2-2 | Sanjiv English Course Class 10 | yes | – | blank | – | – |
| A2-3 | सामान्य हिन्दी | yes | – | blank | – | – |
| A2-4 | Inorganic Chemistry B.Sc. Part-I | yes | INORGANIC CHEMISTRY | correct | – | – |
