# Prevádzková príručka – Agent dispečer na VPS

Príručka pre správcu servera: inštalácia, nastavenie schránky a odosielania, aktualizácie, zálohy,
monitoring a postup pri problémoch. Je písaná pre človeka, ktorý nemusí byť špecialista na Linux –
každý krok má presné príkazy.

> **Dôležité.** Agent dispečer len **odporúča** (PROMPT.md, kapitola 2.2). Prevádzkové e-maily naďalej
> chodia aj ľuďom – agent je ďalší príjemca. Keď agent nebeží, dispečing pracuje ako doteraz.
> Výpadok agenta preto nikdy nezastaví dopravu; monitoring má len včas povedať „pracujte ručne“.

**Konvencie:** príkazy so `sudo` spúšťa administrátor servera. Ostatné príkazy sa spúšťajú ako
používateľ `vlaky` v adresári aplikácie:

```bash
sudo -iu vlaky
cd /opt/vlaky/app
```

Namiesto `agent.dopravca.example`, `dopravca.example` a `203.0.113.10` doplň svoje hodnoty.

## Obsah

1. [Ako to funguje](#1-ako-to-funguje)
2. [Čo potrebujete](#2-čo-potrebujete)
3. [DNS](#3-dns)
4. [Inštalácia s Dockerom – krok za krokom](#4-inštalácia-s-dockerom--krok-za-krokom)
5. [Schránka agenta](#5-schránka-agenta)
6. [Odosielanie upozornení (SMTP, SPF/DKIM/DMARC, Teams, SMS, výpadok servera správ)](#6-odosielanie-upozornení)
7. [Interný zoznam príjemcov a tieňová prevádzka](#7-interný-zoznam-príjemcov-a-tieňová-prevádzka)
8. [Jazykový model (LLM)](#8-jazykový-model-llm)
9. [Železničná sieť z OpenStreetMap](#9-železničná-sieť-z-openstreetmap)
10. [Aktualizácie](#10-aktualizácie)
11. [Zálohy a test obnovy](#11-zálohy-a-test-obnovy)
12. [Monitoring](#12-monitoring)
13. [Postup pri incidentoch](#13-postup-pri-incidentoch)
14. [GDPR a bezpečnosť](#14-gdpr-a-bezpečnosť)
15. [Náklady (orientačne)](#15-náklady-orientačne)
16. [Inštalácia bez Dockera (systemd)](#16-inštalácia-bez-dockera-systemd)
17. [VPN cez WireGuard (voliteľné)](#17-vpn-cez-wireguard-voliteľné)
18. [Prehľad príkazov a súborov](#18-prehľad-príkazov-a-súborov)

---

## 1. Ako to funguje

```
                         Internet
                            │  len 80/443 (SSH len z VPN / povolenej IP)
                     ┌──────▼──────┐
  dispečer ─HTTPS──► │    Caddy    │  certifikát Let's Encrypt, bezpečnostné hlavičky, log
  (prehliadač)       └──────┬──────┘
                            │ sieť "frontend"
                     ┌──────▼──────┐        ┌──────────────┐
                     │     web     │◄──────►│  PostgreSQL  │  sieť "backend" – bez internetu,
                     └─────────────┘        └──────▲───────┘  bez portov na serveri
                     ┌─────────────┐               │
                     │    agent    │───────────────┘
                     └──────┬──────┘
                            │ sieť "egress" (len odchádzajúce spojenia)
     ┌──────────────┬───────┴──────┬──────────────────┬───────────────────────┐
 schránka agenta   LLM API     SMTP / Teams / SMS   heartbeat ──► externý monitoring
 (Graph / IMAP)  (len extrakcia) (len interní ľudia)                 └─► upozorní dispečing a správcu
```

| Kontajner | Úloha | Pri páde |
|---|---|---|
| `caddy` | jediný verejný vstup: HTTPS, presmerovanie HTTP → HTTPS, hlavičky, kompresia, prístupový log | automatický reštart |
| `web` | webová aplikácia (mapa, fronta, odporúčania), `GET /health` bez prihlásenia | automatický reštart |
| `agent` | číta schránku, plánuje, posiela odporúčania len na interný zoznam, heartbeat | automatický reštart |
| `db` | PostgreSQL 16 | automatický reštart |
| `init-db` | pri každom `docker compose up` vytvorí chýbajúce tabuľky a nahrá hraničné priechody, potom skončí | – |

Dáta na serveri (pomenované zväzky Dockeru): `vlaky_pgdata` (databáza), `vlaky_schranka`,
`vlaky_odoslane` (kópie odoslaných správ – audit), `vlaky_caddy_data` (certifikáty), `vlaky_caddy_logs`.
Kontajnery aplikácie bežia ako neprivilegovaný používateľ s koreňovým súborovým systémom len na čítanie.

## 2. Čo potrebujete

- **VPS v EÚ:** 4 vCPU, 8 – 16 GB RAM, 160 GB SSD, **Ubuntu 24.04 LTS**, verejná IPv4 (IPv6 vítaná).
  Poskytovateľ s dátovým centrom v EÚ, so zmluvou o spracúvaní osobných údajov (DPA), ideálne so
  šifrovaným diskom a snímkami (snapshots). Príklady poskytovateľov s dátovými centrami v EÚ: Hetzner,
  OVHcloud, Scaleway – len príklady, nie odporúčanie; podmienky a umiestnenie si overte.
- **Doména alebo subdoména** pre aplikáciu, napr. `agent.dopravca.example`, a prístup k nastaveniu DNS.
- **Schránka agenta** na firemnej doméne, napr. `agent.dispecing@dopravca.example` (kapitola 5).
- **Odosielanie e-mailov** (SMTP) zo schránky alebo služby s SPF/DKIM/DMARC (kapitola 6).
- **Externý monitoring** (napr. healthchecks.io + služba na kontrolu webu) – kapitola 12.
- **Úložisko na zálohy mimo servera** (iný poskytovateľ alebo aspoň iný región v EÚ).
- **SSH kľúč správcu** (`ssh-keygen -t ed25519`) a pevná IP adresa alebo VPN (kapitola 17).
- **Firemný trezor hesiel** – uloží sa doň `.env`, heslo k zálohám, prístupy.
- (voliteľne) **Účet u poskytovateľa LLM** s DPA (kapitola 8). Bez LLM agent funguje na pravidlách.
- **Ľudia:** správca servera a jeho zástupca, vedúci dispečingu (tieňová prevádzka), kontakty na
  upozornenia. Prevádzkový denník (kto, kedy, čo urobil).

## 3. DNS

1. U správcu domény vytvor záznam **A** `agent.dopravca.example → verejná IPv4 servera`
   (a **AAAA** na IPv6 – len ak IPv6 na serveri naozaj funguje; inak AAAA nevytváraj, Let's Encrypt by zlyhal).
   TTL napr. 300 s (ľahšia zmena pri sťahovaní servera).
2. Over (z počítača): `dig +short agent.dopravca.example` alebo `nslookup agent.dopravca.example` → IP servera.
3. Voliteľne záznam **CAA** na doméne: `dopravca.example. CAA 0 issue "letsencrypt.org"`
   (ak CAA záznamy už máte, pridajte Let's Encrypt; ak Caddy použije záložného vydavateľa ZeroSSL,
   treba povoliť aj `sectigo.com`).
4. Porty **80 a 443** musia byť dostupné z internetu (Let's Encrypt overuje doménu cez ne) – setup.sh to zariadi.
5. Záznamy SPF, DKIM a DMARC pre doménu, z ktorej agent posiela e-maily: kapitola 6.

## 4. Inštalácia s Dockerom – krok za krokom

### 4.1 Prvé prihlásenie a účet administrátora

Ak poskytovateľ dal prístup len pre `root`, vytvor si osobný účet so sudo (z vášho počítača):

```bash
ssh root@203.0.113.10
adduser spravca                                      # zadaj heslo (bude sa pýtať pri sudo)
usermod -aG sudo spravca
rsync --archive --chown=spravca:spravca ~/.ssh /home/spravca   # skopíruje SSH kľúč z účtu root
exit
ssh spravca@203.0.113.10                             # NOVÉ prihlásenie kľúčom – musí fungovať
sudo -v
```

### 4.2 Príprava servera (`setup.sh`)

Zisti verejnú IP, z ktorej sa pripájaš (na svojom počítači: `curl -s https://ifconfig.me`), alebo
rozsah VPN (kapitola 17). Na serveri:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/ORGANIZACIA/VLAKY.git /tmp/vlaky-setup      # adresa vášho repozitára
sudo bash /tmp/vlaky-setup/deploy/vps/setup.sh --ssh-allow 203.0.113.10/32 --timezone Europe/Bratislava
```

Skript vypíše plán a pýta sa na potvrdenie. Urobí: aktualizácie a automatické bezpečnostné
aktualizácie, synchronizáciu času, limity logov, swap, Docker z oficiálneho repozitára, používateľa
`vlaky` a adresáre `/opt/vlaky`, fail2ban a firewall UFW (dnu len 80/443 a SSH z `--ssh-allow`).
**Pred zapnutím firewallu hlasno varuje** a odmietne pokračovať, ak tvoja aktuálna IP nie je
v povolenom rozsahu. Je idempotentný – dá sa spustiť znova (napr. po zmene IP).
Voľby: `sudo bash …/setup.sh --help` (napr. `--wireguard-port 51820`, `--auto-reboot 03:40`).

Ak skript na konci napíše, že treba reštart: `sudo reboot`.

### 4.3 Sprísnenie SSH (`harden-ssh.sh`)

```bash
sudo bash /tmp/vlaky-setup/deploy/vps/harden-ssh.sh
```

Zakáže prihlásenie heslom a priame prihlásenie `root`. **Odmietne to urobiť**, ak tvoj účet nemá
sudo alebo platný kľúč v `~/.ssh/authorized_keys`. Po zmene **nezatváraj okno** a v novom okne over
`ssh spravca@203.0.113.10` a `sudo -v`. Návrat: `sudo rm /etc/ssh/sshd_config.d/00-vlaky-hardening.conf && sudo systemctl reload ssh`.

### 4.4 Stiahnutie aplikácie

Repozitár sťahuje používateľ `vlaky` (bude ho aj aktualizovať). Pri súkromnom repozitári použi
kľúč len na čítanie („deploy key“):

```bash
sudo -iu vlaky
ssh-keygen -t ed25519 -C "vlaky@agent" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub        # vlož v GitHub/GitLab: Settings → Deploy keys (bez práva zápisu)
git clone git@github.com:ORGANIZACIA/VLAKY.git /opt/vlaky/app
exit
rm -rf /tmp/vlaky-setup
```

### 4.5 Konfigurácia (`.env`)

```bash
sudo -iu vlaky
cd /opt/vlaky/app
cp .env.example .env && chmod 600 .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^VLAKY_SECRET_KEY=.*/VLAKY_SECRET_KEY=$(openssl rand -hex 32)/" .env
sed -i "s/^VLAKY_POSITIONS_TOKEN=.*/VLAKY_POSITIONS_TOKEN=$(openssl rand -hex 32)/" .env
nano .env
```

Každé nastavenie má v `.env.example` slovenský komentár. Web v produkcii (`VLAKY_ENV=prod`) odmietne
štart, ak `VLAKY_SECRET_KEY` nemá aspoň 32 náhodných znakov alebo `VLAKY_PUBLIC_URL` nezačína `https://`
(chyba je v `docker compose logs web`). Ukážkové dáta (`python -m vlaky demo`) a simulátor polohy sa
v produkcii nespustia. Na začiatok stačí:

| Nastavenie | Hodnota |
|---|---|
| `VLAKY_DOMAIN` | `agent.dopravca.example` |
| `VLAKY_CARRIER_NAME` | názov dopravcu |
| `VLAKY_ALLOWED_SENDERS` | domény ŽSR, partnerov, zákazníkov, vlastná doména |
| `VLAKY_NOTIFY_EMAILS` | dispečeri (interný zoznam) |
| `VLAKY_SHADOW_MODE` / `VLAKY_SHADOW_EMAILS` | `true` / vedúci dispečingu (kapitola 7) |
| `VLAKY_SMTP_*` | odosielanie (kapitola 6) |
| `VLAKY_HEARTBEAT_URL` | ping URL externého monitoringu (kapitola 12) |
| `VLAKY_MAIL_SOURCE` | zatiaľ `directory`; po nastavení schránky `graph` alebo `imap` (kapitola 5) |

**Celý obsah `.env` ulož do firemného trezoru hesiel.** Po každej zmene `.env` použi
`docker compose up -d` (príkaz `docker compose restart` zmeny v `.env` nenačíta).

### 4.6 Spustenie

```bash
docker compose build             # zostaví obraz aplikácie (prvýkrát niekoľko minút)
docker compose up -d             # db → init-db → web + agent → caddy
docker compose ps                # db, web, caddy: "healthy"; agent: "healthy" do ~3 min
docker compose logs -f caddy     # hľadaj "certificate obtained successfully"; ukončenie Ctrl+C
curl https://agent.dopravca.example/health     # {"status":"ok","db":"ok",...}
```

Ak certifikát nevznikne, pozri kapitolu 13 („Problém s certifikátom“).

### 4.7 Databáza (`init-db`)

Tabuľky a hraničné priechody vytvorí automaticky jednorazová služba `init-db` pri každom
`docker compose up` (je bezpečné ju spúšťať opakovane). Ručne: `docker compose run --rm init-db`
(výpis „Databáza pripravená, hraničných priechodov: 23“).

### 4.8 Prvý používateľ

```bash
read -rsp "Heslo pre nového používateľa: " PW; echo
docker compose exec web python -m vlaky create-user admin --role admin --password "$PW" \
    --full-name "Meno Priezvisko" --email spravca@dopravca.example
unset PW
```

Roly: `dispecer`, `veduci`, `planovac`, `obchod`, `admin`. Heslo sa takto neuloží do histórie príkazov.
Prihlás sa na `https://agent.dopravca.example` a ďalších používateľov vytváraj podľa potreby.

### 4.9 Skúšobná správa

```bash
docker compose exec agent python -m vlaky test-notify
```

Vypíše pre každý kanál príjemcu a stav. V tieňovej prevádzke ide správa **len** na
`VLAKY_SHADOW_EMAILS` (SMS a Teams sú vypnuté); ostatní príjemcovia sa vôbec neskúšajú.
Kópia každej správy je aj vo zväzku `odoslane` (audit). Ak správa nepríde: kapitola 6 a
`docker compose logs --tail 100 agent`.

### 4.10 Zálohy, kontrola servera a monitoring

```bash
exit                                                       # späť na účet správcu
sudo bash /opt/vlaky/app/deploy/vps/setup.sh --ssh-allow 203.0.113.10/32 --yes   # doinštaluje časovače
sudo nano /etc/vlaky/ops.env                               # záloha mimo servera, ping URL monitoringu
sudo systemctl start vlaky-backup && journalctl -u vlaky-backup -n 30   # prvá záloha
sudo -iu vlaky bash -c 'cd /opt/vlaky/app && deploy/backup/restore.sh --test && deploy/monitoring/check.sh'
```

Podrobnosti: [deploy/backup/README.md](../deploy/backup/README.md) a
[deploy/monitoring/README.md](../deploy/monitoring/README.md).

### 4.11 Kontrolný zoznam po inštalácii

- [ ] `https://agent.dopravca.example` má platný certifikát, `http://` presmeruje na `https://`
- [ ] `docker compose ps` – všetky služby `healthy`
- [ ] prihlásenie do aplikácie funguje, heslo admina je v trezore
- [ ] `test-notify` doručil e-mail vedúcemu dispečingu
- [ ] schránka agenta nastavená (kapitola 5), skúšobný e-mail od povoleného odosielateľa sa spracoval
- [ ] SPF, DKIM, DMARC domény odosielateľa sú „pass“ (kapitola 6)
- [ ] externý monitoring: heartbeat, `/health`, záloha, kontrola servera; skúška `docker compose stop agent` vyvolala upozornenie
- [ ] prvá záloha, záloha mimo servera a `restore.sh --test` úspešné
- [ ] `ssh root@…` a prihlásenie heslom nefungujú; SSH len z povolenej IP/VPN
- [ ] `.env`, `/etc/vlaky/ops.env`, heslo k zálohám sú v trezore hesiel
- [ ] DPIA a informovanie zamestnancov (kapitola 14) pred ostrou prevádzkou

## 5. Schránka agenta

Agent potrebuje **vlastnú schránku** na firemnej doméne (nie bezplatnú), napr. `agent.dispecing@dopravca.example`.
Prevádzkové e-maily musia **naďalej chodiť aj ľuďom** – agent dostáva kópiu:

- **najlepšie:** adresa agenta je priamo v distribučných zoznamoch ŽSR, partnerov a zákazníkov
  (pôvodné overenie SPF/DKIM/DMARC zostane zachované),
- **alebo** kópia na poštovom serveri: Microsoft 365 – pravidlo toku pošty (Exchange admin center →
  Mail flow → Rules: „ak je príjemca dispecing@… → pridať príjemcu (Bcc) agent.dispecing@…“);
  Google Workspace – smerovanie „Also deliver to“ (Admin console → Gmail → Routing),
- **nepoužívaj** preposielanie pravidlom v Outlooku/Gmaile používateľa – mení odosielateľa a overenie
  odosielateľa by zlyhalo (správy by skončili v karanténe).

### 5.1 Microsoft 365 (Microsoft Graph) – odporúčané pre M365

Exchange Online nepovoľuje IMAP s menom a heslom, preto sa používa Microsoft Graph s aplikačným
oprávnením obmedzeným **len na schránku agenta**.

1. **Schránka:** Microsoft 365 admin center → Teams & groups → Shared mailboxes → Add –
   `agent.dispecing@dopravca.example` (zdieľaná schránka nepotrebuje licenciu do 50 GB; prihlásenie do nej je zakázané).
2. **Registrácia aplikácie:** Microsoft Entra admin center → Applications → App registrations →
   New registration: názov „Agent dispečer – čítanie schránky“, „Accounts in this organizational directory only“,
   bez Redirect URI. Zapíš **Application (client) ID** a **Directory (tenant) ID**.
3. **Oprávnenie:** API permissions → Add a permission → Microsoft Graph → **Application permissions** →
   `Mail.Read` → Add → **Grant admin consent**. (Na čítanie schránky stačí `Mail.Read`. Ak má agent upozornenia
   posielať aj cez Microsoft Graph – `VLAKY_NOTIFY_TRANSPORT=graph`, kapitola 6.1 –, pridaj aj `Mail.Send`;
   obmedzenie na jednu schránku v kroku 5 platí pre obe oprávnenia.)
4. **Tajomstvo:** Certificates & secrets → New client secret – platnosť napr. 12 mesiacov (Entra ID dovolí najviac 24).
   Hodnotu (Value) hneď skopíruj; **dátum vypršania zapíš do kalendára správcu** s mesačným predstihom.
5. **Obmedzenie na jednu schránku** (inak by aplikácia mohla čítať všetky schránky firmy!) –
   v PowerShell s modulom ExchangeOnlineManagement:
   ```powershell
   Install-Module ExchangeOnlineManagement        # raz
   Connect-ExchangeOnline -UserPrincipalName spravca@dopravca.example
   New-DistributionGroup -Name "Agent dispecer - pristup aplikacie" -Alias agent-dispecer-pristup `
       -Type Security -Members agent.dispecing@dopravca.example
   New-ApplicationAccessPolicy -AppId <CLIENT_ID> -PolicyScopeGroupId agent-dispecer-pristup@dopravca.example `
       -AccessRight RestrictAccess -Description "Agent dispecer smie citat len svoju schranku"
   Test-ApplicationAccessPolicy -Identity agent.dispecing@dopravca.example -AppId <CLIENT_ID>   # Granted
   Test-ApplicationAccessPolicy -Identity niekto.iny@dopravca.example     -AppId <CLIENT_ID>   # Denied
   ```
   Zmena začne platiť o niekoľko desiatok minút. Microsoft odporúča aj novší spôsob „RBAC for
   Applications“ v Exchange Online (rola `Application Mail.Read` s rozsahom na jednu schránku) –
   princíp je rovnaký; pred nastavením over aktuálny postup v dokumentácii Microsoftu.
6. **Nastavenie v `.env`:**
   ```ini
   VLAKY_MAIL_SOURCE=graph
   VLAKY_GRAPH_TENANT_ID=<Directory (tenant) ID>
   VLAKY_GRAPH_CLIENT_ID=<Application (client) ID>
   VLAKY_GRAPH_CLIENT_SECRET=<Value>
   VLAKY_GRAPH_MAILBOX=agent.dispecing@dopravca.example
   ```
   `docker compose up -d` a `docker compose logs -f agent`; v aplikácii stránka stavu agenta.
7. Server potrebuje odchádzajúci prístup na `login.microsoftonline.com` a `graph.microsoft.com` (HTTPS).

### 5.2 Google Workspace

**IMAP s heslom aplikácie** (podporované aplikáciou):

1. Admin console: vytvor používateľa `agent.dispecing@dopravca.example` (vyžaduje licenciu) a povoľ IMAP
   (Apps → Google Workspace → Gmail → End User Access → POP and IMAP access).
2. Prihlás sa ako agent, zapni **dvojstupňové overenie** a vytvor **heslo aplikácie**
   (Google účet → Security → App passwords). Heslo ulož do trezoru. (Podmienky Google sa menia – over aktuálny stav.)
3. `.env`:
   ```ini
   VLAKY_MAIL_SOURCE=imap
   VLAKY_IMAP_HOST=imap.gmail.com
   VLAKY_IMAP_PORT=993
   VLAKY_IMAP_USER=agent.dispecing@dopravca.example
   VLAKY_IMAP_PASSWORD=<heslo aplikácie bez medzier>
   VLAKY_IMAP_FOLDER=INBOX
   VLAKY_TRUSTED_AUTHSERV_IDS=mx.google.com
   ```

**Gmail API** (servisný účet s delegovaním na doméne, rozsah `gmail.readonly`) je bezpečnejšie
(bez hesla), ale aplikácia ho zatiaľ nepodporuje – je to požiadavka na vývoj.

### 5.3 Iný poštový server (IMAP)

Vyhradená schránka, IMAP cez TLS (port 993), silné jedinečné heslo, priečinok `INBOX`:
`VLAKY_MAIL_SOURCE=imap`, `VLAKY_IMAP_HOST`, `VLAKY_IMAP_USER`, `VLAKY_IMAP_PASSWORD`.
Do `VLAKY_TRUSTED_AUTHSERV_IDS` daj authserv-id vášho servera (prvé slovo hlavičky
`Authentication-Results` v zdrojovom texte prijatého e-mailu).

### 5.4 Skúšobný režim bez schránky (`directory`)

S `VLAKY_MAIL_SOURCE=directory` agent spracúva súbory `.eml` vložené do zväzku schránky
(spracované presunie do podadresára `spracovane`). Vhodné na skúšky s ukážkovými (FIKTÍVNYMI) e-mailmi:

```bash
docker compose exec -T agent sh -c 'cat > /app/data/schranka/skuska.eml' < data/ukazkove_emaily/01_rov_vyluka_kolaje_poprad_lm.eml
```

### 5.5 Overenie odosielateľov

- `VLAKY_ALLOWED_SENDERS` – len tieto domény/adresy sa spracujú; ostatné idú do karantény (agent ich spomenie v súhrne).
- `VLAKY_REQUIRE_DMARC_PASS=true` – vyžaduje výsledok SPF/DKIM/DMARC „pass“, ktorý zapísal **váš**
  poštový server (`VLAKY_TRUSTED_AUTHSERV_IDS`), aby sa nedalo podvrhnúť hlavičkou v e-maile.
- **Preposlaný obsah** (príloha message/rfc822, priložený .eml, vložené preposlanie): pôvodný odosielateľ
  musí byť povolený a overený, inak karanténa. Výnimka: preposiela overený odosielateľ ŽSR (citovaná správa
  susednej správy, interný ROV bez hlavičiek overenia) – e-mail nejde do karantény, pôvodný odosielateľ sa zapíše
  do `forwarded_from` a všetko z neho ide na potvrdenie dispečerom. Textová príloha sa považuje za priložený
  e-mail, len keď začína skutočným blokom hlavičiek (From s adresou a aspoň jedna z hlavičiek Date, Subject,
  Message-ID, Received, MIME-Version).
- **Rovnaké Message-ID od iného odosielateľa** sa uloží ako samostatný e-mail (Message-ID s dodatkom `#<sha>`)
  a zapíše sa do auditu (`email_message_id_kolizia`, kapitola 13); potichu sa preskočí len opakované doručenie
  od toho istého odosielateľa. Microsoft Graph preto číta aj adresu odosielateľa (pole `from`, stačí oprávnenie
  `Mail.Read`).
- Obsah e-mailov je pre agenta len dáta – pokyny v e-mailoch nevykonáva a odkazy neotvára.

## 6. Odosielanie upozornení

### 6.1 SMTP

Agent posiela e-maily cez `VLAKY_SMTP_HOST`/`PORT` (587 + STARTTLS) s menom a heslom
(`VLAKY_SMTP_USER`, `VLAKY_SMTP_PASSWORD`) z adresy `VLAKY_SMTP_FROM`.

- **Microsoft 365:** Microsoft ukončuje odosielanie cez SMTP AUTH s menom a heslom (Basic auth)
  v Exchange Online – over aktuálny stav. Možnosti: (a) SMTP relé cez konektor Exchange Online
  (port 25, overenie podľa IP servera; pozor, mnohí poskytovatelia VPS port 25 blokujú),
  (b) transakčná e-mailová služba so serverom v EÚ s SPF/DKIM pre vašu doménu,
  (c) odosielanie cez Microsoft Graph zo schránky agenta: `VLAKY_NOTIFY_TRANSPORT=graph` s údajmi
  `VLAKY_GRAPH_*` z kapitoly 5.1 a oprávnením `Mail.Send` (obmedzeným na schránku agenta). Agent
  posiela vždy jednému príjemcovi zo zoznamu, bez kópií.
- **Google Workspace:** `smtp.gmail.com`, port 587, účet agenta a heslo aplikácie (kapitola 5.2),
  alebo služba SMTP relay v Admin console.
- **Iný poskytovateľ:** podľa jeho návodu (port 587, STARTTLS).

Overenie: `docker compose exec agent python -m vlaky test-notify`.

### 6.2 SPF, DKIM a DMARC pre doménu odosielateľa

Bez nich skončia upozornenia v spame alebo sa nedoručia. V DNS domény z `VLAKY_SMTP_FROM`:

| Záznam | Príklad | Poznámka |
|---|---|---|
| SPF (TXT na `dopravca.example`) | `v=spf1 include:spf.protection.outlook.com -all` (M365) / `v=spf1 include:_spf.google.com ~all` (Google) | jeden SPF záznam na doménu; pridaj `include` služby, ktorá posiela; priamo z VPS neposielaj |
| DKIM | M365: Microsoft Defender → Email & collaboration → Policies → Email authentication settings → DKIM (2× CNAME); Google: Admin console → Gmail → Authenticate email (TXT `google._domainkey`) | zapnúť podpisovanie |
| DMARC (TXT na `_dmarc.dopravca.example`) | `v=DMARC1; p=none; rua=mailto:dmarc@dopravca.example` | po 2 – 4 týždňoch bez problémov `p=quarantine`, neskôr `p=reject` |

Kontrola: v prijatom upozornení zobraz zdrojový text – v `Authentication-Results` má byť
`spf=pass`, `dkim=pass`, `dmarc=pass`.

### 6.3 Druhá poistka: agent smie písať len do firmy

Aplikácia technicky odmietne iných príjemcov ako interný zoznam (kapitola 7). Odporúčaná druhá poistka
na poštovom serveri (PROMPT.md 2.2 – „SMTP relé odmietne iné adresy“):

- **Microsoft 365:** Exchange admin center → Mail flow → Rules → nové pravidlo: *ak je odosielateľ*
  `agent.dispecing@dopravca.example` *a príjemca je mimo organizácie* → *zablokovať správu* (s vysvetlením).
- **Google Workspace:** Admin console → Gmail → Compliance → **Restrict delivery** pre organizačnú
  jednotku/skupinu agenta – povoliť len vlastnú doménu.
- **Iná služba:** obmedzenie príjemcov alebo samostatný účet len pre internú doménu, ak to služba umožňuje.

### 6.4 Microsoft Teams a SMS

- **Teams** (`VLAKY_TEAMS_WEBHOOK_URL`): v kanáli dispečingu vytvor webhook cez **Workflows**
  (Power Automate, šablóna typu „Post to a channel when a webhook request is received“). Agent posiela
  JSON `{"text": "…"}`; tok nastav tak, aby text z požiadavky zverejnil v kanáli. Staré konektory
  „Incoming Webhook“ Microsoft postupne ruší – over aktuálny stav. URL webhooku je tajomstvo.
- **SMS** (`VLAKY_SMS_GATEWAY_URL`, `VLAKY_SMS_GATEWAY_TOKEN`): HTTP brána, ktorá prijme
  `POST` JSON `{"to": "+421…", "text": "…"}` s hlavičkou `Authorization: Bearer <token>`.
  Vyber poskytovateľa v EÚ s DPA. SMS chodia len pri priorite KRITICKÉ a len na `VLAKY_NOTIFY_PHONES`.
- V tieňovej prevádzke sú Teams aj SMS vypnuté; vyskúšaj ich pri prechode do ostrej prevádzky
  (`test-notify` po vypnutí tieňového režimu).

### 6.5 Výpadok servera správ (SMTP, Teams, SMS)

- **Limit spojenia:** agent sa na server správ pripája s limitom 10 s (pri SMTP potom 30 s na odpovede
  servera); limit sa dá zmeniť nastavením `VLAKY_NOTIFY_CONNECT_TIMEOUT_S`.
- **Nedostupný kanál:** keď sa spojenie nepodarí alebo server hlási výpadok či preťaženie (SMTP 421, HTTP 5xx,
  408, 429), ďalšie správy na tom istom kanáli (e-mail, Teams, SMS) sa v tom istom cykle agenta už nepripájajú –
  dostanú stav „chyba“ s textom „Neodoslané – kanál … je teraz nedostupný“. Nedostupný alebo ticho zahadzujúci
  server tak nezdrží čítanie schránky. V zázname správy začína chyba spojenia alebo servera textom „Chyba kanála:“.
- **Odmietnutie jednej správy alebo jedného príjemcu** (napr. SMTP 550, SMS brána 403 pre jedno číslo) ostatných
  nezdrží – dostanú správu hneď.
- **Opakovanie:** nedoručené správy sa opakujú s rastúcim odstupom (po 5, 10, 20, 40 a potom každých 60 min)
  a len príjemcom, ktorým neprišli – ostatní ich druhý raz nedostanú. Pri výpadku kanála sa správa zopakuje aj
  hneď, keď tomu istému príjemcovi prišla iná správa (kanál opäť funguje), a KRITICKÉ odporúčania (ESKALOVAŤ)
  sa skúšajú každých 5 min – po obnovení servera prídu do 5 min.
- **Čo sa opakuje:** odporúčania, návrhy k objednávkam, súhrn (najdlhšie do ďalšieho súhrnu), denná správa
  a odovzdávka služby – vždy tá istá správa, nie nová. Súhrn, denná správa a odovzdávka sú vybavené, až keď
  prišli každému dispečerovi zo zoznamu. Aktuálne odporúčanie sa skúša doručiť najviac 48 h (nie pre ukončený
  vlak); nahradené alebo rozhodnuté odporúčanie sa už neposiela. Upozornenia na rušňovodiča a státie vlaku sa
  skúšajú v každom cykle, kým platia (pri výpadku najviac jeden pokus o spojenie na kanál za cyklus).

Sledovanie nedoručených správ: kapitola 12; postup: kapitola 13 („Upozornenia sa nedoručujú“).

## 7. Interný zoznam príjemcov a tieňová prevádzka

**Interný zoznam** – jediné miesta, kam agent smie niečo poslať:
`VLAKY_NOTIFY_EMAILS` (dispečeri), `VLAKY_NOTIFY_PHONES` (SMS), `VLAKY_TEAMS_WEBHOOK_URL`.
Iného príjemcu odosielacia služba zablokuje a zapíše do auditu. Agent nemá prístup do systémov ŽSR
ani zákazníkov; koncepty správ pre nich posiela dispečer sám.

**Tieňová prevádzka** (PROMPT.md 2.7) – prvých **4 – 6 týždňov**:

1. `VLAKY_SHADOW_MODE=true`, `VLAKY_SHADOW_EMAILS=veduci.dispecingu@dopravca.example` (prípadne testovací kanál).
   Agent beží naostro, ale odporúčania idú **len** na tieňové adresy; dispečeri pracujú ako doteraz.
2. Vedúci dispečingu porovnáva odporúčania s tým, čo urobil dispečer, a eviduje: nezachytené zmeny
   (cieľ 0), správnosť vytiahnutých údajov, zhodu odporúčaní, falošné poplachy.
3. Prechod do ostrej prevádzky (rozhodnutie vedenia, zapísať do denníka):
   ```bash
   nano .env                     # VLAKY_SHADOW_MODE=false, skontroluj VLAKY_NOTIFY_*
   docker compose up -d
   docker compose exec agent python -m vlaky test-notify    # musia prísť e-mail, Teams, SMS
   ```
4. Návrat do tieňového režimu kedykoľvek rovnako (`true` + `docker compose up -d`).

## 8. Jazykový model (LLM)

LLM pomáha **len porozumieť textu** e-mailov a príloh. Nemá žiadne nástroje, nič neodosiela;
jeho výstup overujú pravidlá a neistá alebo dôležitá udalosť ide dispečerovi na potvrdenie.
Bez LLM (`VLAKY_LLM_PROVIDER=none`) agent používa deterministické pravidlá (istota najviac 0,85 –
viac udalostí ide na potvrdenie).

1. **Zmluva a spracovanie v EÚ:** pred zapnutím podpíš s poskytovateľom DPA, over umiestnenie
   spracovania a dobu uchovávania vstupov. Pozor: priame API Anthropic (`anthropic`) dnes neponúka
   spracovanie výlučne v EÚ (voľba geografie pozná „us“ a „global“). Ak firemná politika vyžaduje
   spracovanie v EÚ, zvážte Claude cez Google Cloud Vertex AI (región EÚ) alebo Amazon Bedrock
   (regióny EÚ), resp. Azure OpenAI v EÚ / OpenAI s európskou rezidenciou dát – aplikácia dnes podporuje
   len priame API `anthropic` a `openai`, iný adaptér je požiadavka na vývoj. Overte aktuálne podmienky.
2. **Kľúč:** vytvor samostatný API kľúč len pre agenta, nastav **limit výdavkov** v konzole poskytovateľa.
   ```ini
   VLAKY_LLM_PROVIDER=anthropic
   VLAKY_LLM_MODEL=<názov modelu z dokumentácie poskytovateľa>
   VLAKY_ANTHROPIC_API_KEY=<kľúč>
   ```
   Názov modelu je povinný – aplikácia nemá predvolený model. Bez neho (alebo bez kľúča) agent LLM nepoužije
   a stránka Nastavenia to ukáže. Potom `docker compose up -d`.
3. Kľúč meň aspoň raz ročne a pri odchode ľudí, ktorí ho poznali.
4. Server potrebuje odchádzajúci HTTPS prístup k API poskytovateľa.

## 9. Železničná sieť z OpenStreetMap

Sieť SR (`data/siet/*.gz`, `stanice.json`) je súčasťou repozitára a zapečie sa do obrazu.
Dáta © prispievatelia OpenStreetMap, licencia **ODbL** – v aplikácii musí byť uvedený zdroj
a odvodená databáza zostáva pod ODbL. Obnova siete priamo na serveri (napr. raz za štvrťrok):

```bash
sudo -iu vlaky
cd /opt/vlaky/app
curl -fL -o /opt/vlaky/import/slovakia.osm.pbf \
     https://download.openstreetmap.fr/extracts/europe/slovakia.osm.pbf
docker compose --profile tools build osm-import       # obraz s balíkom osmium (requirements-dev.txt)
docker compose --profile tools run --rm --user "$(id -u):$(id -g)" osm-import
git diff --stat data/siet                             # čo sa zmenilo
docker compose build && docker compose up -d          # nová sieť sa zapečie do obrazu; init-db obnoví priechody
```

Import zapíše nové súbory do `data/siet` v repozitári – ten je potom „zmenený“ a `update.sh`
odmietne aktualizáciu, kým zmeny nezapíšeš do vlastnej vetvy (`git commit`) alebo ich nezahodíš
(`git checkout -- data/siet`). Odporúčaný postup je robiť import vo vývoji, skontrolovať zmeny
(trasy a dĺžky relácií, PROMPT.md F0) a dostať ich na server bežnou aktualizáciou.
Bez Dockera: `pip install -r requirements-dev.txt` vo venv a
`python -m vlaky import-osm --pbf /opt/vlaky/import/slovakia.osm.pbf`.

## 10. Aktualizácie

### 10.1 Aplikácia

```bash
sudo -iu vlaky
cd /opt/vlaky/app
deploy/scripts/update.sh                 # najnovšia verzia vetvy
deploy/scripts/update.sh --ref v1.4.0    # konkrétna verzia (tag, vetva alebo commit)
```

Skript: skontroluje, že v repozitári nie sú lokálne zmeny → ukáže zoznam zmien a pýta sa →
**záloha databázy** (`…-pred-aktualizaciou.sql.gz`) → označí aktuálny obraz pre návrat →
stiahne kód (`git pull --ff-only`) → `docker compose build --pull` (aj bezpečnostné záplaty
základného obrazu) a stiahne nové obrazy `db` a `caddy` → `init-db` s novým kódom (pri chybe skončí,
bežiaca verzia sa nezmení) → `docker compose up -d` → počká, kým sú `web` a `agent` zdravé, a overí HTTPS.

Aktualizuj mimo špičky, dispečing vopred upozorni (výpadok webu je zvyčajne pod minútu).
Aj bez zmien v kóde spusti `update.sh` raz mesačne – zostaví obraz s aktuálnymi bezpečnostnými záplatami.

> **Migrácie databázy:** `init-db` (spúšťa sa aj pri štarte webu a agenta) vytvorí chýbajúce tabuľky
> a do existujúcich tabuliek doplní nové stĺpce (s predvolenou hodnotou alebo prázdne). Zmeny typov,
> premenovanie a mazanie stĺpcov vyžadujú migrácie (napr. Alembic) – kým ich aplikácia nemá, nová verzia
> s takou zmenou musí mať vlastný postup v poznámkach k vydaniu.
>
> **Nové stĺpce:** táto verzia pridáva stĺpce `trains.standing_since_utc` (začiatok státia na koľajach ŽSR)
> a `trains.departed_utc` (skutočný čas odchodu). Oba sú prázdne (nullable) a `init-db` ich doplní do existujúcej
> databázy SQLite aj PostgreSQL bez migrácie. Pre staršie záznamy agent používa náhradné kotvy limitu 18 h
> (príchod na hranicu, čas pripravenosti odstaveného alebo prevzatého vlaku), kým sa začiatok státia nezapíše
> pri najbližšej úprave alebo zmene stavu.

### 10.2 Návrat na predchádzajúcu verziu (rollback)

```bash
deploy/scripts/update.sh --rollback      # predchádzajúci obraz + predchádzajúci commit
deploy/backup/restore.sh --db /opt/vlaky/backups/db/vlaky-db-…-pred-aktualizaciou.sql.gz   # ak sa menila DB
deploy/scripts/update.sh --ref main      # neskoršia aktualizácia (po rollbacku je repozitár na pevnom commite)
```

### 10.3 Operačný systém a Docker

- Bezpečnostné aktualizácie sa inštalujú automaticky (unattended-upgrades). Raz mesačne:
  `sudo apt update && sudo apt full-upgrade` (aktualizuje aj Docker – kontajnery sa krátko reštartujú).
- Ak `check.sh` hlási „čaká sa na reštart“, naplánuj `sudo reboot` mimo špičky (služby sa spustia samé).
  Automatický reštart v noci zapne `setup.sh --auto-reboot 03:40`.

### 10.4 PostgreSQL

- Opravné verzie 16.x stiahne `update.sh` (`docker compose pull db`).
- Prechod na novú hlavnú verziu (napr. 17) **nie je automatický**: záloha (`backup.sh`) → zastaviť
  aplikáciu → nový zväzok s novou verziou (zmena `image:` v `docker-compose.yml`) → `restore.sh --db` →
  test. Pôvodný zväzok `vlaky_pgdata` nemaž, kým nie je overené, že všetko funguje.
- **Zmena hesla databázy:** `POSTGRES_PASSWORD` sa použije len pri prvom vytvorení databázy. Postup:
  `docker compose exec db psql -U vlaky -d vlaky` a v ňom `\password vlaky` (nové heslo len písmená
  a číslice), potom nové heslo do `.env` a `docker compose up -d`.

## 11. Zálohy a test obnovy

Podrobný návod: [deploy/backup/README.md](../deploy/backup/README.md). V skratke:

- **Denne o 02:30** (`vlaky-backup.timer`): databáza (`pg_dump` + gzip) a súbory schránky a odoslaných
  správ do `/opt/vlaky/backups`, 14 dní, a **kópia mimo servera** (restic alebo rclone).
- Cieľ PROMPT.md (strata dát najviac 1 h) – prepni časovač na hodinové zálohy (README).
- Ostrá obnova: `deploy/backup/restore.sh --db SÚBOR [--files SÚBOR]` – do **novej prázdnej**
  databázy, s výslovným potvrdením a automatickou poistnou zálohou.
- **Mesačný test obnovy:** `deploy/backup/restore.sh --test` (produkciu nemení; výsledok
  v `/opt/vlaky/backups/obnova-test.log`), štvrťročne zo zálohy mimo servera, ročne nácvik úplnej obnovy.

## 12. Monitoring

Podrobný návod: [deploy/monitoring/README.md](../deploy/monitoring/README.md). V skratke:

| Čo | Ako | Upozornenie |
|---|---|---|
| Agent žije | `VLAKY_HEARTBEAT_URL` – agent pinguje každých 5 min | ping chýba > 15 min |
| Web a databáza | externá kontrola `https://agent.dopravca.example/health` | 2 zlyhania po sebe |
| Server | `check.sh` každých 15 min (disk, vek zálohy, kontajnery, HTTPS, certifikát) → `CHECK_HEARTBEAT_URL` | `/fail` s dôvodom |
| Záloha | `backup.sh` → `BACKUP_HEARTBEAT_URL` | `/fail` alebo chýba > 26 h |
| Doručovanie upozornení | kým ostáva niečo nedoručené, agent v každom cykle hlási chybu „doručenie: N správ(a) sa nepodarilo doručiť (kanály)“ (heartbeat `/fail`, bez adries príjemcov); `python -m vlaky health` vráti chybu (kontajner agenta je „unhealthy“, `update.sh` a `check.sh` to ohlásia) s detailom `dorucenie` – rovnako stránka `/zdravie` a denná správa | `/fail` alebo kontajner agenta „unhealthy“ |
| Chybné odporúčanie | odporúčanie, ktorého správu sa nedá pripraviť (chyba programu alebo neočakávané údaje), hlási každý cyklus ako „odporúčanie #N: …“ (heartbeat `/fail`, detail so stopou v `/zdravie`, komponent planovanie); ostatné odporúčania sa doručujú normálne | `/fail` |

Externý monitoring musí bežať **mimo tohto servera** a upozorňovať aspoň dvoch ľudí.

## 13. Postup pri incidentoch

Pri každom incidente: (1) informuj dispečing, či má pracovať ručne, (2) zachovaj stopy
(`docker compose logs --tail 500 agent > /opt/vlaky/incident-$(date +%F).log`), (3) oprav,
(4) informuj o skončení, (5) zapíš do prevádzkového denníka príčinu a opatrenie.

### Agent nebeží (upozornenie heartbeat)

1. **Dispečing:** pracuje ako doteraz podľa e-mailov vo svojej schránke – nič sa nestratí,
   agent po spustení schránku dočíta.
2. `docker compose ps` – stav kontajnera `agent`.
3. `docker compose logs --tail 200 agent` a `docker compose exec agent python -m vlaky health`
   (DB, posledný cyklus, posledná kontrola schránky).
4. `docker compose up -d agent` (prípadne `docker compose restart agent`).
5. Ak padá stále: posledná zmena? → `deploy/scripts/update.sh --rollback`. Plný disk? → nižšie.
6. Ak sa nepodarí do 30 min, eskaluj (kontakty v denníku).

### Webová aplikácia nedostupná

`docker compose ps`, `docker compose logs --tail 200 web caddy`, `curl -I https://agent.dopravca.example/health`.
Caddy pri nedostupnom webe zobrazí dispečerom správu „webová aplikácia je dočasne nedostupná“.
`/health` s kódom 503 znamená nedostupnú databázu → `docker compose logs --tail 200 db`.

### Vypršal alebo zlyhal prístup do schránky

Príznaky: v stave agenta chyba schránky, `health` hlási starú kontrolu schránky, v logu
`AADSTS7000222` (vypršané tajomstvo Graph), `invalid_client`, `AUTHENTICATIONFAILED` (IMAP).

- **Graph:** v Entra ID → App registrations → Certificates & secrets vytvor nové tajomstvo,
  vlož do `VLAKY_GRAPH_CLIENT_SECRET`, `docker compose up -d`, staré tajomstvo zmaž.
  Skontroluj aj politiku prístupu (`Test-ApplicationAccessPolicy`).
- **IMAP / Google:** nové heslo aplikácie → `VLAKY_IMAP_PASSWORD` → `docker compose up -d`.
- Kým to nefunguje, agent nevidí nové zmeny – **dispečing musí vedieť, že pracuje ručne**.
  Po oprave agent dočíta zmeškané e-maily.

### Upozornenia sa nedoručujú (zdravie: `dorucenie`)

1. **Dispečing:** odporúčania sú stále na webe (Odporúčania, nástenka) – nech ich dispečeri sledujú tam.
2. `docker compose exec agent python -m vlaky health` – detail `dorucenie` ukáže kanál a poslednú chybu
   („Chyba kanála: …“ = server alebo spojenie, inak odmietnutie konkrétnej správy alebo príjemcu).
3. **E-mail:** dostupnosť SMTP servera alebo relé, heslo alebo konektor (kapitola 6.1), pri Microsoft Graph
   platnosť tajomstva aplikácie; **Teams:** či webhook (Workflow) ešte existuje; **SMS:** stav brány a token.
4. **Odmietnutý príjemca** (napr. 550 user unknown, SMS brána odmieta číslo): oprav adresu vo
   `VLAKY_NOTIFY_EMAILS` alebo číslo vo `VLAKY_NOTIFY_PHONES` a reštartuj agenta (`docker compose up -d`).
5. Po oprave servera agent nedoručené správy pošle sám (KRITICKÉ do 5 min, ostatné najneskôr do 60 min, hneď
   po prvej úspešnej správe tomu istému príjemcovi). Odosielanie over:
   `docker compose exec agent python -m vlaky test-notify`.

### Neobvyklé záznamy auditu pošty

- `email_message_id_kolizia` – e-mail s rovnakým Message-ID od iného odosielateľa sa uložil samostatne
  (Message-ID s dodatkom „#…“, kapitola 5.5); over pravosť oboch e-mailov.
- `vyluka_navrh_zlyhal` – záznam „navrh“ pre plánovač sa nepodarilo zapísať (napr. chyba databázy); udalosť
  ostáva vo fronte s upozornením a plánovač s ňou do potvrdenia nepočíta.

### LLM nedostupné

Extrakcia pokračuje **len s pravidlami** (agent to uvedie pri udalosti); viac udalostí ide na potvrdenie
dispečerovi. Over stav služby poskytovateľa, platnosť kľúča a limit výdavkov. Pri dlhšom výpadku
môžeš dočasne nastaviť `VLAKY_LLM_PROVIDER=none` (`docker compose up -d`) – zmizne čakanie na časový limit.

### Plný disk

`deploy/monitoring/check.sh`, `df -h`, `docker system df`, `du -sh /opt/vlaky/backups`, `sudo journalctl --disk-usage`.

- staré obrazy: `docker image prune -a --filter until=168h`
- zálohy: zníž `BACKUP_RETENTION_DAYS` v `/etc/vlaky/ops.env` (zálohy mimo servera zostanú)
- logy: `sudo journalctl --vacuum-time=14d`
- veľkosť databázy: `docker compose exec db psql -U vlaky -d vlaky -c "SELECT pg_size_pretty(pg_database_size('vlaky'))"`;
  trvalé riešenie je väčší disk u poskytovateľa VPS
- pri plnom disku môže zlyhať aj databáza – po uvoľnení miesta `docker compose up -d` a `check.sh`

### Problém s certifikátom

`docker compose logs caddy | grep -i -E "acme|certificate|error"`. Najčastejšie príčiny:
DNS ukazuje inam (`dig +short agent.dopravca.example`), zlý alebo nefunkčný AAAA záznam, porty 80/443
zablokované u poskytovateľa VPS, chybný `VLAKY_DOMAIN`, CAA záznam nepovoľuje vydavateľa,
nesprávny čas servera (`timedatectl`), limity Let's Encrypt po mnohých neúspešných pokusoch
(počkaj hodinu; Caddy pokusy sám opakuje). Po oprave `docker compose restart caddy`.

### Server je preťažený alebo nereaguje

`docker stats --no-stream`, `uptime`, `free -h`. Kontajnery majú limity pamäte a CPU
(`docker-compose.yml`), takže jeden nezahltí ostatné. Ak server nereaguje vôbec, reštart cez
konzolu poskytovateľa – služby sa spustia samé.

### Podozrenie na kompromitáciu

Odpoj server od siete cez konzolu poskytovateľa (nemaž ho – stopy), informuj zodpovednú osobu (GDPR),
vymeň **všetky** tajomstvá (`.env`: DB heslo, `VLAKY_SECRET_KEY`, Graph/IMAP, SMTP, LLM, SMS, Teams,
`VLAKY_POSITIONS_TOKEN`; SSH kľúče; heslo k zálohám), nový server postav podľa kapitoly 4 a obnov zo zálohy.
Porušenie ochrany osobných údajov sa hlási Úradu na ochranu osobných údajov SR do 72 hodín.

## 14. GDPR a bezpečnosť

- **Posúdenie vplyvu (DPIA)** – pred ostrou prevádzkou je povinné: systém spracúva polohu rušňov
  a pracovný čas rušňovodičov (sledovanie zamestnancov). Zapoj zodpovednú osobu (DPO); zamestnancov
  a ich zástupcov treba vopred informovať a rozsah kontroly s nimi prerokovať (Zákonník práce, § 13 ods. 4 –
  over s právnikom). Spracovanie zapíš do záznamov o spracovateľských činnostiach.
- **Zmluvy (DPA):** poskytovateľ VPS, poštová služba, poskytovateľ LLM, SMS brána, úložisko záloh,
  externý monitoring (ak by dostával osobné údaje – heartbeat ich neobsahuje: len typ a prvý riadok chyby,
  bez adries príjemcov a parametrov databázových príkazov).
- **Doby uchovávania** (PROMPT.md 2.6 – potvrdiť s DPO): e-maily 1 rok, správy agenta dispečerom (záznamy
  aj kópie v adresári outbox, vrátane zopakovaných pokusov pri výpadku servera správ) rovnako ako obsah e-mailov
  (`VLAKY_RETENTION_EMAIL_DAYS`, predvolene 1 rok, najmenej 3 dni), surové GPS dáta 90 dní (zabezpečuje
  aplikácia); prístupový log Caddy 30 dní (obsahuje IP adresy); systémové logy 1 mesiac;
  logy kontajnerov najviac 5 × 10 MB na službu; lokálne zálohy 14 dní, zálohy mimo servera podľa
  `RESTIC_FORGET_ARGS` (predvolene najviac 6 mesiacov) alebo `OFFSITE_RETENTION_DAYS`.
  **Zmazané údaje zostávajú v zálohách až do ich vypršania** – uveď to v informácii pre dotknuté osoby.
- **Prístup:** do aplikácie len osobné účty s rolami; na server len správcovia cez SSH kľúč a VPN/povolenú IP;
  skupina `docker` = práva root-a. Odchod zamestnanca → zrušiť účet v aplikácii, jeho SSH kľúč, vymeniť
  tajomstvá, ktoré poznal.
- **Ochrana prihlásenia:** najviac 3 súbežné overenia hesla (bcrypt); na voľné miesto sa čaká najviac 2 s, potom
  odpoveď 503 s `Retry-After: 5`. Limit neúspešných pokusov podľa IP adresy (IPv6 podľa siete /64, adresy IPv4
  v tvare IPv6 ako IPv4) a podľa účtu. Zablokovaný pokus sa do auditu zapíše najviac raz za 15 min pre kľúč, ktorý
  blokuje (`login_blocked`, `blokovane` = `ip` alebo `ucet`); pokusy s prázdnym menom alebo heslom sa do auditu
  nezapisujú. Audit sa nečistí – detail vlaku z neho číta 30 posledných záznamov vlaku a doplnenie (hlásenia
  o probléme, odmietnutia z odkazu rušňovodiča) len za posledných 7 dní.
- **Šifrovanie:** HTTPS (Caddy), zálohy mimo servera šifrované (restic / rclone crypt), disk VPS
  šifrovaný, ak to poskytovateľ umožňuje.
- **Žiadosti dotknutých osôb** (prístup, výmaz) rieši vedúci dispečingu s DPO; údaje sú v databáze
  (export cez aplikáciu, prípadne `docker compose exec db psql …`).
- **Minimalizácia:** do LLM ide len text potrebný na extrakciu; žiadne hodnotenie výkonu jednotlivcov
  bez dohody so zástupcami zamestnancov.

## 15. Náklady (orientačne)

Len rádové odhady – ceny sa menia, **overte aktuálne cenníky** (stav 2026).

| Položka | Rádovo |
|---|---|
| VPS 4 vCPU / 8 – 16 GB / 160 GB v EÚ | 15 – 60 € mesačne (+ snímky/zálohy poskytovateľa cca 10 – 20 %) |
| Úložisko na zálohy mimo servera (desiatky GB) | 1 – 10 € mesačne |
| Doména | 10 – 30 € ročne (subdoména firemnej domény zadarmo) |
| Schránka agenta | M365 zdieľaná schránka 0 €; licencovaná schránka / Google Workspace 5 – 15 € mesačne |
| Odosielanie e-mailov (relé služba, ak treba) | 0 – 20 € mesačne |
| LLM API | desiatky až nižšie stovky € mesačne podľa počtu e-mailov a príloh (napr. 50 e-mailov denne × ~10 000 vstupných a ~2 000 výstupných tokenov pri cene 4 / 20 USD za milión tokenov ≈ 120 USD mesačne) |
| SMS brána | rádovo centy za SMS (len kritické upozornenia) |
| Externý monitoring | 0 € (bezplatné úrovne) – 30 € mesačne |
| Práca správcu | najväčšia položka: aktualizácie, test obnovy, incidenty – rádovo hodiny mesačne |

## 16. Inštalácia bez Dockera (systemd)

Pre prípad, že Docker nemožno použiť. Rovnaký server a `setup.sh` (Docker sa nainštaluje, ale nemusí sa používať).

```bash
sudo apt-get install -y python3-venv python3-dev postgresql postgresql-client caddy git
# (Ubuntu 24.04 má Python 3.12 a PostgreSQL 16; novšiu verziu Caddy ponúka oficiálny repozitár Caddy)
sudo -u postgres createuser --pwprompt vlaky              # heslo len písmená a číslice
sudo -u postgres createdb -O vlaky vlaky
sudo -u vlaky git clone git@github.com:ORGANIZACIA/VLAKY.git /opt/vlaky/app
sudo -u vlaky python3 -m venv /opt/vlaky/venv
sudo -u vlaky /opt/vlaky/venv/bin/pip install -r /opt/vlaky/app/requirements.txt
sudo install -m 0640 -o root -g vlaky /opt/vlaky/app/.env.example /etc/vlaky/vlaky.env
sudo nano /etc/vlaky/vlaky.env
```

V `/etc/vlaky/vlaky.env` navyše nastav:

```ini
VLAKY_DATABASE_URL=postgresql+psycopg://vlaky:HESLO@127.0.0.1:5432/vlaky
VLAKY_PUBLIC_URL=https://agent.dopravca.example
VLAKY_SECRET_KEY=<openssl rand -hex 32>
VLAKY_MAIL_DIR=/var/lib/vlaky/schranka
VLAKY_OUTBOX_DIR=/var/lib/vlaky/odoslane
```

```bash
cd /opt/vlaky/app
sudo cp deploy/systemd/vlaky-*.service deploy/systemd/vlaky-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
# jednorazové príkazy (init-db, create-user, test-notify) s rovnakou konfiguráciou ako služby
# (funkciu si môžeš pridať do ~/.bashrc správcu):
vlaky-cli() {
  sudo systemd-run --quiet --wait --pipe --uid=vlaky --gid=vlaky \
    --property=EnvironmentFile=/etc/vlaky/vlaky.env --working-directory=/opt/vlaky/app \
    /opt/vlaky/venv/bin/python -m vlaky "$@"
}
vlaky-cli init-db
sudo systemctl enable --now vlaky-web vlaky-agent vlaky-backup.timer vlaky-check.timer
```

Služby `vlaky-web` a `vlaky-agent` bežia ako používateľ `vlaky` s `Restart=always`, sprísnením
(`NoNewPrivileges`, `ProtectSystem=strict`, zápis len do `/var/lib/vlaky`, `PrivateTmp`) a limitmi
pamäte. Web počúva len na `127.0.0.1:8000`.

**Caddy:** skopíruj `deploy/Caddyfile` do `/etc/caddy/Caddyfile` a nastav premenné
(`sudo systemctl edit caddy`):

```ini
[Service]
Environment=VLAKY_DOMAIN=agent.dopravca.example
Environment=VLAKY_UPSTREAM=127.0.0.1:8000
```

potom `sudo systemctl restart caddy`. **Zálohy a kontrola:** v `/etc/vlaky/ops.env` nastav
`DEPLOY_MODE=systemd`, `BACKUP_DATABASE_URL` (rovnaké ako `VLAKY_DATABASE_URL`) a
`BACKUP_FILES_ROOT=/var/lib/vlaky`; ostrú obnovu spúšťaj cez `sudo`.
Ďalšie príkazy cez funkciu: `vlaky-cli create-user …`, `vlaky-cli test-notify`, `vlaky-cli health`.
Aktualizácia: záloha, `git pull`, `pip install -r requirements.txt`, `vlaky-cli init-db`,
`sudo systemctl restart vlaky-web vlaky-agent` (skript `update.sh` je len pre Docker).

## 17. VPN cez WireGuard (voliteľné)

SSH je potom dostupné len z VPN – nezáleží na meniacej sa IP správcu.

Na serveri:

```bash
sudo apt-get install -y wireguard
wg genkey | sudo tee /etc/wireguard/server.key | wg pubkey | sudo tee /etc/wireguard/server.pub
sudo chmod 600 /etc/wireguard/server.key
sudo nano /etc/wireguard/wg0.conf
```

```ini
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820
PrivateKey = <obsah /etc/wireguard/server.key>

[Peer]
# notebook správcu
PublicKey = <verejný kľúč správcu>
AllowedIPs = 10.8.0.2/32
```

```bash
sudo systemctl enable --now wg-quick@wg0
# dočasne nechaj aj svoju verejnú IP, kým VPN neoveríš:
sudo bash /opt/vlaky/app/deploy/vps/setup.sh --ssh-allow 10.8.0.0/24 --ssh-allow 203.0.113.10/32 --wireguard-port 51820
```

U správcu (aplikácia WireGuard, nový tunel):

```ini
[Interface]
Address = 10.8.0.2/32
PrivateKey = <súkromný kľúč správcu>

[Peer]
PublicKey = <obsah /etc/wireguard/server.pub>
Endpoint = 203.0.113.10:51820
AllowedIPs = 10.8.0.1/32
PersistentKeepalive = 25
```

Over `ssh spravca@10.8.0.1`. Potom povoľ SSH len z VPN – staré pravidlo zruš a skript spusti znova:
`sudo ufw delete allow proto tcp from 203.0.113.10/32 to any port 22` a
`sudo bash /opt/vlaky/app/deploy/vps/setup.sh --ssh-allow 10.8.0.0/24 --wireguard-port 51820`.
Súkromné kľúče nikdy neposielaj e-mailom; každý správca má vlastný kľúč (vlastný `[Peer]`).

## 18. Prehľad príkazov a súborov

| Úloha | Príkaz (ako `vlaky` v `/opt/vlaky/app`) |
|---|---|
| Stav služieb | `docker compose ps` |
| Logy | `docker compose logs -f agent` / `web` / `caddy` / `db` |
| Stav agenta | `docker compose exec agent python -m vlaky health` |
| Reštart po zmene `.env` | `docker compose up -d` |
| Reštart jednej služby | `docker compose restart agent` |
| Zastavenie / spustenie všetkého | `docker compose down` / `docker compose up -d` (dáta zostávajú vo zväzkoch; **nikdy** `down -v`) |
| Nový používateľ | `docker compose exec web python -m vlaky create-user MENO --role dispecer --password "$PW"` |
| Skúšobná správa | `docker compose exec agent python -m vlaky test-notify` |
| Aktualizácia / návrat | `deploy/scripts/update.sh` / `deploy/scripts/update.sh --rollback` |
| Záloha / zoznam / test obnovy | `deploy/backup/backup.sh` / `deploy/backup/restore.sh --list` / `--test` |
| Kontrola servera | `deploy/monitoring/check.sh` |
| Časovače | `systemctl list-timers 'vlaky-*'` |
| Databáza (psql) | `docker compose exec db psql -U vlaky -d vlaky` |

| Súbor / adresár | Obsah |
|---|---|
| `/opt/vlaky/app` | repozitár: `docker-compose.yml`, `.env` (tajomstvá, práva 600), `deploy/` |
| `/opt/vlaky/backups` | lokálne zálohy (`db/`, `files/`, `obnova-test.log`) |
| `/opt/vlaky/import` | stiahnuté extrakty OpenStreetMap |
| `/etc/vlaky/ops.env` | nastavenia záloh a kontroly servera (vzor `deploy/ops.env.example`) |
| `/etc/vlaky/vlaky.env` | konfigurácia aplikácie pri inštalácii bez Dockera |
| `deploy/Caddyfile` | reverse proxy, HTTPS, hlavičky |
| `deploy/vps/setup.sh`, `deploy/vps/harden-ssh.sh` | príprava a zabezpečenie servera |
| `deploy/backup/` | `backup.sh`, `restore.sh`, návod |
| `deploy/monitoring/` | `check.sh`, návod |
| `deploy/systemd/` | jednotky systemd (zálohy, kontrola; web a agent bez Dockera) |
| `deploy/scripts/update.sh` | aktualizácia a návrat |
