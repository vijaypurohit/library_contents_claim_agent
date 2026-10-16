# Evaluation: sweep `far`

Commit `32de3b9+changes` · 2026-10-16 12:34 UTC · ground truth verified

| Measure | Result | Target | |
|---|---|---|---|
| Book count | 10 counted, 10 true (0% error) | within 5% | met |
| Titles correct (legible spines) | 8 of 9 (89%) | at least 70% | met |
| Titles confidently wrong | 0 of 10 (0%); 1 other wrong (sent to review), 1 blank | at most 3% | met |
| Spine dimensions | not measured: no ground truth | within 15% | – |
| Book prices (hand-checked) | 2 of 6 within target; 4 not priced | within 25% | **missed** |
| Books with no listing anywhere left unpriced | 4 of 4 | all | met |
| Non-book items found (right category) | 1 of 1 (100%) | at least 80% | met |
| Floor area | not measured: no ground truth (system: empty) | within 10% | – |
| Wall area | not measured: no ground truth (system: empty) | within 15% | – |

## Books

| Position | True title | Legible | System title | Outcome | True price | System price |
|---|---|---|---|---|---|---|
| A1-1 | Mathematics Class X | yes | Refresher Course in Mathematics Part - 1 for Class X | wrong | – | – |
| A1-2 | Mathematics XI | yes | Mathematics for Class 11 | correct | 396.0 | 351.0 |
| A1-3 | Social Science Class X | yes | SOCIAL SCIENCE CLASS X | correct | 520.0 | – |
| A1-4 | Word Power Made Easy | yes | Word Power Made Easy | correct | 182.0 | 172.0 |
| A1-5 | English Language and Literature Class X | yes | ENGLISH LANGUAGE AND LITERATURE CLASS X | correct | 460.0 | – |
| A1-6 | Pradeep's Fundamental Physics Class XII | yes | Fundamental Physics Class XII | correct | 834.0 | – |
| A2-1 | Secondary School Mathematics for Class 10 | no | – | blank | 621.0 | – |
| A2-2 | Sanjiv English Course Class 10 | yes | Sanjay English Course Class 10 | correct | – | – |
| A2-3 | सामान्य हिन्दी | yes | सामान्य हिंदी | correct | – | – |
| A2-4 | Inorganic Chemistry B.Sc. Part-I | yes | B.Sc. Part-I Inorganic Chemistry | correct | – | – |
