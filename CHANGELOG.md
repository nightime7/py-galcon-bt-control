# Changelog

## 1.2.37 - 2026-11-06

### Added

- Added a Home Assistant seasonal status switch that can disable adjustment
  by staging the neutral `100%` value.
- Added seasonal percentage read-back from controller status byte 16.
- Added a seasonal enabled checkbox to the desktop GUI.
- Added device-level Apply and Discard actions for staged seasonal and
  rain-off changes.

### Fixed

- Matched the controller's seasonal semantics: `100%` reports disabled and
  every other percentage reports enabled, with percentage as the only value
  written to the controller.
- Limited Home Assistant seasonal adjustment to verified 10% increments.

## 1.2.36 - 2026-10-20

### Added

- Added a Home Assistant manual-duration number for each zone.
- Added MQTT topics at `zone/{zone}/manual_duration/set` for configuring how
  long a plain `ON` command should water.
- Added logging of the resolved manual watering duration.

### Fixed

- Fixed Home Assistant manual zone starts always watering for one minute.
- Added validation for manual watering durations from 1 through 255 minutes.

## 1.2.35 - 2026-09-05

### Added

- Added staged Home Assistant controls for program duration, weekdays, and
  four start windows per zone.
- Added per-zone Apply and Discard actions for schedule changes.