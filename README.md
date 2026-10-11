# AIAGENT001: AI obchodník

Agent pro příkazovou řádku postavený na [Claude API](https://platform.claude.com/docs). Vyhledává potenciální zákazníky, ověří, že jim vaše nabídka dává smysl, najde jejich veřejný kontakt a napíše jim osobní e-mail s nabídkou spolupráce. Každý e-mail vám před odesláním ukáže ke schválení.

```
vy › Najdi 10 e-shopů s vlastním skladem v Jihomoravském kraji a připrav jim nabídku.
→ list_leads {}
→ ares_search {"name": "shop", "city": "Brno", "legal_form": ["112"]}
→ web_search {"query": "e-shop Brno vlastní sklad expedice"}
→ web_fetch {"url": "https://www.priklad-eshop.cz/kontakt"}
→ save_lead {"company": "Příklad Shop s.r.o.", "email": "obchod@priklad-eshop.cz", ...}
→ save_draft {"id": 1, "subject": "Rychlejší expedice pro Příklad Shop", ...}
...
Prošel jsem 23 firem, 10 odpovídá profilu. Návrhy e-mailů jsou ve složce navrhy/,
u 2 firem jsem nenašel veřejný e-mail. Mám návrhy odeslat?
```

## Jak agent pracuje

1. **Hledání:** české firmy hledá v obchodním rejstříku ARES (podle oboru, města a názvu) a na webu (katalogy, oborové seznamy, zprávy).
2. **Kvalifikace:** projde web firmy, porovná ji s vaším ideálním zákazníkem z profilu a najde konkrétní důvod, proč by je nabídka mohla zajímat.
3. **Kontakt:** použije jen e-mail, který firma sama zveřejňuje, a zapíše, kde ho našel. Adresy nehádá.
4. **Databáze leadů:** každou vhodnou firmu uloží i s odůvodněním. Hlídá duplicity (IČO, e-mail, doména), takže nikoho neosloví dvakrát.
5. **E-mail na míru:** krátký, věcný, s odkazem na konkrétní zjištění o firmě, jednou výzvou k akci, vaším podpisem a možností odhlášení. Návrh uloží do `workspace/navrhy/`.
6. **Odeslání:** jen na váš pokyn a každý e-mail po schválení. Platí denní limit a kontakty, které si zprávy nepřejí, agent zablokuje.

## Nástroje

| Nástroj | K čemu | Schválení |
|---|---|---|
| `ares_search`, `ares_company` | vyhledání firem v ARES a detail podle IČO | ne |
| `web_search`, `web_fetch` | vyhledávání a čtení webu (běží na serverech Anthropicu) | ne |
| `save_lead`, `list_leads`, `get_lead`, `update_lead` | databáze leadů (`workspace/.aiagent/leads.json`) | ne |
| `save_draft` | uložení návrhu e-mailu k leadu a do `workspace/navrhy/` | ne |
| `export_leads` | export leadů do CSV pro Excel | ne |
| `send_email` | odeslání e-mailu přes SMTP (jen když je nastavený `SMTP_HOST`) | **ano** |
| `add_task`, `list_tasks`, `update_task` | připomínky follow-upů | ne |
| `get_current_time` | dnešní datum | ne |
| `list_files`, `read_file`, `write_file` | soubory v pracovním adresáři | zápis **ano** |
| `http_request` | webhooky a REST API (např. zápis do CRM přes Make) | **ano**, kromě GET |
| `run_command` | příkaz v shellu | **ano** |

Stavy leadu: `new` → `qualified` → `drafted` → `contacted` → `replied` / `interested` / `not_interested` / `do_not_contact`.

## Instalace

Je potřeba Python 3.10 nebo novější.

```bash
git clone https://github.com/petrkrapek/AIAGENT001.git
cd AIAGENT001
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env               # doplňte ANTHROPIC_API_KEY
cp profil.example.md profil.md     # popište svou firmu a nabídku
```

API klíč vytvoříte na <https://platform.claude.com/settings/keys>.

### Profil firmy (`profil.md`)

Tohle je nejdůležitější krok. Podle profilu agent vybírá firmy a píše e-maily. Vyplňte, co nabízíte, komu (obor, velikost, region, podle čeho poznat, že nabídku potřebují), čím se lišíte, pravdivé reference, jakou akci od adresáta chcete a svůj podpis. Bez profilu se vás agent na tyto informace nejdřív zeptá.

### Odesílání e-mailů

Bez nastaveného SMTP agent jen ukládá návrhy a odeslání necháte na sobě. Pro odesílání doplňte do `.env` údaje SMTP (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`). Hodí se samostatná obchodní schránka. Limit `AIAGENT_DAILY_EMAIL_LIMIT` (výchozí 20) chrání pověst vaší domény.

## Použití

**Interaktivní chat:**

```bash
aiagent
```

Příklady zadání:
- „Najdi 15 stavebních firem v Brně s 10 až 50 zaměstnanci a připrav jim nabídky.“
- „Projdi leady ve stavu drafted a odešli je.“ (u každého se zeptá na schválení)
- „Firma Beta odpověděla, že nemá zájem.“ (nastaví `do_not_contact`)
- „Firma Alfa má zájem, připomeň mi zavolat jim v pondělí.“
- „Exportuj leady do CSV.“

V chatu fungují příkazy `/leads`, `/tasks`, `/tools`, `/reset`, `/help` a `/exit`.

**Jeden úkol a konec** (třeba ranní hledání přes cron):

```bash
aiagent run "Najdi 5 nových firem podle profilu a připrav jim návrhy e-mailů."
```

Když agent běží bez terminálu, nemá se koho zeptat. E-maily proto neodešle a připraví jen návrhy, které ráno projdete a schválíte v chatu. Přepínač `--yes` schvaluje všechno automaticky. U odesílání e-mailů ho nedoporučuji.

```cron
50 7 * * 1-5  cd /cesta/k/AIAGENT001 && .venv/bin/aiagent run "Najdi 5 nových firem podle profilu a připrav návrhy." >> workspace/cron.log 2>&1
```

Další přepínače: `--model`, `--effort low|medium|high|xhigh|max`, `--workspace CESTA` a `--show-thinking`.

## Konfigurace (`.env`)

| Proměnná | Výchozí | Popis |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | API klíč (povinný) |
| `AIAGENT_MODEL` | `claude-opus-5-5` | model Claude |
| `AIAGENT_EFFORT` | `high` | hloubka uvažování; `medium` je levnější a rychlejší |
| `AIAGENT_PROFILE` | `profil.md` | profil firmy a nabídky |
| `AIAGENT_DAILY_EMAIL_LIMIT` | `20` | maximum odeslaných e-mailů za den |
| `AIAGENT_WORKSPACE` | `./workspace` | data agenta (leady, návrhy, úkoly, audit log) |
| `AIAGENT_TIMEZONE` | `Europe/Prague` | časové pásmo |
| `AIAGENT_WEB_TOOLS` | `1` | vyhledávání a čtení webu |
| `SMTP_*` | – | odesílání e-mailů |

## Právní minimum (ČR/EU)

Tohle není právní rada. Než začnete e-maily odesílat, ověřte si pravidla s právníkem.

- **Obchodní sdělení e-mailem** upravuje zákon č. 480/2004 Sb. Obecně vyžaduje předchozí souhlas adresáta (výjimkou jsou vlastní zákazníci a obdobné produkty). Úřad pro ochranu osobních údajů ho uplatňuje i na firemní adresy.
- **Jmenné e-maily** (jan.novak@firma.cz) jsou osobní údaje podle GDPR. Potřebujete právní titul (typicky oprávněný zájem) a adresáta musíte informovat.
- Agent proto píše **jednotlivé, adresné** zprávy, používá jen veřejně zveřejněné kontakty a zapisuje jejich zdroj. Každý e-mail končí možností odhlášení a odhlášené kontakty trvale blokuje (`do_not_contact`).

## Jak to funguje uvnitř

```
src/aiagent/
├── agent.py      smyčka agenta a systémový prompt obchodníka
├── cli.py        příkazová řádka, schvalování akcí, výpis průběhu
├── config.py     nastavení z prostředí / .env, načtení profilu
├── storage.py    jednoduché JSON úložiště
└── tools/
    ├── ares.py   leads.py   mail.py        obchodní nástroje
    ├── web.py    tasks.py   files.py   shell.py   system.py
    └── __init__.py   registr nástrojů
```

Agent volá Messages API s adaptivním uvažováním. Opakující se část požadavku (prompt, profil, nástroje, historie) se ukládá do cache, takže další kroky stojí méně. Kdyby model požadavek odmítl, API ho zkusí zopakovat na záložním modelu (`fallbacks: "default"`). Každé volání nástroje se zapisuje do `workspace/.aiagent/audit.jsonl`.

### Přidání vlastního nástroje

Vytvořte soubor v `src/aiagent/tools/` s funkcí `build(config) -> list[Tool]` a zaregistrujte ho v `build_registry()` v `tools/__init__.py`. Vzorem může být `ares.py`. Popis nástroje (`description`) je pro model nejdůležitější: napište, k čemu nástroj slouží a kdy ho použít.

## Testy

```bash
pip install -e ".[dev]"
pytest
```

Testy nepotřebují API klíč ani síť. API, ARES i SMTP jsou v nich simulované.
