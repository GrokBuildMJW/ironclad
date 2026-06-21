<!-- run:REC-arch-0001 template:decision-matrix conflicts:— -->

| Criterion (wt.) | Modulith behalten | In Microservices aufteilen |
|---|--:|--:|
| Evolvierbarkeit (×3) | 3 | 4 |
| Betriebslast (×2) | 5 | 2 |
| TCO (×2) | 5 | 2 |
| **Weighted score** | **29** | **20** |

**Recommendation:** **Modulith behalten** — Bis fünf Teams überwiegen niedrigere Betriebslast und TCO; die Evolvierbarkeits-Lücke ist mit einem modularen Schnitt schließbar.
**Fallback:** In Microservices aufteilen — trigger when klare, stabile Domänengrenzen ODER >5 Teams mit Deploy-Konflikten.