# Mailingové sekvence — ukázkové scénáře

Konkrétní příklady toho, jak vypadá odesílání pro člověka v jedné, dvou, třech
a čtyřech skupinách. Vychází z pravidel dohodnutých na callu: poměrové
rozdělení, bodová priorita při kolizi, tvrdý strop 2 maily týdně.

Datum modelu: 6. 8. 2026 (čtvrtek). Všechny kalendáře začínají v týdnu od 10. 8.

---

## 1. Nastavení modelu

### 1.1 Odesílací sloty

Odesílá se ve dvou pevných slotech týdně:

| Slot | Den | Čas |
|---|---|---|
| A | úterý | 10:00 |
| B | čtvrtek | 10:00 |

Strop 2 maily / týden je tím pádem daný strukturou, ne jen kontrolou. Slot je
buď obsazený, nebo prázdný — nikdy se do jednoho slotu nedávají dva maily.

Konkrétní sloty použité v příkladech níže:

```
S1  11. 8. út    S5  25. 8. út    S9   8. 9. út    S13 22. 9. út
S2  13. 8. čt    S6  27. 8. čt    S10 10. 9. čt    S14 24. 9. čt
S3  18. 8. út    S7   1. 9. út    S11 15. 9. út    S15 29. 9. út
S4  20. 8. čt    S8   3. 9. čt    S12 17. 9. čt    S16  1. 10. čt
```

### 1.2 Ukázkové skupiny a jejich sekvence

Tři skupiny odpovídající třem vstupním branám na webu. Každá má 6 mailů, každý
mail má pevné pořadí uvnitř své sekvence a to pořadí se nikdy nepřeskakuje.

**Skupina A — Prověření emitenta dluhopisu**
Vstup: člověk si otevřel / stáhl prověrku konkrétního emitenta.

| # | Předmět | Cíl |
|---|---|---|
| A1 | Tři čísla, která v prověrce emitenta čtěte jako první | dodat hodnotu, potvrdit volbu |
| A2 | Pět varovných signálů z účetní závěrky | edukace |
| A3 | Případ z praxe: emitent, který splácel z nových emisí | důkaz kompetence |
| A4 | Kdo je ručitel a jak si ho ověřit ve struktuře skupiny | edukace |
| A5 | Dluhopis vs. fond kvalifikovaných investorů — kdy co | most k nabídce |
| A6 | Pošlete nám svůj seznam emitentů, projdeme ho s vámi | výzva k akci |

**Skupina B — Mapa nemovitostních projektů**
Vstup: člověk si stáhl mapu projektů.

| # | Předmět | Cíl |
|---|---|---|
| B1 | Jak mapu číst: co znamenají barvy fází projektu | onboarding nástroje |
| B2 | Tři lokality, kde se ceny za poslední rok zastavily | data |
| B3 | Dohledání developera v pěti krocích | edukace |
| B4 | Nájemní výnos vs. růst ceny — co se počítá kdy | edukace |
| B5 | Nemovitostní FKI vs. přímý nákup bytu | most k nabídce |
| B6 | Konzultace nad konkrétní lokalitou | výzva k akci |

**Skupina C — Srovnávač FKI fondů**
Vstup: člověk si porovnal dva a více fondů.

| # | Předmět | Cíl |
|---|---|---|
| C1 | Poplatky FKI: co se schovává pod „průběžné" | edukace |
| C2 | Cílový výnos není zaručený výnos | očekávání |
| C3 | Třídy akcií PIA vs. VIA — proč mají různá čísla | edukace |
| C4 | Likvidita a odkupní lhůty, na které se nikdo neptá | edukace |
| C5 | Minimální investice 1 mil. Kč v praxi | kvalifikace |
| C6 | Sestavíme vám short-list pěti fondů | výzva k akci |

**Vlna R — reaktivační** (není skupina, je to jednorázová vlna)

| # | Předmět |
|---|---|
| R1 | Aktualizovali jsme databázi 230 FKI — nová data o poplatcích |

### 1.3 Skóre

```
skóre = stupeň × 10  +  čerstvost  +  obchodní váha kategorie
```

Obchodní váhy nastavené pro tento model (obchod je mění podle toho, co se prodává):

| Kategorie | Váha |
|---|---|
| Srovnávač FKI (C) | 10 |
| Mapa nemovitostí (B) | 8 |
| Prověření emitenta (A) | 6 |
| Kalkulačka minimální investice (D) | 5 |

Přepočet skóre běží **jednou týdně, v pondělí ráno**. Změna signálu ve středu se
projeví až od následujícího pondělí — jinak by lidem skákaly poměry uprostřed
sekvence.

### 1.4 Jak rotace vybírá skupinu do slotu

Do každého slotu jde ta skupina, která je vůči svému poměru nejvíc pozadu.
Prakticky to znamená tyhle vzory:

| Poměr | Vzor jednoho cyklu | Délka cyklu |
|---|---|---|
| 1 | P P | průběžně |
| 2 : 1 | P, V, P | 3 sloty = 1,5 týdne |
| 3 : 2 : 1 | P, V, P, T, V, P | 6 slotů = 3 týdny |

P = primární, V = vedlejší, T = třetí.

---

## 2. Příklad 1 — Jana, jedna skupina

Stáhla si prověrku emitenta 5. 8., nic dalšího.

| Skupina | Stupeň | Čerstvost | Váha | Skóre |
|---|---|---|---|---|
| A — prověření emitenta | 3 → 30 | do 7 dní → +5 | 6 | **41** |

Poměr 1 → všechny sloty patří sekvenci A.

| Slot | Datum | Mail |
|---|---|---|
| S1 | 11. 8. út | A1 |
| S2 | 13. 8. čt | A2 |
| S3 | 18. 8. út | A3 |
| S4 | 20. 8. čt | A4 |
| S5 | 25. 8. út | A5 |
| S6 | 27. 8. čt | A6 |

Sekvence dojede za 3 týdny. Poté Jana padá do měsíčního newsletteru, dokud
nevyvolá nový signál.

---

## 3. Příklad 2 — Petr, dvě skupiny (2 : 1)

Prověřil si dva emitenty (2. 8., opakovaně) a před třemi týdny si stáhl mapu
nemovitostí (15. 7.).

| Skupina | Stupeň | Čerstvost | Váha | Skóre | Role |
|---|---|---|---|---|---|
| A — prověření emitenta | 3 → 30 | 4 dny → +5 | 6 | **41** | primární |
| B — mapa nemovitostí | 2 → 20 | 22 dní → +2 | 8 | **30** | vedlejší |

Poměr **2 : 1**, rotační vzor `A, B, A`.

| Slot | Datum | Mail | Skupina |
|---|---|---|---|
| S1 | 11. 8. út | A1 | A |
| S2 | 13. 8. čt | B1 | B |
| S3 | 18. 8. út | A2 | A |
| S4 | 20. 8. čt | A3 | A |
| S5 | 25. 8. út | B2 | B |
| S6 | 27. 8. čt | A4 | A |
| S7 | 1. 9. út | A5 | A |
| S8 | 3. 9. čt | B3 | B |
| S9 | 8. 9. út | A6 | A |
| S10 | 10. 9. čt | B4 | B |
| S11 | 15. 9. út | B5 | B |
| S12 | 17. 9. čt | B6 | B |

Po S9 je sekvence A vyčerpaná. Její váha propadá a zbývající sloty přebírá B —
poměr se dopočítá mezi zbývajícími živými skupinami. Petr dostal obsah z obou
skupin, ale nikdy víc než 2 maily týdně.

---

## 4. Příklad 3 — Lucie, tři skupiny (3 : 2 : 1)

Nejaktivnější typ: mapa nemovitostí (stáhla 3. 8. a vrátila se), jeden emitent
(20. 7.), jednou si porovnala fondy (1. 6.).

| Skupina | Stupeň | Čerstvost | Váha | Skóre | Role |
|---|---|---|---|---|---|
| B — mapa nemovitostí | 3 → 30 | 3 dny → +5 | 8 | **43** | primární |
| A — prověření emitenta | 2 → 20 | 17 dní → +2 | 6 | **28** | vedlejší |
| C — srovnávač FKI | 1 → 10 | 66 dní → 0 | 10 | **20** | třetí |

Poměr **3 : 2 : 1**, rotační vzor `B, A, B, C, A, B`.

| Slot | Datum | Mail | Skupina | Pozn. |
|---|---|---|---|---|
| S1 | 11. 8. út | B1 | B | |
| S2 | 13. 8. čt | A1 | A | |
| S3 | 18. 8. út | B2 | B | |
| S4 | 20. 8. čt | C1 | C | jediný mail C za 3 týdny |
| S5 | 25. 8. út | A2 | A | |
| S6 | 27. 8. čt | B3 | B | konec 1. cyklu: B 3, A 2, C 1 |
| S7 | 1. 9. út | B4 | B | |
| S8 | 3. 9. čt | A3 | A | |
| S9 | 8. 9. út | B5 | B | |
| S10 | 10. 9. čt | C2 | C | |
| S11 | 15. 9. út | A4 | A | |
| S12 | 17. 9. čt | B6 | B | B vyčerpaná |
| S13 | 22. 9. út | A5 | A | poměr přepočten na A : C = 2 : 1 |
| S14 | 24. 9. čt | C3 | C | |
| S15 | 29. 9. út | A6 | A | A vyčerpaná |
| S16 | 1. 10. čt | C4 | C | |

**Praktický důsledek, který je potřeba vzít v úvahu při psaní obsahu:** třetí
skupina doručí svůj první mail až po 10 dnech a celou sekvenci má rozprostřenou
přes dva měsíce. Obsah na třetí pozici proto nesmí být časově vázaný (žádné
„tento týden", žádné odkazy na aktuální akci) — musí být evergreen. Časově
vázané kampaně patří do reaktivačních vln, ne do sekvencí.

---

## 5. Příklad 4 — Tomáš, čtyři skupiny (bere se top 3)

| Skupina | Stupeň | Čerstvost | Váha | Skóre | Role |
|---|---|---|---|---|---|
| C — srovnávač FKI | 3 → 30 | 2 dny → +5 | 10 | **45** | primární |
| A — prověření emitenta | 3 → 30 | 40 dní → 0 | 6 | **36** | vedlejší |
| B — mapa nemovitostí | 2 → 20 | 25 dní → +2 | 8 | **30** | třetí |
| D — kalkulačka min. investice | 1 → 10 | 50 dní → 0 | 5 | **15** | pod čarou |

D se **nevyhazuje** — zůstává evidovaná, dál sbírá signály a počítá si skóre.
Jen z ní neodchází žádný mail, dokud se nedostane do top 3.

Rotace běží stejně jako u Lucie, vzor `C, A, C, B, A, C`:

| Slot | Datum | Mail | Skupina |
|---|---|---|---|
| S1 | 11. 8. út | C1 | C |
| S2 | 13. 8. čt | A1 | A |
| S3 | 18. 8. út | C2 | C |
| S4 | 20. 8. čt | B1 | B |
| S5 | 25. 8. út | A2 | A |
| S6 | 27. 8. čt | C3 | C |

Kdyby Tomáš v září znovu otevřel kalkulačku (stupeň 3, čerstvost +5 → skóre 45),
při pondělním přepočtu se D dostane nad B a od dalšího slotu se role prohodí:
D nastoupí na místo, které měla B, a **B se posune pod čáru na svém posledním
odeslaném mailu** — až se vrátí do top 3, pokračuje mailem B2, ne od začátku.

---

## 6. Kolize a posuny

### 6.1 Reaktivační vlna přebije sekvenci

Vlna R1 je naplánovaná na čtvrtek 20. 8. Petr (příklad 2) má na stejný slot
naplánovaný A3.

Původní plán:

```
S3 18. 8.  A2
S4 20. 8.  A3
S5 25. 8.  B2
S6 27. 8.  A4
S7  1. 9.  A5
```

Po vlně:

```
S3 18. 8.  A2
S4 20. 8.  R1   ← vlna vyhrává, plní databázi novými lidmi
S5 25. 8.  A3   ← posunuto, nezrušeno
S6 27. 8.  B2   ← posunuto
S7  1. 9.  A4   ← posunuto
S8  3. 9.  A5
```

Celý plán se posouvá o jeden slot. Nic se neztrácí, pořadí uvnitř sekvencí
zůstává. Petr v týdnu 17.–23. 8. dostal 2 maily (A2 + R1), ne 3.

### 6.2 Dva sekvenční maily do jednoho slotu

Nastává jen výjimečně (rotace to obvykle nedopustí) — typicky po ručním zásahu
nebo po návratu skupiny nad čáru. Lucie má do S9 naplánované B5 (skóre 43)
i A3 (skóre 28):

| Slot | Odejde | Co se stane s druhým |
|---|---|---|
| S9 8. 9. | B5 — vyšší skóre | A3 se přesune na první volný slot, tedy S10 |

C2, která měla být v S10, se posouvá na S11 a řetěz pokračuje. Pořadí uvnitř
sekvence A se nemění — A3 stále odchází před A4.

### 6.3 Přepočet skóre uprostřed sekvence

Lucie 26. 8. proklikne mail A2 a otevře prověrku dalšího emitenta → stupeň
skupiny A stoupá na 3. Přepočet proběhne až v pondělí 31. 8.:

| Skupina | Před 31. 8. | Po 31. 8. | Role |
|---|---|---|---|
| A | 28 | 30 + 5 + 6 = **41** | primární (bylo vedlejší) |
| B | 43 | 30 + 0 + 8 = **38** | vedlejší (bylo primární) |
| C | 20 | 10 + 0 + 10 = **20** | třetí |

Ukazatele v sekvencích se **nikam neresetují**: A je na A3, B je na B4, C je na
C2. Mění se jen to, kdo dostává kolik slotů.

| Slot | Původní plán | Po přepočtu |
|---|---|---|
| S7 1. 9. | B4 | **A3** |
| S8 3. 9. | A3 | **B4** |
| S9 8. 9. | B5 | **A4** |
| S10 10. 9. | C2 | C2 |
| S11 15. 9. | A4 | **B5** |
| S12 17. 9. | B6 | **A5** |

### 6.4 Strop je nadřazený všemu

Kdo je ve třech skupinách, nedostane šest mailů týdně. Dostane dva. Poměr
3 : 2 : 1 se naplní až v horizontu tří týdnů, ne jednoho.

| Počet skupin | Mailů týdně | Za tři týdny |
|---|---|---|
| 1 | 2 | 6 z jedné sekvence |
| 2 | 2 | 4 : 2 |
| 3 | 2 | 3 : 2 : 1 |
| 4 a víc | 2 | 3 : 2 : 1 z top 3, zbytek jen evidován |

---

## 7. Shrnutí pravidel

| Pravidlo | Chování |
|---|---|
| Strop frekvence | max. 2 maily týdně na kontakt ze všech zdrojů; tvrdý, stojí nad vším |
| Přiřazení skupin | člověk zůstává ve všech skupinách, do kterých se zařadil |
| Rozdělení slotů | 1 → 1, 2 → 2 : 1, 3 → 3 : 2 : 1, 4+ → top 3 podle skóre |
| Výběr do slotu | rotace: bere skupina nejvíc pozadu vůči svému poměru |
| Kolize dvou sekvenčních mailů | vyhrává vyšší skóre, druhý na první volný slot |
| Kolize se vlnou | vyhrává vlna, sekvenční mail se posouvá |
| Přepočet skóre | jednou týdně, v pondělí |
| Pořadí uvnitř sekvence | nikdy se nepřeskakuje ani neresetuje |
| Vyčerpaná sekvence | uvolní váhu, poměr se přepočítá mezi zbývajícími |
| Skupina pod čarou | eviduje se dál, jen se z ní neodesílá |

---

## 8. Otevřené body k doladění

1. **Týden = kalendářní, nebo klouzavých 7 dní?** Při kalendářním týdnu může
   člověk dostat mail ve čtvrtek a další v úterý (4 dny), ale taky v úterý a
   ve čtvrtek dalšího týdne po pondělním resetu. Doporučuji klouzavých 7 dní.
2. **Co dělá konverze / odpověď?** Návrh: odpověď na mail nebo objednaná
   konzultace pauzují všechny sekvence a kontakt přechází na obchod.
3. **Odhlášení** — je globální (všechny skupiny), nebo per skupina? Právně
   jednodušší globální, obchodně horší.
4. **Poslední mail sekvence (výzva k akci)** — má mít přednost před rotací?
   U Lucie odejde C6 až po dvou měsících. Varianta: závěrečný mail skupiny
   dostane bonus ke skóre, aby se neztratil na konci fronty.
5. **Obchodní váhy** — kdo a jak často je mění. Změna váhy o 10 bodů přehodí
   pořadí skupin stejně silně jako změna stupně o jeden.
