# AIAGENT001: AI obchodník

Agent pro příkazovou řádku postavený na [Claude API](https://platform.claude.com/docs). Vyhledává potenciální zákazníky, ověří, že jim vaše nabídka dává smysl, najde jejich veřejný kontakt a napíše jim osobní e-mail s nabídkou spolupráce. Žádný e-mail neodejde bez vašeho schválení: buď ho potvrdíte v terminálu, nebo předem schválíte návrh v Airtable.

Všechna data (leady, návrhy e-mailů, odeslané e-maily, úkoly a audit log) ukládá do vaší báze v **[Airtable](https://airtable.com)**. Tam také popíšete svou firmu a služby, které nabízíte. Na serveru tak zůstává jen soubor `.env` s klíči. Lokálně neukládá nic, takže leady můžete procházet, filtrovat a upravovat přímo v Airtable a agent při další práci vychází z aktuálního stavu.

```
vy › Najdi 10 e-shopů s vlastním skladem v Jihomoravském kraji a připrav jim nabídku.
→ list_leads {}
→ ares_search {"name": "shop", "city": "Brno", "legal_form": ["112"]}
→ web_search {"query": "e-shop Brno vlastní sklad expedice"}
→ web_fetch {"url": "https://www.priklad-eshop.cz/kontakt"}
→ save_lead {"company": "Příklad Shop s.r.o.", "email": "obchod@priklad-eshop.cz", ...}
→ save_draft {"id": "recA1b2C3d4E5f6G7", "subject": "Rychlejší expedice pro Příklad Shop", ...}
...
Prošel jsem 23 firem, 10 odpovídá profilu. Návrhy e-mailů najdete v Airtable
(tabulka Leady, stav „Návrh připraven“), u 2 firem jsem nenašel veřejný e-mail.
Mám návrhy odeslat?
```

## Jak agent pracuje

1. **Hledání:** české firmy hledá v obchodním rejstříku ARES (podle oboru, města a názvu) a na webu (katalogy, oborové seznamy, zprávy).
2. **Kvalifikace:** projde web firmy, porovná ji s vaším ideálním zákazníkem a se službami z Airtable, vybere službu, která firmě nejvíc dává smysl, a najde konkrétní důvod, proč by ji mohla zajímat.
3. **Kontakt:** použije jen e-mail, který firma sama zveřejňuje, a zapíše, kde ho našel. Adresy nehádá.
4. **Databáze leadů v Airtable:** každou vhodnou firmu uloží i s odůvodněním. Hlídá duplicity (IČO, e-mail, doména), takže nikoho neosloví dvakrát.
5. **E-mail na míru:** krátký, věcný, s odkazem na konkrétní zjištění o firmě, jednou výzvou k akci, vaším podpisem a možností odhlášení. Návrh uloží k leadu v Airtable, kde ho můžete zkontrolovat i upravit.
6. **Odeslání:** jen na váš pokyn, jen na adresu uloženou u leadu a jen po schválení (v terminálu, nebo v Airtable). Platí denní limit a kontakty, které si zprávy nepřejí, agent zablokuje.

## Nástroje

| Nástroj | K čemu | Schválení |
|---|---|---|
| `ares_search`, `ares_company` | vyhledání firem v ARES a detail podle IČO | ne |
| `web_search`, `web_fetch` | vyhledávání a čtení webu (běží na serverech Anthropicu) | ne |
| `save_lead`, `list_leads`, `get_lead`, `update_lead` | databáze leadů (Airtable, tabulka Leady) | ne |
| `save_draft` | uložení návrhu e-mailu k leadu (Airtable) | ne |
| `send_email` | odeslání e-mailu přes SMTP na adresu leadu a zápis do tabulky E-maily (jen když je odesílání zapnuté) | **ano**; v režimu `auto` jen návrhy schválené v Airtable |
| `add_task`, `list_tasks`, `update_task` | připomínky follow-upů (Airtable, tabulka Úkoly) | ne |
| `get_current_time` | dnešní datum | ne |
| `http_request` | webhooky a REST API (např. Make); vnitřní a neveřejné adresy jsou zakázané | **ano**, vždy |

## Airtable

Agent používá ve vaší bázi sedm tabulek. Vytvoří je sám příkazem `aiagent airtable-setup`:

| Tabulka | Obsah |
|---|---|
| **Profil firmy** | kdo jste, ideální zákazník, tón e-mailů, podpis; **vyplňujete vy** (jeden řádek), agent jen čte |
| **Služby** | co nabízíte; **vyplňujete vy**, agent jen čte |
| **Leady** | firma, stav, IČO, web, e-mail a jeho zdroj, kontaktní osoba, město, obor, proč se hodí, poznámky, nabízená služba, návrh e-mailu (předmět, text, **schválení návrhu**), kdy byl naposledy osloven, nepotvrzené odeslání (*Odesílání zahájeno*) |
| **E-maily** | každý odeslaný e-mail: předmět, text, komu, kdy, propojený s leadem |
| **Úkoly** | připomínky a follow-upy s termínem |
| **Blokované kontakty** | koho agent nikdy neosloví: odhlášené kontakty zapisuje sám, konkurenci nebo stávající zákazníky **doplňujete vy** |
| **Audit** | záznam každé akce agenta: nástroj, vstup, čas, schválení a výsledek |

Stavy leadu: Nový → Kvalifikovaný → Návrh připraven → Osloven → Odpověděl / Má zájem / Nemá zájem / **Nekontaktovat**. Stav můžete měnit i ručně v Airtable. Když nastavíte „Nekontaktovat“, agent kontakt (i celou jeho doménu) už nikdy neosloví a sám tento stav zrušit nemůže, to jde jen ručně v Airtable. Kontakt si zároveň zapíše do tabulky Blokované kontakty (ručně nastavené stavy převezme při dalším spuštění), takže blokace platí, i když lead smažete. Stav, který agent nezná (třeba vlastní nebo přejmenovaný), bere z opatrnosti jako „Nekontaktovat“.

**Schválení návrhu:** když agent uloží nebo změní návrh e-mailu, nastaví pole *Schválení návrhu* na „Čeká na schválení“. Návrh si v Airtable přečtěte, případně upravte, a přepněte na „Schváleno“. Toto pole agent nikdy nenastavuje sám. V režimu `auto` odešle jen takto schválené návrhy, beze změny textu.

### Tabulka Profil firmy

Jeden řádek s tím, co platí pro firmu jako celek. Agent ho čte před každou prací. Pokud řádků bude víc, použije první vyplněný.

| Pole | Co do něj napsat |
|---|---|
| Firma, IČO, Web | základní údaje |
| O nás | jedna až dvě věty, čím se firma zabývá |
| Čím se lišíme | v čem jste lepší než konkurence |
| Ideální zákazník | obor, velikost, region a signály, že nabídku potřebují (u služeb jde upřesnit v poli „Pro koho“) |
| Koho neoslovovat | obecná pravidla, např. veřejná správa, firmy nad 250 zaměstnanců (konkrétní firmy patří do tabulky Blokované kontakty) |
| Obecné reference | pravdivé výsledky a reference firmy (reference ke konkrétní službě patří do tabulky Služby) |
| Výzva k akci | co má adresát udělat, např. 15minutový hovor |
| Tón e-mailů | např. „vykání, věcně, bez superlativů, do 150 slov“ |
| Podpis | jméno, funkce, firma, telefon, e-mail, web |

### Tabulka Blokované kontakty

Firmy a adresy, které agent nesmí uložit ani oslovit. Každý řádek blokuje podle vyplněných polí:

| Pole | Co do něj napsat |
|---|---|
| Název | pro vaši orientaci, např. název firmy |
| E-mail | konkrétní adresa |
| Doména | doména nebo web firmy (`konkurence.cz` i `https://www.konkurence.cz/`); blokuje i její subdomény a všechny adresy na ní |
| IČO | IČO firmy (mezery nevadí) |
| Důvod | např. „konkurence“, „stávající zákazník“, „odhlásil se“ |

Sem patří konkurence a stávající zákazníci. Agent sem sám zapisuje kontakty ve stavu Nekontaktovat. U adresy na veřejné schránce (Seznam, Gmail…) blokuje jen tu adresu, ne celou doménu. Řádek smažte, jen když kontakt opravdu znovu oslovovat chcete.

### Tabulka Služby

Každá služba je jeden řádek. Agent nabízí jen služby ve stavu **Aktivní** a čerpá jen z toho, co v nich napíšete:

| Pole | Co do něj napsat |
|---|---|
| Název | krátký název služby |
| Stav | Aktivní / Neaktivní (neaktivní agent ignoruje) |
| Popis | co přesně služba obsahuje |
| Pro koho | jakým firmám je určená a podle čeho poznat, že ji potřebují |
| Přínos pro zákazníka | co zákazník získá, ideálně měřitelně |
| Cena | cena nebo cenový rámec (nepovinné) |
| Reference | pravdivé reference a výsledky ke službě |
| Odkaz | stránka služby na vašem webu |
| Pokyny pro agenta | zvláštní pravidla, např. „v prvním e-mailu nezmiňuj cenu“ |

Agent profil i služby načte při spuštění a pak při `/reset`, `/profil` nebo `/sluzby`. Úpravy v Airtable se tak projeví bez restartu. U každého leadu a návrhu e-mailu zapíše, kterou službu nabízel (pole Služba), takže v Airtable uvidíte, co komu šlo.

**Nastavení:**
1. Vytvořte v Airtable prázdnou bázi (např. „AI obchodník“). Její ID najdete v URL: `airtable.com/appXXXXXXXXXXXXXX/…`.
2. Vytvořte osobní token na <https://airtable.com/create/tokens> s oprávněními `data.records:read`, `data.records:write`, `schema.bases:read` a `schema.bases:write` a s přístupem k této bázi.
3. Doplňte do `.env` hodnoty `AIRTABLE_TOKEN` a `AIRTABLE_BASE_ID`.
4. Spusťte `aiagent airtable-setup` (v Dockeru `docker compose run --rm agent airtable-setup`). Příkaz vytvoří chybějící tabulky a pole. Existující data nemění, takže ho můžete spustit kdykoli znovu, třeba po aktualizaci agenta.
5. Vyplňte jeden řádek v tabulce **Profil firmy**.
6. V tabulce **Služby** popište, co nabízíte, a nastavte stav Aktivní.

Do tabulek můžete přidávat vlastní pole i pohledy (třeba kanban podle stavu). Jen nepřejmenovávejte pole, která agent používá.

## Spuštění v Dockeru (doporučeno)

> Podrobný návod krok za krokem pro VPS s Ubuntu (od prvního přihlášení přes Docker po cron) je v [docs/NASAZENI-VPS.md](docs/NASAZENI-VPS.md).

Potřebujete jen [Docker](https://docs.docker.com/get-docker/) s Docker Compose. Python instalovat nemusíte.

```bash
git clone https://github.com/petrkrapek/AIAGENT001.git
cd AIAGENT001
cp .env.example .env               # doplňte ANTHROPIC_API_KEY a údaje Airtable
docker compose build
docker compose run --rm agent airtable-setup   # vytvoří tabulky v Airtable
# v Airtable vyplňte tabulky Profil firmy a Služby
```

**Chat s agentem:**

```bash
docker compose run --rm agent
```

**Jeden úkol a konec:**

```bash
docker compose run --rm agent run "Najdi 5 stavebních firem v Brně a připrav jim nabídky."
```

Kontejner neukládá žádná data, všechno jde do Airtable. Z disku nečte nic, konfiguraci dostane z `.env`. Můžete ho tedy kdykoli smazat a znovu sestavit bez ztráty dat. Po úpravě `.env` stačí agenta spustit znovu, úpravy profilu a služeb v Airtable se projeví i bez restartu (`/reset`). Po změně kódu spusťte `docker compose build`. Kontejner běží pod neprivilegovaným uživatelem.

**Odesílání e-mailů z Dockeru:** doplňte do `.env` údaje SMTP (viz [Odesílání e-mailů](#odesílání-e-mailů)) a ověřte je:

```bash
docker compose run --rm agent smtp-test --to vas@email.cz
```

V chatu (`docker compose run --rm agent`) se vás agent před každým e-mailem zeptá na schválení stejně jako bez Dockeru.

**Pravidelné spouštění přes cron hostitele** (`-T` vypne terminál; v režimu `approve` proto agent jen připraví návrhy, v režimu `auto` odešle ty, které jste schválili v Airtable):

```cron
50 7 * * 1-5  cd /cesta/k/AIAGENT001 && docker compose run --rm -T agent run "Najdi 5 nových firem podle profilu a připrav návrhy." >> $HOME/aiagent.log 2>&1
```

## Instalace bez Dockeru

Je potřeba Python 3.10 nebo novější.

```bash
git clone https://github.com/petrkrapek/AIAGENT001.git
cd AIAGENT001
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env               # doplňte ANTHROPIC_API_KEY a údaje Airtable
aiagent airtable-setup             # vytvoří tabulky v Airtable
```

API klíč vytvoříte na <https://platform.claude.com/settings/keys>.

### Profil firmy a služby

Tohle je nejdůležitější krok. Podle profilu a služeb agent vybírá firmy a píše e-maily. Obojí vyplníte v Airtable (viz [Tabulka Profil firmy](#tabulka-profil-firmy) a [Tabulka Služby](#tabulka-služby)). Když něco chybí, agent se vás na to před psaním e-mailů zeptá.

### Odesílání e-mailů

Odesílání funguje stejně v Dockeru i bez něj. Stačí do `.env` doplnit SMTP údaje vaší schránky. Hodí se samostatná obchodní schránka.

```ini
# Seznam: smtp.seznam.cz, Gmail: smtp.gmail.com, Microsoft 365: smtp.office365.com
SMTP_HOST=smtp.seznam.cz
# 465 = SSL, 587 = STARTTLS
SMTP_PORT=465
SMTP_USER=obchod@vasefirma.cz
# u Gmailu a Microsoftu heslo aplikace, ne běžné heslo
SMTP_PASSWORD=...
SMTP_FROM=obchod@vasefirma.cz
AIAGENT_SEND_MODE=approve
```

Nastavení ověříte příkazem `aiagent smtp-test` (v Dockeru `docker compose run --rm agent smtp-test`). S `--to vas@email.cz` zároveň pošle testovací e-mail.

**Kopie ve složce Odeslané:** SMTP server odeslaný e-mail obvykle do vaší schránky neuloží (výjimkou je třeba Gmail). Aby byly e-maily od agenta vidět ve složce Odeslané, doplňte IMAP stejné schránky:

```ini
# Seznam: imap.seznam.cz (port 993, SSL)
IMAP_HOST=imap.seznam.cz
# přihlášení se převezme ze SMTP_USER a SMTP_PASSWORD; jinak IMAP_USER a IMAP_PASSWORD
# složku Odeslané agent najde sám; jinak IMAP_SENT_FOLDER=Odeslané
```

Agent pak každý odeslaný e-mail uloží jako přečtený do složky Odeslané. Když se kopie uložit nepodaří, e-mail je i tak odeslaný a agent na to upozorní. `smtp-test` ověří i IMAP a vypíše, do které složky bude kopie ukládat. U Gmailu IMAP nenastavujte, e-maily by ve složce Odeslané byly dvakrát. Microsoft 365 přihlášení k IMAP heslem nepovoluje, tam tuto funkci nevyužijete.

Režim odesílání určuje `AIAGENT_SEND_MODE` v `.env` nebo přepínač `--send-mode`:

| Režim | Co dělá | Kdy použít |
|---|---|---|
| `off` | nic neodesílá, jen ukládá návrhy do Airtable | když chcete posílat ručně |
| `approve` | před každým e-mailem ukáže celý text a čeká na „a“ | chat (výchozí, když je SMTP nastavené) |
| `auto` | odesílá bez ptaní, ale jen návrhy, které jste v Airtable schválili (*Schválení návrhu* = Schváleno), beze změny textu; nikdy neposílá follow-upy | běh bez terminálu: cron, `docker compose run -T` |

Ve všech režimech platí denní limit `AIAGENT_DAILY_EMAIL_LIMIT` (výchozí 20, chrání pověst vaší domény), náhodná pauza mezi e-maily (`AIAGENT_SEND_PAUSE`), volitelně doba odesílání jen v pracovní dny a hodiny (`AIAGENT_SEND_HOURS`), blokace kontaktů `do_not_contact` a zákaz opakovaného oslovení. Každý odeslaný e-mail se zapíše do tabulky E-maily v Airtable. E-mail jde vždy jen na adresu uloženou u leadu, a to jedné osobě (žádné kopie ani seznamy adres). Každý e-mail má hlavičku `List-Unsubscribe`, takže poštovní programy u něj ukážou tlačítko pro odhlášení; žádost o odhlášení přijde jako odpověď na adresu odesílatele.

**Ochrana proti dvojímu odeslání:** těsně před odesláním agent zapíše k leadu čas do pole *Odesílání zahájeno* a po úspěšném zápisu do tabulky E-maily ho zase vymaže. Když se výsledek odeslání nepodaří zapsat (výpadek Airtable, spojení se SMTP serverem spadne uprostřed odesílání), pole zůstane vyplněné a agent leadu žádný další e-mail nepošle, ani v příštím běhu. Zkontrolujte ve schránce složku Odeslané: pokud e-mail odešel, nastavte stav Osloven, pokud ne, stačí pole vymazat. Typický postup s režimem `auto`: cron ráno najde firmy a připraví návrhy, vy je během dne schválíte v Airtable a další běh je odešle.

## Použití

V Dockeru pište místo `aiagent` příkaz `docker compose run --rm agent`. Přepínače i zadání zůstávají stejné.

**Interaktivní chat:**

```bash
aiagent
```

Příklady zadání:
- „Najdi 15 stavebních firem v Brně s 10 až 50 zaměstnanci a připrav jim nabídky.“
- „Projdi leady ve stavu drafted a odešli je.“ (u každého se zeptá na schválení)
- „Firma Beta odpověděla, že nemá zájem.“ (nastaví `do_not_contact`)
- „Firma Alfa má zájem, připomeň mi zavolat jim v pondělí.“

V chatu fungují příkazy `/leads`, `/profil`, `/sluzby`, `/tasks`, `/tools`, `/reset`, `/help` a `/exit`. Export do CSV nebo Excelu uděláte přímo v Airtable.

**Jeden úkol a konec** (třeba ranní hledání přes cron):

```bash
aiagent run "Najdi 5 nových firem podle profilu a připrav jim návrhy e-mailů."
```

Když agent běží bez terminálu, nemá se koho zeptat. V režimu `approve` proto e-maily neodešle a připraví jen návrhy, které ráno projdete a schválíte v chatu. Pro odesílání bez dozoru použijte `--send-mode auto` (nebo `AIAGENT_SEND_MODE=auto`), který pošle jen návrhy schválené v Airtable. Přepínač `--yes` schvaluje ostatní akce (např. webhooky), e-maily ale nikdy.

Návratový kód příkazu `run` je 0, když agent úkol dokončil, 1 při chybě nebo nedokončení (odmítnutí, limit délky nebo kroků) a 130 po Ctrl+C, takže ho cron i skripty umí vyhodnotit. Zadání můžete předat i souborem: `aiagent run -f zadani.md` (nebo `-f -` ze standardního vstupu).

```cron
50 7 * * 1-5  cd /cesta/k/AIAGENT001 && .venv/bin/aiagent run "Najdi 5 nových firem podle profilu a připrav návrhy." >> $HOME/aiagent.log 2>&1
```

Další přepínače: `--model`, `--effort low|medium|high|xhigh|max`, `--send-mode` a `--show-thinking`.

## Konfigurace (`.env`)

| Proměnná | Výchozí | Popis |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | API klíč (povinný) |
| `AIRTABLE_TOKEN` | – | osobní token Airtable (povinný) |
| `AIRTABLE_BASE_ID` | – | ID báze Airtable, `app…` (povinné) |
| `AIAGENT_MODEL` | `claude-opus-5-5` | model Claude |
| `AIAGENT_EFFORT` | `medium` | hloubka uvažování (`low` až `max`); vyšší je pečlivější, ale dražší a pomalejší |
| `AIAGENT_DAILY_EMAIL_LIMIT` | `20` | maximum odeslaných e-mailů za den |
| `AIAGENT_SEND_PAUSE` | `30-90` | náhodná pauza v sekundách mezi dvěma e-maily jednoho běhu (`0` = bez pauzy); počítá se do ní i čas, kdy e-mail schvalujete |
| `AIAGENT_SEND_HOURS` | – | doba odesílání ve všední dny, např. `8-17` (od 8:00 do 16:59); prázdné = kdykoli |
| `AIAGENT_TIMEZONE` | `Europe/Prague` | časové pásmo |
| `AIAGENT_WEB_TOOLS` | `1` | vyhledávání a čtení webu |
| `AIAGENT_SEND_MODE` | `approve` se SMTP, jinak `off` | režim odesílání: `off`, `approve`, `auto` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | – | SMTP server pro odesílání |
| `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_SENT_FOLDER` | –, `993` | ukládání kopií odeslaných e-mailů do složky Odeslané (IMAP přes SSL; přihlášení výchozí ze SMTP) |
| `AIAGENT_LOG_LEVEL` | `WARNING` | logování na stderr; `INFO` vypíše u každého volání modelu spotřebu tokenů a request id |
| `AIAGENT_ENV_FILE` | `.env` | jiný konfigurační soubor (načítá se jen z aktuální složky, ne z nadřazených) |
| `NO_COLOR` | – | vypne barevný výstup |

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
├── config.py     nastavení z prostředí / .env
├── airtable.py   klient Airtable API a schéma tabulek (airtable-setup)
├── repos.py      profil, služby, leady, e-maily, úkoly a audit v Airtable
└── tools/
    ├── ares.py   leads.py   mail.py   tasks.py   obchodní nástroje
    ├── web.py    system.py
    └── __init__.py   registr nástrojů
```

Agent volá Messages API se streamováním (dlouhé kroky tak nenarazí na časový limit) a s adaptivním uvažováním. Mezi voláním nástrojů vypisuje krátké průběžné poznámky. Opakující se část požadavku (prompt, profil, nástroje, historie) se ukládá do cache, takže další kroky stojí méně. Kdyby model požadavek odmítl, API ho zkusí zopakovat na záložním modelu (`fallbacks: "default"`). Každé volání nástroje se zapisuje do tabulky Audit v Airtable.

### Přidání vlastního nástroje

Vytvořte soubor v `src/aiagent/tools/` s funkcí `build(config) -> list[Tool]` (nástroje pracující s daty dostávají i repozitář, např. `build(config, repo)`) a zaregistrujte ho v `build_registry()` v `tools/__init__.py`. Vzorem může být `ares.py`. Nástroje mají ve výchozím stavu zapnutý `strict` režim, takže schéma parametrů musí mít `additionalProperties: false` (zajistí helper `schema()`). Popis nástroje (`description`) je pro model nejdůležitější: napište, k čemu nástroj slouží a kdy ho použít.

## Vývoj

```bash
pip install -e ".[dev]"
ruff check .        # linter
mypy                # typová kontrola
pytest --cov=aiagent
```

Testy nepotřebují API klíč ani síť. Claude API, Airtable, ARES i SMTP jsou v nich simulované a testy neovlivní proměnné prostředí ani váš `.env`. Stejné kontroly a sestavení Docker image spouští GitHub Actions (`.github/workflows/ci.yml`) na Pythonu 3.10 a 3.13.

Docker image instaluje přesné verze závislostí z `requirements.lock` (s kontrolními součty). Po změně závislostí v `pyproject.toml` lock vygenerujte znovu:

```bash
uv pip compile pyproject.toml --python-version 3.13 --python-platform linux --generate-hashes -o requirements.lock
```

## Bezpečnost

- Agent čte cizí weby, které mohou obsahovat podstrčené pokyny (prompt injection). Proto každý HTTP požadavek schvalujete vy, požadavky na vnitřní a neveřejné adresy jsou zakázané (i přes přesměrování) a model má výslovný pokyn obsah webů brát jen jako data.
- E-mail jde jen na adresu uloženou u leadu, jedné osobě. Bez terminálu jen schválené návrhy z Airtable.
- Odhlášení a blokace (tabulka Blokované kontakty) kontroluje kód při ukládání leadu, návrhu i při odeslání, nespoléhá na model.
- Hodnoty, které vypadají jako tajné (hlavičky `Authorization`, tokeny, hesla), se v auditu i na obrazovce maskují. Řídicí znaky z textů modelu a webů se před výpisem odstraňují, aby nešlo podvrhnout, co schvalujete.
- Klíče jsou jen v `.env`, který není v gitu ani v Docker image. Kontejner běží pod neprivilegovaným uživatelem a nic neukládá.
