import QtQuick
import Quickshell
import Quickshell.Io
import Caelestia.Plugins
import qs.services
import qs.utils

Item {
    id: root
    property SettingsObject settings: null
    readonly property string helper: Paths.toLocalFile(Qt.resolvedUrl("scripts/theme-sync"))
    readonly property color activeColour: Colours.palette.m3primary
    readonly property color inactiveColour: Colours.palette.m3outlineVariant
    readonly property color shadowColour: Colours.palette.m3shadow
    property bool applied: false

    visible: false
    implicitWidth: 0
    implicitHeight: 0

    function queueApply(): void { if (settings) applyTimer.restart(); }
    function applyTheme(): void {
        if (!settings || themeProc.running) return;
        if (!settings.syncHyprlandChrome) {
            if (applied) {
                themeProc.command = [helper, "restore"];
                themeProc.running = true;
                applied = false;
            }
            return;
        }
        themeProc.command = [helper, "apply", String(activeColour), String(inactiveColour), String(shadowColour), String(settings.shadowOpacityPercent)];
        themeProc.running = true;
        applied = true;
    }

    onSettingsChanged: queueApply()
    onActiveColourChanged: queueApply()
    onInactiveColourChanged: queueApply()
    onShadowColourChanged: queueApply()
    Component.onCompleted: queueApply()
    Component.onDestruction: if (applied) Quickshell.execDetached([helper, "restore"])

    Connections {
        target: root.settings
        function onChanged(): void { root.queueApply(); }
    }


    Timer { id: applyTimer; interval: 120; repeat: false; onTriggered: root.applyTheme() }
    Process { id: themeProc; running: false }
}
