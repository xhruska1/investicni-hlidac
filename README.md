# Investiční hlídač

Malý automat, který mi každé ráno pohlídá vybrané ETF, zlato a bitcoin. Když nastane situace, kterou mám v pravidlech (začátek měsíce, výrazný propad pod historické maximum), pošle mi notifikaci na mobil. K upozornění přidá krátký komentář, který vygeneruje AI z aktuálních čísel.

Projekt jsem si udělal ve volném čase, protože jsem chtěl investovat pravidelně a bez emocí a zároveň si vyzkoušet automatizaci a napojení na AI.

> Jen upozorňuje, nic nenakupuje. Nejde o investiční doporučení.

## Co umí

- **Stahuje data** o cenách přes `yfinance` a přepočítává je do CZK podle aktuálních kurzů.
- **Počítá metriky:** pokles pod historickým maximem (ATH), 52týdenní minimum a maximum, změnu za měsíc.
- **Vyhodnocuje pravidla** z `config.json`:
  - připomínka pravidelného měsíčního nákupu,
  - přikoupení při propadu o X % pod ATH (stupňovitě),
  - limity na jeden nákup a na celý měsíc.
- **Nic neposílá dvakrát:** ve `state.json` si pamatuje, co už v daném měsíci odeslal.
- **AI komentář:** zavolá Claude Code (`claude -p`) a nechá napsat 2–3 věty jen z dodaných čísel.
- **Notifikace** posílá na mobil přes [ntfy.sh](https://ntfy.sh).
- **Běží sám každý den** přes **GitHub Actions** (cron), stav ukládá zpátky do repozitáře.

## Ukázka notifikace (ilustrační čísla)

```
Bitcoin: 32 % pod ATH

📌 Přikoupit Bitcoin (BTC-USD)
💰 500 Kč
❓ 32.4 % pod ATH (pravidlo ≥30 %)
   Cena 1 912 000 Kč · ATH 2 830 000 Kč
💬 Bitcoin je přes třetinu pod maximem a za měsíc oslabil, …
```

## Technologie

Python 3.12 · pandas · yfinance · GitHub Actions · Claude Code (CLI) · ntfy

## Spuštění

```bash
pip install -r requirements.txt
cp config.example.json config.json      # a uprav si aktiva, částky a ntfy téma

python hlidac.py --dry-run   # nic neposílá ani neukládá, jen vypíše výsledek
python hlidac.py --test      # pošle testovací notifikaci
python hlidac.py             # běžný běh
```

Další přepínače: `--date 2026-12-01` simuluje jiné datum, `--daily-after 9` pustí běh jen jednou denně po 9:00 pražského času.

### GitHub Actions

Workflow `.github/workflows/hlidac.yml` spouští hlídač každé ráno. V repozitáři je potřeba nastavit tyto *Secrets*:

| Secret | K čemu |
|---|---|
| `NTFY_URL` | adresa vlastního ntfy tématu (drž ji v tajnosti) |
| `CLAUDE_CODE_OAUTH_TOKEN` | přístup pro AI komentáře |

## Struktura

| Soubor | Účel |
|---|---|
| `hlidac.py` | celá logika: data → metriky → pravidla → zpráva → notifikace |
| `config.example.json` | ukázková konfigurace (aktiva, pravidla, limity) |
| `.github/workflows/hlidac.yml` | denní automatické spouštění |
