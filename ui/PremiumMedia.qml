pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Widgets
import Quickshell.Services.Mpris
import Caelestia.Config
import Caelestia.Plugins
import qs.components
import qs.components.controls
import qs.services

Item {
    id: root

    property SettingsObject settings: null
    readonly property real touchScale: Math.max(0.9, Math.min(1.4, Number(settings?.touchScalePercent ?? 110) / 100))
    readonly property var activeMonitor: Brightness.getMonitor("active") ?? (Brightness.monitors.length > 0 ? Brightness.monitors[0] : null)
    readonly property real screenWidth: activeMonitor?.modelData?.width ?? 1200
    readonly property bool compact: screenWidth < 900

    implicitWidth: compact ? Math.max(520, Math.min(760, screenWidth - 100)) : 940
    implicitHeight: compact ? 720 : 560

    function timeString(seconds): string {
        if (!isFinite(seconds) || seconds < 0) return "--:--";
        const s = Math.floor(seconds);
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        const sec = String(s % 60).padStart(2, "0");
        return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
    }

    Timer {
        running: Players.active?.isPlaying ?? false
        interval: GlobalConfig.dashboard.mediaUpdateInterval
        repeat: true
        triggeredOnStart: true
        onTriggered: Players.active?.positionChanged()
    }

    ClippingRectangle {
        anchors.fill: parent
        radius: Tokens.rounding.extraLarge
        color: Colours.palette.m3surfaceContainerLow

        Image {
            anchors.fill: parent
            source: Players.getArtUrl(Players.active)
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            opacity: root.settings?.showArtworkBackdrop === false ? 0 : Math.max(0, Math.min(0.4, Number(root.settings?.artworkIntensity ?? 16) / 100))
        }

        StyledRect {
            anchors.fill: parent
            color: Qt.alpha(Colours.palette.m3surface, 0.82)
        }

        GridLayout {
            anchors.fill: parent
            anchors.margins: Tokens.padding.large
            columns: root.compact ? 1 : 2
            rowSpacing: Tokens.spacing.large
            columnSpacing: Tokens.spacing.extraLarge

            ClippingRectangle {
                Layout.alignment: Qt.AlignTop | Qt.AlignHCenter
                Layout.preferredWidth: root.compact ? Math.min(320, root.width - Tokens.padding.extraLarge * 2) : 330
                Layout.preferredHeight: width
                radius: Tokens.rounding.extraLarge
                color: Colours.palette.m3surfaceContainerHighest

                Image {
                    id: artwork
                    anchors.fill: parent
                    source: Players.getArtUrl(Players.active)
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                }

                MaterialIcon {
                    anchors.centerIn: parent
                    visible: artwork.status === Image.Null || artwork.status === Image.Error
                    text: artwork.status === Image.Error ? "broken_image" : "music_note"
                    color: Colours.palette.m3onSurfaceVariant
                    fontStyle: Tokens.font.icon.size(Math.max(56, parent.width * 0.22)).build()
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: Tokens.spacing.medium

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Tokens.spacing.small

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Tokens.spacing.extraSmall

                        StyledText {
                            Layout.fillWidth: true
                            text: Players.active?.trackTitle || qsTr("Nothing playing")
                            font: Tokens.font.headline.medium
                            color: Colours.palette.m3onSurface
                            elide: Text.ElideRight
                            maximumLineCount: 1
                        }

                        StyledText {
                            Layout.fillWidth: true
                            text: Players.active ? (Players.active.trackArtist || qsTr("Unknown artist")) : qsTr("Start some media and it will appear here")
                            font: Tokens.font.body.large
                            color: Colours.palette.m3onSurfaceVariant
                            elide: Text.ElideRight
                            maximumLineCount: 1
                        }

                        StyledText {
                            Layout.fillWidth: true
                            visible: !!Players.active
                            text: Players.active?.trackAlbum || qsTr("Unknown album")
                            font: Tokens.font.body.medium
                            color: Colours.palette.m3outline
                            elide: Text.ElideRight
                            maximumLineCount: 1
                        }
                    }

                    StyledRect {
                        visible: Players.list.length > 1
                        implicitWidth: pickerText.implicitWidth + Tokens.padding.large * 2
                        implicitHeight: Math.max(44 * root.touchScale, pickerText.implicitHeight + Tokens.padding.small * 2)
                        radius: Tokens.rounding.full
                        color: Colours.palette.m3secondaryContainer

                        StyledText {
                            id: pickerText
                            anchors.centerIn: parent
                            text: Players.active ? Players.getIdentity(Players.active) : qsTr("Player")
                            color: Colours.palette.m3onSecondaryContainer
                            font: Tokens.font.label.medium
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    visible: Players.list.length > 1
                    spacing: Tokens.spacing.small

                    Repeater {
                        model: Players.list

                        StyledRect {
                            required property var modelData
                            Layout.fillWidth: true
                            implicitHeight: Math.max(44 * root.touchScale, 44)
                            radius: Tokens.rounding.full
                            color: Players.active === modelData ? Colours.palette.m3primaryContainer : Colours.palette.m3surfaceContainerHigh

                            StyledText {
                                anchors.centerIn: parent
                                width: parent.width - Tokens.padding.medium * 2
                                horizontalAlignment: Text.AlignHCenter
                                text: Players.getIdentity(parent.modelData)
                                color: Players.active === parent.modelData ? Colours.palette.m3onPrimaryContainer : Colours.palette.m3onSurfaceVariant
                                font: Tokens.font.label.medium
                                elide: Text.ElideRight
                            }

                            TapHandler { onTapped: Players.manualActive = parent.modelData }
                        }
                    }
                }

                Item {
                    Layout.fillWidth: true
                    implicitHeight: 48 * root.touchScale
                    enabled: Players.active?.canSeek ?? false

                    StyledSlider {
                        id: seek
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        implicitHeight: Math.max(14, 14 * root.touchScale)
                        enabled: parent.enabled
                        value: Players.active?.length > 0 ? Players.active.position / Players.active.length : 0
                        interactionOnMove: false
                        wavy: true
                        animateWave: Players.active?.isPlaying ?? false
                        onInteraction: v => {
                            const p = Players.active;
                            if (p?.canSeek && p?.positionSupported && p.length > 0) p.position = v * p.length;
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    StyledText { text: root.timeString(seek.dragging ? seek.pos * (Players.active?.length ?? 0) : (Players.active?.position ?? -1)); color: Colours.palette.m3onSurfaceVariant; font: Tokens.font.label.medium }
                    Item { Layout.fillWidth: true }
                    StyledText { text: root.timeString(Players.active?.length ?? -1); color: Colours.palette.m3onSurfaceVariant; font: Tokens.font.label.medium }
                }

                RowLayout {
                    Layout.alignment: Qt.AlignHCenter
                    spacing: Tokens.spacing.small

                    IconButton {
                        type: IconButton.Tonal
                        icon: "shuffle"
                        isRound: true
                        checked: Players.active?.shuffle ?? false
                        disabled: !(Players.active?.shuffleSupported ?? false)
                        implicitWidth: 48 * root.touchScale
                        implicitHeight: 48 * root.touchScale
                        onClicked: if (Players.active) Players.active.shuffle = !Players.active.shuffle
                    }
                    IconButton {
                        type: IconButton.Tonal
                        icon: "skip_previous"
                        isRound: true
                        disabled: !(Players.active?.canGoPrevious ?? false)
                        implicitWidth: 52 * root.touchScale
                        implicitHeight: 52 * root.touchScale
                        onClicked: Players.active?.previous()
                    }
                    IconButton {
                        icon: Players.active?.isPlaying ? "pause" : "play_arrow"
                        isRound: true
                        checked: Players.active?.isPlaying ?? false
                        disabled: !(Players.active?.canTogglePlaying ?? false)
                        implicitWidth: 64 * root.touchScale
                        implicitHeight: 64 * root.touchScale
                        onClicked: Players.active?.togglePlaying()
                    }
                    IconButton {
                        type: IconButton.Tonal
                        icon: "skip_next"
                        isRound: true
                        disabled: !(Players.active?.canGoNext ?? false)
                        implicitWidth: 52 * root.touchScale
                        implicitHeight: 52 * root.touchScale
                        onClicked: Players.active?.next()
                    }
                    IconButton {
                        type: IconButton.Tonal
                        icon: Players.active?.loopState === MprisLoopState.Track ? "repeat_one" : "repeat"
                        isRound: true
                        checked: Players.active?.loopState === MprisLoopState.Track || Players.active?.loopState === MprisLoopState.Playlist
                        disabled: !(Players.active?.loopSupported ?? false)
                        implicitWidth: 48 * root.touchScale
                        implicitHeight: 48 * root.touchScale
                        onClicked: {
                            if (!Players.active) return;
                            const state = Players.active.loopState;
                            Players.active.loopState = state === MprisLoopState.None ? MprisLoopState.Track : (state === MprisLoopState.Track ? MprisLoopState.Playlist : MprisLoopState.None);
                        }
                    }
                }

                StyledRect {
                    Layout.fillWidth: true
                    implicitHeight: utilityLayout.implicitHeight + Tokens.padding.medium * 2
                    radius: Tokens.rounding.large
                    color: Colours.palette.m3surfaceContainer

                    ColumnLayout {
                        id: utilityLayout
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.margins: Tokens.padding.medium
                        spacing: Tokens.spacing.medium

                        RowLayout {
                            Layout.fillWidth: true
                            visible: root.settings?.showVolume !== false
                            spacing: Tokens.spacing.medium

                            IconButton {
                                type: IconButton.Tonal
                                icon: Audio.muted ? "volume_off" : (Audio.volume < 0.5 ? "volume_down" : "volume_up")
                                isRound: true
                                implicitWidth: 48 * root.touchScale
                                implicitHeight: 48 * root.touchScale
                                onClicked: Audio.setStreamMuted(Audio.sink, !Audio.muted)
                            }
                            Item {
                                Layout.fillWidth: true
                                implicitHeight: 48 * root.touchScale
                                StyledSlider {
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    implicitHeight: 14
                                    value: Math.min(1, Audio.volume)
                                    onInteraction: v => Audio.setVolume(v)
                                }
                            }
                            StyledText { text: `${Math.round(Math.min(1, Audio.volume) * 100)}%`; color: Colours.palette.m3onSurfaceVariant; font: Tokens.font.label.medium; Layout.preferredWidth: 46 }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            visible: root.settings?.showBrightness !== false && root.activeMonitor !== null
                            spacing: Tokens.spacing.medium

                            IconButton {
                                type: IconButton.Tonal
                                icon: `brightness_${Math.max(1, Math.min(7, Math.round((root.activeMonitor?.brightness ?? 0.5) * 6) + 1))}`
                                isRound: true
                                disabled: true
                                implicitWidth: 48 * root.touchScale
                                implicitHeight: 48 * root.touchScale
                            }
                            Item {
                                Layout.fillWidth: true
                                implicitHeight: 48 * root.touchScale
                                StyledSlider {
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    implicitHeight: 14
                                    value: root.activeMonitor?.brightness ?? 0
                                    onInteraction: v => root.activeMonitor?.setBrightness(v)
                                }
                            }
                            StyledText { text: `${Math.round((root.activeMonitor?.brightness ?? 0) * 100)}%`; color: Colours.palette.m3onSurfaceVariant; font: Tokens.font.label.medium; Layout.preferredWidth: 46 }
                        }
                    }
                }
            }
        }
    }
}
