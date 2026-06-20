<!-- run:REC-arch-0001 template:decision-matrix conflicts:— -->

| Kriterium (Gew.) | Modulith behalten | In Microservices aufteilen |
|---|--:|--:|
| Evolvierbarkeit (×3) | 3 | 4 |
| Betriebslast (×2) | 5 | 2 |
| TCO (×2) | 5 | 2 |
| **Gewichteter Score** | **29** | **20** |

**Empfehlung:** **Modulith behalten** — Bis fünf Teams überwiegen niedrigere Betriebslast und TCO; die Evolvierbarkeits-Lücke ist mit einem modularen Schnitt schließbar.
**Rückzugsoption:** In Microservices aufteilen — auslösen wenn klare, stabile Domänengrenzen ODER >5 Teams mit Deploy-Konflikten.