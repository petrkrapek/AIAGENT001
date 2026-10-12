# Nasazení na VPS s Ubuntu 26.04 LTS

Návod krok za krokem: od prvního přihlášení na čerstvou VPS po běžícího agenta a jeho každodenní používání. Příkazy kopírujte po jednom. Řádky začínající `#` jsou komentáře.

Budete potřebovat:
- IP adresu VPS a heslo nebo SSH klíč pro `root` (od poskytovatele),
- API klíč Claude z <https://platform.claude.com/settings/keys>,
- účet v Airtable,
- účet na GitHubu s přístupem k repozitáři `petrkrapek/AIAGENT001`,
- volitelně údaje k e-mailové schránce (SMTP), pokud má agent e-maily i odesílat.

---

## 1. Přihlášení na server

Na svém počítači (Windows: PowerShell, macOS/Linux: Terminál):

```bash
ssh root@IP_ADRESA_SERVERU
```

Při prvním připojení potvrďte otisk serveru slovem `yes`.

## 2. Aktualizace systému a časové pásmo

```bash
apt update && apt upgrade -y
timedatectl set-timezone Europe/Prague
```

Pokud se po aktualizaci objeví hláška, že je potřeba restart, spusťte `reboot`, počkejte minutu a přihlaste se znovu (krok 1).

## 3. Vlastní uživatel místo roota

Agenta je bezpečnější provozovat pod běžným uživatelem. Vytvořte uživatele `agent` (zeptá se na heslo, ostatní otázky stačí odklepnout Enterem):

```bash
adduser agent
usermod -aG sudo agent
```

Pokud se přihlašujete SSH klíčem, zkopírujte ho i novému uživateli:

```bash
rsync --archive --chown=agent:agent ~/.ssh /home/agent
```

Odhlaste se (`exit`) a přihlaste se už jako nový uživatel:

```bash
ssh agent@IP_ADRESA_SERVERU
```

Všechny další kroky dělejte jako uživatel `agent`.

## 4. Firewall

Agent nepotřebuje žádný otevřený port, stačí povolit SSH:

```bash
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```

## 5. Swap (pojistka paměti)

Při 2 GB RAM doporučuji přidat 1 GB swapu:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

## 6. Instalace Dockeru a Gitu

Nejjednodušší je použít balíčky přímo z Ubuntu:

```bash
sudo apt install -y docker.io docker-compose-v2 docker-buildx git
sudo usermod -aG docker $USER
```

Aby se projevilo členství ve skupině `docker`, **odhlaste se a znovu přihlaste** (`exit`, pak `ssh agent@IP_ADRESA_SERVERU`). Ověření:

```bash
docker --version
docker compose version
docker run --rm hello-world
```

Poslední příkaz musí vypsat „Hello from Docker!“.

> Chcete-li nejnovější Docker přímo od výrobce, postupujte podle <https://docs.docker.com/engine/install/ubuntu/>. Pro agenta to není potřeba.

## 7. Stažení agenta z GitHubu

Repozitář je soukromý, takže GitHub bude chtít přihlášení. Místo hesla se používá token:

1. Na GitHubu otevřete **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Repository access: **Only select repositories** → `petrkrapek/AIAGENT001`.
3. Permissions → Repository permissions → **Contents: Read-only**.
4. Token zkopírujte (začíná `github_pat_…`), zobrazí se jen jednou.

Na serveru:

```bash
cd ~
git clone https://github.com/petrkrapek/AIAGENT001.git
cd AIAGENT001
```

Jako *Username* zadejte své uživatelské jméno na GitHubu, jako *Password* vložte token.

Aby se git na token neptal při každé aktualizaci, můžete ho nechat uložit (uloží se do `~/.git-credentials`, čitelného jen pro vás):

```bash
git config --global credential.helper store
```

## 8. Příprava Airtable (v prohlížeči)

1. V Airtable vytvořte novou prázdnou bázi, např. „AI obchodník“. ID báze je v adrese stránky: `airtable.com/appXXXXXXXXXXXXXX/…` (část začínající `app`).
2. Na <https://airtable.com/create/tokens> vytvořte token:
   - Scopes: `data.records:read`, `data.records:write`, `schema.bases:read`, `schema.bases:write`
   - Access: vaše nová báze
3. Token zkopírujte (začíná `pat…`).

## 9. Konfigurace (`.env`)

```bash
cp .env.example .env
nano .env
```

Vyplňte minimálně:

```ini
ANTHROPIC_API_KEY=sk-ant-...
AIRTABLE_TOKEN=pat...
AIRTABLE_BASE_ID=app...
```

Pokud má agent e-maily i odesílat, doplňte SMTP (příklad pro Seznam):

```ini
SMTP_HOST=smtp.seznam.cz
SMTP_PORT=465
SMTP_USER=obchod@vasefirma.cz
SMTP_PASSWORD=...
SMTP_FROM=obchod@vasefirma.cz
```

Uložení v editoru nano: `Ctrl+O`, `Enter`, zavření `Ctrl+X`.

Soubor obsahuje klíče, omezte k němu přístup:

```bash
chmod 600 .env
```

## 10. Sestavení agenta

```bash
docker compose build
```

Poprvé to trvá pár minut. Kontrola:

```bash
docker compose run --rm agent --version
```

## 11. Vytvoření tabulek v Airtable

```bash
docker compose run --rm agent airtable-setup
```

Příkaz vytvoří tabulky Profil firmy, Služby, Leady, E-maily, Úkoly a Audit a skončí hláškou „Airtable je připravený.“

## 12. Vyplnění profilu a služeb (v prohlížeči)

V Airtable:
1. **Profil firmy**: vyplňte jeden řádek (kdo jste, čím se lišíte, ideální zákazník, koho neoslovovat, reference, výzva k akci, tón e-mailů, podpis).
2. **Služby**: každá služba jeden řádek, u těch, které má agent nabízet, nastavte **Stav = Aktivní**.

Podrobný popis polí je v [README](../README.md#tabulka-profil-firmy).

## 13. Test odesílání e-mailů (jen pokud jste nastavili SMTP)

```bash
docker compose run --rm agent smtp-test --to vas@email.cz
```

Na zadanou adresu by měl dorazit testovací e-mail.

## 14. Spuštění agenta

Aby chat nespadl, když vypadne SSH spojení, spouštějte ho v `tmux`:

```bash
tmux new -s agent
docker compose run --rm agent
```

Agent se představí a vypíše, jaké služby a jaký režim odesílání má. Pak čeká na vaše zadání za `vy ›`.

Práce s tmux:
- odpojit se a nechat agenta běžet: `Ctrl+B`, pak `D`,
- vrátit se k němu (i po novém přihlášení): `tmux attach -t agent`.

---

## Co psát agentovi

### Příkazy v chatu

| Příkaz | Co udělá |
|---|---|
| `/help` | nápověda |
| `/profil` | znovu načte a vypíše profil firmy z Airtable |
| `/sluzby` | znovu načte a vypíše aktivní služby |
| `/leads` | přehled leadů podle stavu |
| `/tasks` | otevřené úkoly (follow-upy) |
| `/tools` | nástroje, které agent má |
| `/reset` | nová konverzace (znovu načte profil a služby) |
| `/exit` nebo `Ctrl+D` | ukončení |

`/profil`, `/sluzby` a `/reset` začínají novou konverzaci. Použijte je po úpravě profilu nebo služeb v Airtable.

### Příklady zadání

Hledání a návrhy:
- „Najdi 10 stavebních firem v Brně s 10 až 50 zaměstnanci a připrav jim nabídky.“
- „Najdi e-shopy s vlastním skladem v Jihomoravském kraji, kterým by se hodila služba Audit skladu.“
- „Ukaž mi leady ve stavu drafted.“

Odesílání (jen se SMTP a v režimu `approve`):
- „Odešli návrh leadu Alfa s.r.o.“
- „Projdi leady ve stavu drafted a odešli je.“

U každého e-mailu agent vypíše adresu, předmět a celý text a zeptá se `Povolit? [a/N]`. Napište `a` a Enter pro odeslání, cokoli jiného (nebo jen Enter) odeslání zamítne.

Odpovědi a follow-upy:
- „Firma Beta odpověděla, že nemá zájem.“ → agent ji označí „Nekontaktovat“.
- „Firma Alfa má zájem, připomeň mi zavolat jim v pondělí.“ → vytvoří úkol.
- „Co mám dnes za úkoly?“

Agent může chtít provést akci, kterou musíte schválit (například HTTP požadavek). Postup je stejný: `a` povolí, cokoli jiného zamítne.

### Jeden úkol bez chatu

```bash
docker compose run --rm agent run "Najdi 5 nových firem podle profilu a připrav jim návrhy."
```

Agent úkol udělá a skončí. Delší zadání uložte do souboru a předejte ho přes standardní vstup:

```bash
docker compose run --rm -T agent run -f - < zadani.md
```

---

## Automatické spouštění (cron)

Otevřete plánovač úloh svého uživatele (při prvním spuštění vyberte editor `nano`):

```bash
crontab -e
```

Na konec souboru přidejte například:

```cron
# Každý pracovní den v 7:50 najdi nové firmy a připrav návrhy
50 7 * * 1-5  cd $HOME/AIAGENT001 && /usr/bin/docker compose run --rm -T agent run "Najdi 5 nových firem podle profilu a připrav jim návrhy." >> $HOME/aiagent.log 2>&1
```

Bez terminálu (`-T`) agent nic neschvaluje. V režimu `approve` tedy jen připraví návrhy a odeslat je můžete později v chatu.

Odesílání bez dozoru: nastavte v `.env` hodnotu `AIAGENT_SEND_MODE=auto` a přidejte druhou úlohu:

```cron
# Ve 14:50 odešli návrhy, které jsem během dne schválil v Airtable
50 14 * * 1-5  cd $HOME/AIAGENT001 && /usr/bin/docker compose run --rm -T agent run "Odešli všechny návrhy schválené v Airtable." >> $HOME/aiagent.log 2>&1
```

V režimu `auto` agent odešle jen návrhy, u kterých jste v Airtable přepnuli pole **Schválení návrhu** na **Schváleno**, a to beze změny textu.

Kontrola, co agent v cronu dělal:

```bash
tail -n 100 ~/aiagent.log
```

---

## Aktualizace agenta

```bash
cd ~/AIAGENT001
git pull
docker compose build
docker compose run --rm agent airtable-setup
```

Poslední příkaz doplní do Airtable pole, která případně přibyla v nové verzi. Existující data nemění.

Občas uvolněte místo po starých verzích image:

```bash
docker system prune -f
```

---

## Když něco nejde

| Hláška | Řešení |
|---|---|
| `permission denied … docker.sock` | Po `usermod -aG docker` jste se neodhlásili a nepřihlásili znovu. |
| `Agent ukládá data do Airtable: nastavte AIRTABLE_TOKEN…` | V `.env` chybí token nebo ID báze. |
| `Airtable chyba 401` | Neplatný `AIRTABLE_TOKEN`. |
| `Airtable chyba 403` | Token nemá potřebná oprávnění nebo přístup k bázi (krok 8). |
| `Airtable chyba 404` / `422` | Spusťte `docker compose run --rm agent airtable-setup`. |
| `Neplatný API klíč` / `Chybí API klíč` | Zkontrolujte `ANTHROPIC_API_KEY` v `.env`. |
| `SMTP nefunguje: … timed out` | Poskytovatel VPS může blokovat odchozí SMTP. Zkuste port 587 (STARTTLS) nebo 465 (SSL), případně požádejte poskytovatele o odblokování. |
| Agent nevidí změny profilu/služeb | V chatu napište `/reset`. |

Podrobnější výpis (spotřeba tokenů a request id u každého volání modelu) zapnete v `.env` řádkem `AIAGENT_LOG_LEVEL=INFO`.
