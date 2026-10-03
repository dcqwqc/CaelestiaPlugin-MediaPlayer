import Caelestia.Plugins

SettingsObject {
    property bool showArtworkBackdrop: true
    SettingMeta on showArtworkBackdrop { label: "Artwork backdrop"; description: "Use the current album art as a restrained background layer behind the media controls."; icon: "image"; inputType: SettingMeta.Switch }

    property int artworkIntensity: 16
    SettingMeta on artworkIntensity { label: "Artwork intensity"; description: "Opacity of the artwork backdrop."; icon: "opacity"; inputType: SettingMeta.SpinBox; min: 0; max: 40; step: 1 }

    property bool showVolume: true
    SettingMeta on showVolume { label: "Volume control"; description: "Show a touch-draggable output-volume slider."; icon: "volume_up"; inputType: SettingMeta.Switch }

    property bool showBrightness: true
    SettingMeta on showBrightness { label: "Brightness control"; description: "Show a touch-draggable brightness slider when an active monitor is available."; icon: "brightness_6"; inputType: SettingMeta.Switch }

    property int touchScalePercent: 110
    SettingMeta on touchScalePercent { label: "Touch target scale"; description: "Scale primary transport and utility controls."; icon: "touch_app"; inputType: SettingMeta.SpinBox; min: 90; max: 140; step: 5 }

    property bool syncHyprlandChrome: true
    SettingMeta on syncHyprlandChrome { label: "Match Hyprland chrome"; description: "Derive runtime borders and shadows from the current Caelestia palette. No Hyprland config file is edited."; icon: "palette"; inputType: SettingMeta.Switch }

    property int shadowOpacityPercent: 22
    SettingMeta on shadowOpacityPercent { label: "Window shadow strength"; description: "Opacity used for palette-matched Hyprland shadows."; icon: "blur_on"; inputType: SettingMeta.SpinBox; min: 0; max: 60; step: 1 }
}
