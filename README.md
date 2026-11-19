# OMEN Hub

Un'unica app per l'HP OMEN MAX 16 (board 8D41) su Arch + Plasma: ventole,
profili CPU/GPU, luci e tutti i servizi/fix installati nei mesi scorsi.
Non sostituisce i progetti esistenti: li comanda.

| Pagina | Cosa fa | Attraverso |
|---|---|---|
| Panoramica | temperature (quella di controllo delle ventole, non solo il package), giri, carico P/E, batteria, GPU; cambio rapido profilo | sysfs, omen-hubd |
| Prestazioni | libreria profili (governor, EPP, turbo, platform profile, frequenze, ventole) e assegnazione rete/batteria; powertop | `/etc/auto-cpufreq.conf` ([charger]/[battery]) |
| Ventole | modalità, libreria curve + editor, sorgente temperatura (hybrid), pulizia a rotazione inversa e pianificazione | `/etc/omen-fan-control/config.json` (il demone lo rilegge da solo) |
| Luci e schermo | luminosità reale e tastiera, luminosità adattiva (webcam come sensore di luce), livelli per alimentazione, Alt+tasti luminosità per la tastiera (omen-fx-brightnessd), attenuazione per rete/batteria, azioni rapide omen-fx | powerdevil (D-Bus, powerdevilrc), `omen-hub-luce.service`, socket omen-fxd |
| ↳ Studio luci | l'editor completo di omen-fx dentro l'hub (pulsante «Studio luci» in Luci e schermo): temi, layout a zone disegnabili, aspetto predefinito di tastiera e barra, effetti degli avvisi; anteprima animata e «dal vivo sull'hardware» | motore di omen-fx importato da `/usr/local/lib/omen-fx` (`omen_hub/fxengine.py`), socket omen-fxd |
| Servizi | stato, avvio/arresto, abilitazione, installazione/rimozione, log; controlli di integrità | systemd, installer dei progetti |
| Test | prove guidate (animazioni che dicono quando staccare caricatore/HDMI e quando non toccare): consumi a riposo, GPU (D3cold, chi la tiene accesa, gpu_burn), ventole, CPU sotto carico, schermo spento, standby; report Markdown + JSON in `~/omen-hub-reports/` (anche `ultimo-report.md`) da far leggere a un'AI | `omen_hub/testsuite.py` (test come generatori di passi), `testkit.py` (misure, report), omen-hubd `test.rapl` / `test.rtcwake` / `test.gpu_holders` |

## Installazione

```
sudo ~/omen-hub/install.sh      # una volta, da un terminale vero
omen-hub                        # oppure dal menu: OMEN Hub
```

Senza `omen-hubd` l'app parte lo stesso (`~/omen-hub/omen-hub`) in sola
lettura. L'installazione non cambia nessuna impostazione.

Disinstallazione: `sudo ~/omen-hub/uninstall.sh` (tiene profili e backup).

Icona: se in `share/icons/` ci sono i PNG `omen-hub-<dimensione>.png` (fuori da
git) l'installer usa quelli, altrimenti `share/omen-hub.svg`.

## Architettura

- **GUI** (PyQt6, utente): legge sysfs/procfs; luminosità, unit utente,
  journal e omen-fx li gestisce da sola.
- **omen-hubd** (root, `/usr/local/lib/omen-hub`, copia di proprietà di root):
  socket `/run/omen-hub/hub.sock` 0660 root:wheel, controllo SO_PEERCRED,
  solo comandi in whitelist (unit del registro, hook del registro, profili,
  config ventole validata, pulizia, powertop). Nessuna shell, nessun percorso
  arbitrario. Ogni file riscritto finisce prima in `/var/lib/omen-hub/backup/`.
- Gli installer dei progetti (fan control, omen-fx, omen-f1, brightness) vengono
  eseguiti come root dalla loro cartella in `~`: è lo stesso confine di fiducia
  di `omen-fan-control.service`, che già esegue `~/omen-fan-control` come root.

## Luminosità adattiva (omen-hub-luce.service)

Il portatile non ha un sensore di luce ambientale (niente ISH, ACPI0008 o IIO:
verificato il 19/11/2026), quindi la luce della stanza la misura la webcam RGB
con uno scatto ogni 30 s. Servizio utente `omen-hub-luce.service`
(`omen-hub --adaptive`, `omen_hub/adaptived.py`), installato da install.sh ma
acceso solo dall'interruttore in Luci e schermo.

- **Misura** (`omen_hub/camlight.py`): ioctl V4L2 diretti, niente OpenCV. Un
  fotogramma YUYV con esposizione manuale; se è nero o saturo l'esposizione si
  sposta (da 4,8 ms al massimo impostato, 250 ms di default) e si rifà. La
  luminanza media (nero a 16) diventa una stima di luce con la curva di
  risposta misurata su questa camera (tabella `RESPONSE`: la stessa scena dà la
  stessa stima entro ±3 % da 32 a 400 ms di posa, ed entro ±4 % già con un
  segnale di 1,3 sopra il nero). La scala 0–100 % è logaritmica tra i due
  estremi della calibrazione: buio e torcia del telefono.
- **Solo le fasce laterali** dell'inquadratura (un quarto per lato): la metà
  centrale sei tu, la cosa che schermo e tastiera illuminano di più. Misurato
  il 19/11 con quattro serie di foto (stanza buia / lampada × PC all'1 % / al
  100 %): con la lampada l'errore dovuto allo schermo scende da +34 % a +6 %,
  al buio la luce finta da 30 % a 15 % della scala. Pesare le zone scure invece
  non va: con la lampada alle spalle tu sei tra le zone più scure. Le
  calibrazioni sono marcate con la zona (`CAL_ZONE`): quelle fatte
  sull'immagine intera non valgono più.
- **Posa più corta possibile**: ogni scatto sceglie la posa del successivo
  per un segnale di 2 sopra il nero (il doppio del minimo usabile), mai sotto
  un periodo di fotogramma (33 ms: sotto non si risparmia). Conta perché il
  primo fotogramma arriva dopo ~0,21 s + 3 pose e lo stop aspetta il
  fotogramma in corso: camera accesa 0,33 s a 33 ms, 0,52 s a 100 ms, 0,97 s a
  250 ms. Il guadagno del sensore non si usa (alza anche il nero).
- **LED il meno possibile al buio**: lì serve una posa lunga per distinguere
  penombra e buio, e il LED resta acceso ~3 pose. Lo scatto lungo (fino
  all'esposizione massima) si fa solo quando la stanza si scurisce e poi ogni
  10 minuti (`DARK_REFRESH_S`; "lungo" = oltre 66 ms, `LONG_FROM`); in mezzo
  uno scatto da un fotogramma (33 ms,
  camera accesa ~0,32 s, il minimo: il sensore impiega 0,206 s ad avviarsi in
  qualunque formato e risoluzione) controlla solo che sia ancora buio, e una
  luce che si accende la vede subito. Formato: YUYV 640×360, il più piccolo
  non compresso.
- **Consumi** (misurati il 19/11 a batteria, blocchi alterni spento/acceso):
  ~5 J per scatto con la stanza buia (posa 200–250 ms), cioè **~0,15–0,17 W
  con uno scatto ogni 30 s** mentre usi il PC; zero quando sei via. Di questi
  ~0,8 J sono della CPU (RAPL), il resto camera e chipset finché il flusso è
  attivo. La coda di 2 s in cui la USB resta sveglia dopo lo scatto non costa
  nulla di misurabile (provato a 100 ms: stessa energia).
- **Non disturba nessuno**: la camera viene prenotata (S_FMT, REQBUFS) *prima*
  di toccarne i controlli, quindi con un'altra app in streaming lo scatto fallisce
  senza cambiare nulla; nessuno scatto se la camera trasmette (stato runtime PM
  USB, vale anche per i processi di root come howdy sul nodo IR), se un altro
  processo la tiene aperta, o se qualcuno l'ha aperta negli ultimi 10 s;
  inotify (IN_OPEN) su tutti i nodi del modulo interrompe all'istante uno scatto
  in corso. Alla fine la camera torna in esposizione automatica. Niente scatti
  a coperchio chiuso, a sessione bloccata o dopo 10 minuti senza input (tempo di
  inattività letto da omen-fxd: su Wayland una sessione non lo vede).
- **Luminosità** via powerdevil (`org.kde.ScreenBrightness`, contesto
  `omen-hub-luce`, senza OSD; tastiera con `setKeyboardBrightnessSilent`).
  Ogni cambio dura «Transizione» (2 s di default, una scrittura ogni 50 ms;
  la tastiera a passi di un punto). Un livello si muove solo se la luce della
  stanza si è spostata di «Variazione minima» (5 punti della scala 0–100) dalla
  misura che ha impostato il livello attuale, non dalla precedente: una deriva
  lenta si accumula e passa. L'attenuazione per inattività di powerdevil sullo
  schermo è un rapporto separato e non viene annullata; la tastiera si regola
  solo mentre scrivi.
- **Due curve per uscita**, una a batteria e una a rete (con «Solo a
  batteria» si usa solo la prima); il servizio segue l'alimentazione del
  momento. Le impostazioni di prima, con una curva sola, la copiano in
  entrambe.
- **Regolazioni a mano** (tasti, cursori, pannello): sospendono la regolazione
  automatica di quell'uscita (schermo e tastiera separati) per «Pausa dopo un
  cambio a mano» (3 h) o finché la luce della stanza non si sposta di 30 punti
  da quando l'hai cambiata; il livello che powerdevil applica attaccando o
  staccando il cavo non conta. Le pause sopravvivono a un riavvio del servizio
  (`$XDG_RUNTIME_DIR/omen-hub/luce-adattiva-pausa.json`).
- **Interruttore nel vassoio** («Luminosità e colore»): Plasma 6.8 compila quel
  pannello dentro powerdevil, quindi l'hub ne installa un gemello per utente
  (`plasma/org.mircogiorgi.brightness`: il QML di powerdevil 6.8.3 con le sue
  traduzioni + `AdaptiveItem.qml`) e lo sostituisce all'originale nei vassoi
  (Luci e schermo → Avanzate, o `omen-hub --vassoio installa|rimuovi`;
  plasmashell si riavvia, l'originale resta tra i `knownItems` per non tornare
  da solo). Spegnerlo = cambiare la luminosità a mano. Comandi:
  `omen-hub --luce stato|pausa|riprendi [schermo|tastiera]`. Dopo un
  aggiornamento di powerdevil la copia può restare indietro: l'hub lo segnala.
- File: impostazioni `~/.config/omen-hub/luce-adattiva.json` (scritte dalla
  GUI), stato `$XDG_RUNTIME_DIR/omen-hub/luce-adattiva.json` (scritto dal
  servizio). Segnali: HUP rilegge (e riapplica le curve all'ultima misura, senza
  foto), USR1 misura subito (scatto completo), USR2 esegue la richiesta di
  `omen-hub --luce` (pausa/riprendi).

```
systemctl --user kill -s USR1 omen-hub-luce.service   # misura adesso
journalctl --user -u omen-hub-luce -f                 # cosa decide e perché
```

## Gruppo OMEN (omen-hub.target)

I servizi restano separati (un guasto non si porta dietro gli altri, e quelli di
root non possono vivere nella sessione), ma si comandano come un blocco:

| Target | Membri |
|---|---|
| `omen-hub.target` (sistema) | omen-fan-control, nvidia-powerd, auto-cpufreq, powertop, omen-backlight-sync, omen-fxd, omen-f1 |
| `omen-hub-user.target` (sessione) | omen-fx-notifyd, omen-fx-brightnessd, omen-hub-luce, omen-mute-monitor |

Ogni membro ha un drop-in `/etc/systemd/{system,user}/<unità>.d/50-omen-hub.conf`
con `PartOf=` il target: fermare o riavviare il target ferma o riavvia tutti.

```
systemctl restart omen-hub.target && systemctl --user restart omen-hub-user.target
```

I target non hanno `Wants=`: un servizio disabilitato resta spento. «Avvia tutto»
(Servizi → Gruppo OMEN, oppure `omen-hubd` comando `group`) avvia il target e i
soli membri abilitati. Fuori dal gruppo: omen-hubd (serve a riavviarlo), gli
hook di sospensione e gli extra del desktop.

## Correzione GPU NVIDIA per il kernel 7.3 (RTD3)

Dal kernel 7.3 i kthread hanno una root vuota: il driver NVIDIA (615+) fa il
probe nel kthread `nv-probe`, non riesce ad aprire il config PCI in sysfs e
disattiva il runtime PM (`Runtime D3 status: Not supported`). La GPU non va
più in D3cold: 17,8 W a riposo invece di 11,6 (test del 18/11/2026).

| Pezzo | Cosa fa |
|---|---|
| `omen_hub/gpufix.py` | rileva lo stato (driver, sorgente DKMS, modulo compilato, riavvio pendente) e applica la patch CachyOS 0004 a `nv-pci.c` per ancore esatte |
| barra in cima all'app | compare solo se serve: «Applica correzione», avanzamento con «Dettagli», poi «Riavvia ora» |
| omen-hubd `gpufix` | `apply` in un thread (root): `pacman -S nvidia-open-dkms` se c'è il precompilato, patch con backup, `dkms build/install --force` per ogni kernel, `mkinitcpio -P`; `job` per l'avanzamento |
| `/etc/pacman.d/hooks/95-omen-hub-gpufix.hook` | dopo ogni aggiornamento di nvidia-open(-dkms) o linux: una riga nell'output di pacman e `/var/lib/omen-hub/gpufix-check.json` |
| `omen-hub-notify.path/.service` (sessione) | al login e a ogni cambio di quel file: notifica «Apri OMEN Hub», una sola volta per driver/kernel/stato |

Con nvidia-open-dkms la patch si perde a ogni aggiornamento del driver: l'hook
se ne accorge prima del riavvio («Correzione GPU persa con l'aggiornamento»).
Quando NVIDIA includerà la correzione (`scoped_with_init_fs` già nel
sorgente) l'hub non patcha più niente.

## Regole che l'app rispetta (imparate su questa macchina)

- Mai `nvidia-smi`/`lspci` in automatico: svegliano la GPU dal D3cold (+10 W).
  Lo stato viene da `power/runtime_status`; i sensori GPU solo su richiesta e
  solo se la GPU è già attiva.
- Gli hook rimossi vengono parcheggiati in `/var/lib/omen-hub/disabled-hooks`,
  mai lasciati in `system-sleep/` (systemd-sleep esegue anche i `.bak`).
- `auto-cpufreq.conf` è modificato riga per riga: commenti e chiavi non gestite
  restano intatti (il primo salvataggio lo riscrive identico al byte).
- Campionamento a 2 s, fermo quando la finestra è ridotta a icona o nascosta.
- powerdevil applica i livelli per alimentazione solo al cambio profilo: dopo il
  salvataggio viene riavviato.

## Test

```
python -m unittest discover -s tests
```

Il test del demone lo avvia come utente su un albero finto
(`OMEN_HUB_ROOT`, `OMEN_HUB_DRY_RUN=1`): systemctl non viene mai chiamato.

I test della luminosità adattiva che usano la camera vera (accendono il LED per
un paio di secondi) girano solo con `OMEN_HUB_CAMERA_TEST=1`. Il servizio si
prova dal repo senza installarlo, su file di prova:

```
OMEN_HUB_LUCE_CONFIG=/tmp/c.json OMEN_HUB_LUCE_STATUS=/tmp/s.json \
OMEN_HUB_LUCE_IGNORE_IDLE=1 python3 ~/omen-hub/omen-hub --adaptive
```
