# OMEN Hub

Un'unica app per l'HP OMEN MAX 16 (board 8D41) su Arch + Plasma: ventole,
profili CPU/GPU, luci e tutti i servizi/fix installati nei mesi scorsi.
Non sostituisce i progetti esistenti: li comanda.

| Pagina | Cosa fa | Attraverso |
|---|---|---|
| Panoramica | temperature (quella di controllo delle ventole, non solo il package), giri, carico P/E, batteria, GPU; cambio rapido profilo | sysfs, omen-hubd |
| Prestazioni | libreria profili (governor, EPP, turbo, platform profile, frequenze, ventole) e assegnazione rete/batteria; powertop | `/etc/auto-cpufreq.conf` ([charger]/[battery]) |
| Ventole | modalità, libreria curve + editor, sorgente temperatura (hybrid), pulizia a rotazione inversa e pianificazione | `/etc/omen-fan-control/config.json` (il demone lo rilegge da solo) |
| Luci e schermo | luminosità reale e tastiera, livelli per alimentazione, Alt+tasti luminosità per la tastiera (omen-fx-brightnessd), attenuazione per rete/batteria, azioni rapide omen-fx | powerdevil (D-Bus, powerdevilrc), socket omen-fxd |
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

## Gruppo OMEN (omen-hub.target)

I servizi restano separati (un guasto non si porta dietro gli altri, e quelli di
root non possono vivere nella sessione), ma si comandano come un blocco:

| Target | Membri |
|---|---|
| `omen-hub.target` (sistema) | omen-fan-control, nvidia-powerd, auto-cpufreq, powertop, omen-backlight-sync, omen-fxd, omen-f1 |
| `omen-hub-user.target` (sessione) | omen-fx-notifyd, omen-fx-brightnessd, omen-mute-monitor |

Ogni membro ha un drop-in `/etc/systemd/{system,user}/<unità>.d/50-omen-hub.conf`
con `PartOf=` il target: fermare o riavviare il target ferma o riavvia tutti.

```
systemctl restart omen-hub.target && systemctl --user restart omen-hub-user.target
```

I target non hanno `Wants=`: un servizio disabilitato resta spento. «Avvia tutto»
(Servizi → Gruppo OMEN, oppure `omen-hubd` comando `group`) avvia il target e i
soli membri abilitati. Fuori dal gruppo: omen-hubd (serve a riavviarlo), gli
hook di sospensione e gli extra del desktop.

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
