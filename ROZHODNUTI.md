# Rozhodnutí

Projekt jsem vytvořil v Pythonu 3.12, protože je vhodný pro scraping, zpracování dat a PDF dokumentů. Pro správu závislostí používám `uv`, pro HTTP komunikaci `httpx`, pro HTML `selectolax`, pro PDF `PyMuPDF`, pro validaci `Pydantic`, pro CLI `Typer` a pro průběžný stav SQLite.

Data získávám primárně z oficiálních webů fondů a jejich dokumentů, protože je považuji za nejdůvěryhodnější zdroj. Následně používám adaptéry pro konkrétní správce, například AVANT, AMISTA.

Jako další fallback jsem vyzkoušel PorovnejFondy, ale při testování nepřinesl téměř žádná nová data, proto jsem ho prozatím vynechal z hlavního běhu.

Pro zrychlení používám paralelní zpracování, HTTP cache a cache již zpracovaných dokumentů. Výstup je ukládán do JSON a kontrolován pomocí Pydantic modelů a automatických testů.

OCR pro naskenovaná PDF jsem zatím nepřidal kvůli časové náročnosti a zpomalení běhu. V další verzi bych ho spouštěl pouze u dokumentů, ze kterých se nepodaří získat text běžným parserem.

Dále bych rozšířil počet adaptérů, zlepšil hledání dokumentů přes `sitemap.xml`, přidal další PDF parsery a automatickou kontrolu konfliktů mezi více zdroji.
