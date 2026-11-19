/*
    OMEN Hub: the adaptive brightness switch in Luminosità e colore.

    Shown only while the omen-hub-luce service runs (switched on in OMEN Hub →
    Luci e schermo). Off = the same as setting the brightness by hand: the
    automatic control of screen and keyboard pauses until its timer runs out
    or the room light changes a lot; on = it resumes at once.
    State and commands go through `omen-hub --luce`.

    SPDX-License-Identifier: GPL-2.0-or-later
*/

import QtQuick
import QtQuick.Layouts
import QtQuick.Window

import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmaComponents3.ItemDelegate {
    id: root

    // poll the state while the popup is open (its window is shown)
    property bool attivo: Window.window ? Window.window.visible : false
    property var stato: null
    readonly property bool regolabile: stato !== null && stato.servizio === true && stato.regolabile === true
    readonly property bool accesa: regolabile && stato.accesa === true
    property string comando: "/usr/local/bin/omen-hub --luce"

    visible: regolabile
    background.visible: highlighted
    highlighted: activeFocus
    hoverEnabled: false

    Accessible.description: dettaglio.text
    Accessible.onPressAction: interruttore.click()
    Keys.onSpacePressed: interruttore.click()

    function aggiorna() {
        exec.connectSource(comando + " stato");
    }

    function invia(azione) {
        exec.connectSource(comando + " " + azione);
        presto.restart();
    }

    function descrizione() {
        if (!regolabile) {
            return "";
        }
        if (accesa) {
            return "attiva";
        }
        let fino = 0;
        for (const k of ["screen", "keyboard"]) {
            const u = stato.uscite[k];
            if (u && u.attiva && u.sospesa && u.fino) {
                fino = Math.max(fino, u.fino);
            }
        }
        if (!fino) {
            return "sospesa";
        }
        return "sospesa fino alle " + Qt.formatTime(new Date(fino * 1000), "HH:mm");
    }

    onStatoChanged: interruttore.checked = accesa

    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: (source, data) => {
            if (source.endsWith(" stato")) {
                try {
                    root.stato = JSON.parse(data["stdout"]);
                } catch (e) {
                    root.stato = null;
                }
            }
            disconnectSource(source);
        }
    }

    Timer {
        interval: 3000
        repeat: true
        triggeredOnStart: true
        running: root.attivo
        onTriggered: root.aggiorna()
    }

    Timer {
        id: presto
        interval: 700
        onTriggered: root.aggiorna()
    }

    // know at once whether to show the switch at all
    Component.onCompleted: aggiorna()

    contentItem: RowLayout {
        spacing: Kirigami.Units.gridUnit

        Kirigami.Icon {
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: Kirigami.Units.iconSizes.medium
            Layout.preferredHeight: Kirigami.Units.iconSizes.medium
            source: "brightness-high"
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            spacing: Kirigami.Units.smallSpacing

            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                PlasmaComponents3.Label {
                    text: "Luminosità adattiva"
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }

                PlasmaComponents3.Label {
                    id: dettaglio
                    text: root.descrizione()
                    textFormat: Text.PlainText
                    opacity: 0.75
                }
            }

            PlasmaComponents3.Switch {
                id: interruttore
                Layout.fillWidth: true
                activeFocusOnTab: false
                focusPolicy: Qt.NoFocus
                text: "Segui la luce della stanza"
                onToggled: root.invia(checked ? "riprendi" : "pausa")
            }
        }
    }
}
