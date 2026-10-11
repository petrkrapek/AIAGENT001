# AIAGENT001: osobní AI agent pro automatizaci úkolů

Agent pro příkazovou řádku postavený na [Claude API](https://platform.claude.com/docs). Zadání mu napíšete běžnou řečí. Sám si rozvrhne postup, použije nástroje (soubory, příkazy, seznam úkolů, webhooky, e-mail, web) a pokračuje, dokud úkol nedokončí.

```
vy › Najdi na webu otevírací dobu FÚ Praha 1, ulož ji do poznámek a přidej mi úkol podat přiznání do 31. 3.
→ web_search {"query": "Finanční úřad Praha 1 úřední hodiny"}
→ get_current_time {}
Agent chce provést akci: write_file
  path: poznamky/fu-praha1.md
  content: ...
Povolit? [a/N] a
→ add_task {"title": "Podat daňové přiznání", "due": "2027-03-31"}

Hotovo: otevírací doba je uložená v poznamky/fu-praha1.md a úkol #1 máte s termínem 31. 3. 2027.
```

## Co agent umí

| Nástroj | K čemu | Schválení |
|---|---|---|
| `get_current_time` | aktuální datum a čas (výpočet termínů) | ne |
| `list_files`, `read_file` | čtení souborů v pracovním adresáři | ne |
| `write_file` | zápis souborů v pracovním adresáři | **ano** |
| `run_command` | spuštění příkazu v shellu (skripty, git, zpracování dat) | **ano** |
| `add_task`, `list_tasks`, `update_task` | vlastní seznam úkolů (`workspace/.aiagent/tasks.json`) | ne |
| `http_request` | webhooky (Make, n8n, Zapier, Slack) a REST API | **ano**, kromě GET |
| `send_email` | odeslání e-mailu přes SMTP (jen když je nastavený `SMTP_HOST`) | **ano** |
| `web_search`, `web_fetch` | vyhledávání a čtení webu (běží na serverech Anthropicu) | ne |

Než agent provede akci se schválením, vypíše ji celou a zeptá se `Povolit? [a/N]`. Každé volání nástroje se zapisuje do `workspace/.aiagent/audit.jsonl`.

## Instalace

Je potřeba Python 3.10 nebo novější.

```bash
git clone https://github.com/petrkrapek/AIAGENT001.git
cd AIAGENT001
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env               # doplňte ANTHROPIC_API_KEY
```

API klíč vytvoříte na <https://platform.claude.com/settings/keys>.

## Použití

**Interaktivní chat:**

```bash
aiagent
```

V chatu fungují příkazy `/help`, `/tools`, `/tasks`, `/reset` a `/exit`.

**Jeden úkol a konec** (hodí se do skriptů a cronu):

```bash
aiagent run "Shrň soubory ve složce faktury/ do tabulky prehled.csv"
aiagent run -f ukoly/ranni-prehled.md
```

Když agent běží bez terminálu (cron, pipe), není se koho zeptat. Akce vyžadující schválení proto automaticky zamítne. Přepínač `--yes` je schválí všechny. Používejte ho jen u úkolů, kterým plně důvěřujete.

Příklad pro cron (každý pracovní den v 7:50):

```cron
50 7 * * 1-5  cd /cesta/k/AIAGENT001 && .venv/bin/aiagent run -f ukoly/ranni-prehled.md >> workspace/cron.log 2>&1
```

**Další přepínače:** `--model`, `--effort low|medium|high|xhigh|max`, `--workspace CESTA` a `--show-thinking` (zobrazí shrnutí uvažování modelu).

## Konfigurace (`.env`)

| Proměnná | Výchozí | Popis |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | API klíč (povinný) |
| `AIAGENT_MODEL` | `claude-opus-5-5` | model Claude |
| `AIAGENT_EFFORT` | `high` | hloubka uvažování; `medium`/`low` je levnější a rychlejší |
| `AIAGENT_WORKSPACE` | `./workspace` | jediný adresář, kde agent pracuje se soubory a spouští příkazy |
| `AIAGENT_TIMEZONE` | `Europe/Prague` | časové pásmo |
| `AIAGENT_WEB_TOOLS` | `1` | vyhledávání a čtení webu |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS` | – | odesílání e-mailů |

## Jak to funguje

```
src/aiagent/
├── agent.py      smyčka agenta: volání Claude → spuštění nástrojů → vrácení výsledků → opakování
├── cli.py        příkazová řádka, schvalování akcí, výpis průběhu
├── config.py     načtení nastavení z prostředí / .env
└── tools/        nástroje (každý soubor = jedna skupina)
    ├── files.py  shell.py  tasks.py  web.py  mail.py  system.py
    └── __init__.py   registr nástrojů
```

Agent volá Messages API s adaptivním uvažováním a nastavitelnou hloubkou (`effort`). Opakující se část požadavku (systémový prompt, nástroje, historie) se ukládá do cache, takže další kroky stojí méně. Kdyby model požadavek odmítl, API ho zkusí zopakovat na záložním modelu (parametr `fallbacks: "default"`). Historie konverzace se jen doplňuje, nikdy se zpětně nepřepisuje.

### Přidání vlastního nástroje

1. Vytvořte soubor v `src/aiagent/tools/`, třeba `kalendar.py`:

   ```python
   from ..config import Config
   from . import Tool, ToolError, schema

   def build(config: Config) -> list[Tool]:
       def create_event(title: str, start: str) -> str:
           ...  # volání vašeho API
           return f"Událost '{title}' vytvořena."

       return [
           Tool(
               name="create_event",
               description="Vytvoří událost v kalendáři. start ve formátu ISO 8601.",
               input_schema=schema(
                   {"title": {"type": "string"}, "start": {"type": "string"}},
                   ["title", "start"],
               ),
               handler=create_event,
               needs_approval=True,
           )
       ]
   ```

2. Zaregistrujte ho v `build_registry()` v `src/aiagent/tools/__init__.py`.

Popis nástroje (`description`) je pro model nejdůležitější: napište, k čemu nástroj slouží a kdy ho použít.

## Testy

```bash
pip install -e ".[dev]"
pytest
```

Testy používají simulovaného klienta, takže nepotřebují API klíč ani síť.

## Bezpečnost

- Soubory i příkazy jsou omezené na pracovní adresář. Příkaz v shellu ale technicky může sáhnout i jinam, proto každý vyžaduje schválení.
- `.env` s klíči je v `.gitignore`. Nikdy ho necommitujte.
- Webové stránky, e-maily a odpovědi API mohou obsahovat podstrčené pokyny (prompt injection). U akcí s dopadem proto vždy čtěte, co schvalujete.
